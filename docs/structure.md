# Nexus — Project Structure (Draft v0.2)

## Tree (target shape after Phase 4)

```
memory_assistant/
├── README.md
├── pyproject.toml              # uv-managed
├── uv.lock
├── .env.example
├── .gitignore
├── docker-compose.yml          # postgres+pgvector
├── alembic.ini
├── alembic/
│   ├── env.py                  # async pattern, reads URL from nexus.config
│   └── versions/
│       ├── 0001_initial_schema.py        # Phase 1 — all 8 base tables + pgvector
│       └── 0002_plans_sessions.py        # Phase 2 — plans, sessions, message/summary FKs
│
├── nexus/
│   ├── __init__.py
│   ├── settings.py             # env-var loading via pydantic-settings
│   │
│   ├── config/                 # human-tunable assets
│   │   ├── loaders.py          # load_prompt, render_prompt, load_domain_yaml
│   │   ├── prompts/            # .md, $placeholder substitution
│   │   │   ├── architect_system.md
│   │   │   ├── specialist_system.md
│   │   │   ├── summarize_language_learning.md
│   │   │   └── summarize_fitness.md
│   │   └── domains/
│   │       ├── language_learning.yaml
│   │       └── fitness.yaml
│   │
│   ├── db/                     # Phase 1
│   │   ├── engine.py
│   │   ├── models.py
│   │   └── repository.py
│   │
│   ├── llm/                    # Phase 2
│   │   ├── base.py             # provider-agnostic chat interface
│   │   ├── anthropic.py
│   │   └── tokens.py
│   │
│   ├── domains/                # Phase 2 — pydantic schemas only
│   │   ├── base.py             # DomainConfig + ArchitectProposal
│   │   └── registry.py         # validates YAML from nexus.config
│   │
│   ├── architect/              # Phase 2
│   │   ├── interview.py
│   │   ├── prompts.py          # thin wrapper around render_prompt
│   │   └── persist.py
│   │
│   ├── specialist/             # Phase 3
│   │   ├── session.py          # lifecycle: open/resume, end (explicit + timeout)
│   │   ├── context.py          # build system prompt + chat history once per session
│   │   ├── agent.py            # the chat loop
│   │   ├── summarizer.py       # session-end summary + plan-item update + optional revision
│   │   └── prompts.py
│   │
│   ├── clients/                # Phase 3 (CLI), Phase 4 (Telegram)
│   │   ├── base.py             # ClientAdapter protocol — designed *when* we have two
│   │   ├── cli.py
│   │   └── telegram.py
│   │
│   ├── workers/                # Phase 4 — small in-process scheduler
│   │   └── timeout.py          # idle-timeout sweeper for active sessions
│   │
│   └── observability/
│       ├── logging.py
│       └── costs.py
│
├── tests/
│   ├── conftest.py
│   ├── test_smoke.py
│   ├── integration/
│   │   ├── test_repository.py             # Phase 1 ✓
│   │   ├── test_plans_sessions.py         # Phase 2
│   │   ├── test_architect_flow.py         # Phase 2
│   │   └── test_specialist_loop.py        # Phase 3
│   └── fixtures/
│       └── canned_interviews/
│
└── docs/
    ├── schema.md
    ├── structure.md
    ├── plan.md
    └── use_case_language_learning.md
```

## What's *not* in this tree

Compared to the v0.1 draft, these directories are gone (deferred to v2 — see `docs/plan.md`):

- `nexus/specialist/tools.py` — no per-turn tool calls in v1.
- `nexus/specialist/stats.py` — no structural-stat functions in v1.
- `nexus/domains/handlers.py` — no state-update handlers (`spaced_repetition_v1`, `epley_estimate`) in v1.
- `nexus/workers/embedder.py` — no embedding pipeline in v1.
- `nexus/mcp/server.py` — deferred to V2.6.

When v2 features land, the corresponding files appear without disturbing v1 code paths.

## Module responsibilities (one-liners, v1 path)

- **`db/`** — owns the schema and all SQL. Nothing else writes raw queries.
- **`llm/`** — provider-agnostic chat. Switching providers means changing one module.
- **`domains/`** — declarative descriptions: pydantic schema + per-domain YAML.
- **`architect/`** — runs the onboarding interview; output is a `DomainConfig` + one or more `Plan`s.
- **`specialist/`** — the runtime agent. **No mid-turn writes beyond `messages`.** Reads context once at session start, talks, writes summary + plan update at session end.
- **`clients/`** — transport. CLI for dev iteration, Telegram for production. Thin adapters around `specialist.agent` + `architect.interview`.
- **`workers/`** — idle-timeout sweeper. Tiny in v1; grows in v2.

## Key import direction (one-way)

```
clients ──▶ specialist ──▶ llm
                       ──▶ db (via repository)
                       ──▶ domains (read config)

clients ──▶ architect ──▶ llm
                      ──▶ db (writes projects.config, plan rows)
                      ──▶ domains

workers ──▶ db
```

`db`, `llm`, `domains`, `observability` are leaf modules — nothing in them imports anything else from `nexus/`.

## Configuration

`.env` covers:
- `POSTGRES_URL`
- `ANTHROPIC_API_KEY`
- `TELEGRAM_BOT_TOKEN` (Phase 4+)
- `LOG_LEVEL`, `ENV`

Anything domain-specific lives in `projects.config`. Anything per-user lives in `users.settings` (including the `active_project_per_chat` map for Telegram routing).

## Tooling (recommended)

| Concern | Pick | Why |
|---|---|---|
| Package manager | **uv** | already in use |
| ORM | SQLAlchemy 2.0 async | already in use |
| Migrations | Alembic | already in use |
| Validation | pydantic v2 | for DomainConfig |
| LLM SDK | `anthropic` | first provider |
| CLI | typer | already in use |
| Telegram | python-telegram-bot v21 | mature, async |
| Logging | structlog | structured logs |
| Tests | pytest + pytest-asyncio | already in use |
| Background jobs | asyncio loop (in-process) | dramatically simpler than Celery for v1 |
