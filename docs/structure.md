# Nexus — Project Structure (Draft v0.1)

## Tree

```
memory_assistant/
├── README.md
├── pyproject.toml              # uv-managed (recommended) — see "Tooling choices"
├── uv.lock
├── .env.example                # POSTGRES_URL, ANTHROPIC_API_KEY, etc.
├── .gitignore
├── docker-compose.yml          # postgres+pgvector, app, optional pgadmin
├── Dockerfile
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/               # real migrations — no LLM-generated DDL
│
├── nexus/
│   ├── __init__.py
│   ├── config.py               # pydantic-settings: env vars, model choices, paths
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── engine.py           # SQLAlchemy engine + async session factory
│   │   ├── models.py           # FIXED schema — users, projects, entities, events,
│   │   │                       #   event_entities, messages, summaries, embeddings
│   │   └── repository.py       # typed query helpers (get_recent_messages, etc.)
│   │
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── client.py           # provider-agnostic interface (chat, stream)
│   │   ├── anthropic.py        # Anthropic implementation (default)
│   │   ├── embeddings.py       # embedding client (Voyage / local fallback)
│   │   └── tokens.py           # token counting / budget helpers
│   │
│   ├── domains/
│   │   ├── __init__.py
│   │   ├── base.py             # pydantic DomainConfig schema + validators
│   │   ├── registry.py         # load YAML configs, list available domains
│   │   ├── handlers.py         # named state-update handlers (epley_estimate, sm2, ...)
│   │   ├── language_learning.yaml  # gold-standard reference configs
│   │   └── fitness.yaml
│   │
│   ├── architect/
│   │   ├── __init__.py
│   │   ├── interview.py        # multi-turn interview loop
│   │   ├── prompts.py          # system prompts + few-shot examples
│   │   └── config_writer.py    # produces & validates a DomainConfig, writes to projects.config
│   │
│   ├── specialist/
│   │   ├── __init__.py
│   │   ├── agent.py            # NexusSpecialistAgent — main loop, tool dispatch
│   │   ├── memory.py           # three-tier assembly: episodic + semantic + structural
│   │   ├── stats.py            # structural-stat queries (registered by name in domain config)
│   │   ├── tools.py            # tool schemas + handlers (create_entity, log_event, ...)
│   │   ├── summarizer.py       # daily/weekly summary generation
│   │   └── prompts.py
│   │
│   ├── mcp/
│   │   ├── __init__.py
│   │   └── server.py           # MCP server exposing repo + stats as tools
│   │                           # (added in Phase 7 — not Phase 1)
│   │
│   ├── clients/
│   │   ├── __init__.py
│   │   ├── cli.py              # typer/click — develop the agent loop here first
│   │   └── telegram.py         # python-telegram-bot — added in Phase 8
│   │
│   ├── workers/
│   │   ├── __init__.py
│   │   ├── embedder.py         # background: embed new messages/summaries/entities
│   │   └── scheduler.py        # cron-ish: trigger summarizer, spaced-repetition due, etc.
│   │
│   └── observability/
│       ├── __init__.py
│       ├── logging.py          # structured logging (structlog)
│       └── costs.py            # per-project token & cost accounting
│
├── tests/
│   ├── conftest.py             # shared fixtures (test DB, factory_boy)
│   ├── unit/
│   │   ├── test_domain_config.py
│   │   ├── test_handlers.py
│   │   └── test_memory_assembly.py
│   ├── integration/            # hits real postgres via docker-compose
│   │   ├── test_repository.py
│   │   ├── test_architect_flow.py
│   │   └── test_specialist_loop.py
│   └── fixtures/
│       └── domain_configs/
│
└── docs/
    ├── schema.md               # this draft
    ├── structure.md            # this file
    ├── plan.md                 # implementation plan
    └── decisions/              # ADR-style notes as decisions accumulate
```

## Module responsibilities (one-liners)

- **`db/`** — owns the schema and all SQL. Nothing else writes raw queries; everything goes through `repository.py`.
- **`llm/`** — provider abstraction. Switching from Anthropic to local llama-cpp later means changing one module.
- **`domains/`** — declarative descriptions of what each domain looks like. **No business logic** here, just schemas and named handlers.
- **`architect/`** — read-only on the DB except for writing `projects.config`. Its only output is a validated DomainConfig.
- **`specialist/`** — the runtime agent. Reads memory, calls LLM, dispatches tools that write events/entities/messages.
- **`mcp/`** — thin wrapper. Reuses `repository.py` and `specialist/stats.py`. Added late, not first.
- **`clients/`** — transport. CLI for development, Telegram for production. Both are thin adapters around `specialist.agent`.
- **`workers/`** — async/background work that doesn't belong in the request path (embedding, summarization, due-item triggers).

## Key import direction (one-way)

```
clients ──▶ specialist ──▶ llm
                       ──▶ db (via repository)
                       ──▶ domains (read config)
                       ──▶ specialist.stats / tools

architect ──▶ llm
          ──▶ domains
          ──▶ db (writes only projects.config + creates project rows)

workers   ──▶ db
          ──▶ llm

mcp       ──▶ db (via repository)
          ──▶ specialist.stats
```

`db`, `llm`, `domains`, `observability` are leaf modules — nothing in them imports anything else from `nexus/`.

## Tooling choices (recommended, open to override)

| Concern | Pick | Why |
|---|---|---|
| Package manager | **uv** | fastest, lockfile, drop-in for pip workflows |
| ORM | SQLAlchemy 2.0 (async) | already implied by your design |
| Migrations | Alembic | standard SQLAlchemy companion |
| Validation | pydantic v2 | for DomainConfig + LLM tool schemas |
| LLM SDK | `anthropic` | matches your existing usage |
| Embeddings | Voyage (`voyage-3`) or local `bge-base` | decide based on local/cloud preference |
| Vector store | pgvector | already postgres, no extra service |
| CLI | typer | minimal boilerplate |
| Telegram | python-telegram-bot v21 | mature, async |
| Logging | structlog | structured logs into one place |
| Tests | pytest + pytest-asyncio + testcontainers (or compose) | real postgres in CI |
| Background jobs | APScheduler or simple asyncio loop | dramatically simpler than Celery for solo/small deployments |

## Configuration

`.env` covers:
- `POSTGRES_URL`
- `ANTHROPIC_API_KEY`
- `VOYAGE_API_KEY` (or local embed model path)
- `TELEGRAM_BOT_TOKEN` (Phase 8+)
- `LOG_LEVEL`, `ENV` (dev / prod)

Anything domain-specific lives in `projects.config`, not env. Anything per-user lives in `users.settings`.

## Where the "modular for local→remote later" promise lives

- **`llm/client.py`** abstracts the provider — swap Anthropic for a local model without touching `specialist/`.
- **`db/engine.py`** reads `POSTGRES_URL` — local docker today, managed Postgres later, no code change.
- **`workers/`** is a standalone process — runs on the same host now, separate worker node later.
- **`clients/`** are thin adapters — `cli.py` for dev, `telegram.py` for users, future `web.py` drops in the same way.
- **`mcp/server.py`** is optional and only needed once you want to expose this DB to other agents/tools outside the project.
