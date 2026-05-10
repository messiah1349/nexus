# Nexus — Implementation Plan (Draft v0.1)

## Strategy

Build vertically, not horizontally. Every phase ends with something **runnable end-to-end** — even if it covers only a sliver of the eventual feature set. This is the opposite of "first build all the DB, then all the LLM, then all the UI." That path always ships nothing for months.

Each phase has:
- **Goal**: what new behavior the system gains
- **Done when**: testable definition of done
- **Cuts**: what we explicitly defer

## Phase 0 — Repo bootstrap (~½ day)

**Goal:** Empty project boots; you can `uv run nexus --help`.

- `pyproject.toml`, `uv.lock`, `.env.example`, `.gitignore`
- `docker-compose.yml` with postgres + pgvector
- `nexus/config.py` reads env
- `nexus/clients/cli.py` with one `hello` command
- `tests/` skeleton; `pytest` runs (no real tests yet)
- Init git repo (currently not initialized — confirm before)

**Done when:** `docker compose up -d`, `uv run nexus hello` prints something, `pytest` runs with 0 tests.

**Cuts:** No DB connection yet, no LLM calls.

---

## Phase 1 — DB foundation (1–2 days)

**Goal:** Fixed schema lives in code, migrates cleanly, basic CRUD works.

- `nexus/db/models.py` — all tables from `docs/schema.md`
- Alembic init + first migration
- `pgvector` extension enabled in migration
- `nexus/db/repository.py` — minimum useful set:
  - `create_user`, `create_project`
  - `add_message`, `recent_messages(project_id, limit)`
  - `add_event`, `recent_events(project_id, since)`
  - `upsert_entity`, `get_entity_by_name`
- CLI: `nexus user create`, `nexus project create --domain language_learning`
- Integration tests against compose-managed postgres

**Done when:** Can create a user + project, write/read messages and events via CLI, alembic up/down works cleanly.

**Cuts:** No embeddings yet, no LLM, no domain validation.

---

## Phase 2 — Domain config + Architect MVP (2–3 days)

**Goal:** Architect can interview a user and produce a validated `DomainConfig`.

- `nexus/domains/base.py` — pydantic `DomainConfig` schema
- `nexus/domains/language_learning.yaml` — hand-written gold standard
- `nexus/domains/registry.py` — load + validate YAML configs
- `nexus/llm/client.py` + `nexus/llm/anthropic.py` — minimal chat interface
- `nexus/architect/interview.py` — multi-turn loop:
  - System prompt explains the schema
  - Asks user about goals, focus tags, preferred cadence
  - Outputs a `DomainConfig` JSON
  - Validates against pydantic; if invalid, loops back to fix
- `nexus/architect/config_writer.py` — writes config to `projects.config`
- CLI: `nexus architect run --user <id>` runs interactive interview

**Done when:** You can run the architect on language-learning, end up with a valid `DomainConfig` saved to `projects.config`, and the gold-standard YAML validates against the same schema.

**Cuts:** No specialist yet. Architect doesn't need to be perfect — only "produces a valid config." Refinement comes later.

---

## Phase 3 — Specialist v1: episodic-only loop (2–3 days)

**Goal:** End-to-end conversation works. CLI chat. Agent reads recent messages, responds, and can log entities + events via tool calls.

- `nexus/specialist/tools.py` — tool schemas (Anthropic tool-use format):
  - `create_entity(type, name, attributes)`
  - `update_entity_state(entity_id, state_patch)`
  - `log_event(type, payload, entity_names?, occurred_at?)`
- `nexus/specialist/memory.py` — Phase 3 version: just last-N messages
- `nexus/specialist/agent.py` — main loop:
  1. Load project + DomainConfig
  2. Build system prompt from config (entity types, event types, tone)
  3. Append recent messages (episodic)
  4. Call LLM with tools
  5. Execute tool calls, persist results
  6. Persist assistant message
- CLI: `nexus chat --project <id>` interactive loop

**Done when:** You can run a 5-minute language-learning session via CLI, end up with vocab entities and review events in the DB, and the agent recalls earlier turns.

**Cuts:** No semantic search, no summaries, no structural stats. Domain-specific behavior comes purely from the system prompt + DomainConfig.

---

## Phase 4 — Telegram client (2–3 days)

**Goal:** Telegram is the primary user-facing client. Uses the same specialist loop as the CLI.

- Define `nexus/clients/base.py` — `ClientAdapter` protocol (send_text, on_user_message, attach_specialist). Refactor `cli.py` to implement it. **Don't design this protocol until now** — designing it before a second client exists is guessing.
- `nexus/clients/telegram.py` — bot with handlers:
  - `/start` → user lookup or creation, auth via `users.telegram_id`
  - `/projects` → list user's projects
  - `/use <project>` → set active project for this chat
  - `/architect` → run the architect interview in-chat
  - default text → forward to `specialist.agent`
- Multi-user routing falls out for free because everything is keyed on `(user_id, project_id)`.

**Done when:** You can use Nexus end-to-end from Telegram on your phone with the language-learning project.

**Cuts:** No semantic recall or structural stats yet — that lands in 5 and 6. The agent will feel a bit shallow. That's fine; the goal is "primary client works."

---

## Phase 5 — Embeddings + semantic memory (2 days)

**Goal:** Agent can recall things from much earlier than the episodic window.

- `nexus/llm/embeddings.py` — embedding client (provider-agnostic)
- `nexus/workers/embedder.py` — async worker:
  - Watches new rows in `messages`, `summaries`, `entities`
  - Chunks + embeds → writes to `embeddings`
- `nexus/db/repository.py` — `semantic_search(project_id, query, k)`
- `nexus/specialist/memory.py` — extend to fetch top-k semantic chunks
- CLI: `nexus reindex --project <id>` for backfills

**Done when:** Agent answers a question whose answer lies before the episodic window (e.g., "what was my hardest word last week?").

**Cuts:** Not optimizing chunking or HNSW params yet — defaults are fine.

---

## Phase 6 — Structural stats + summaries (2–3 days)

**Goal:** Third memory tier. Agent has access to typed aggregations and periodic recaps.

- `nexus/specialist/stats.py` — registered stat handlers, e.g.:
  - `vocab_mastery_distribution(project_id)`
  - `daily_practice_streak(project_id)`
  - `prs_last_30_days(project_id)`
- `nexus/domains/handlers.py` — state-update rules referenced by `event_types[*].updates_state` in DomainConfig (e.g., `epley_estimate`, `spaced_repetition_v1`)
- Wire stats into specialist's system prompt as a "current state" block
- `nexus/specialist/summarizer.py` — generates daily/weekly summaries
- `nexus/workers/scheduler.py` — triggers summarizer on cadence

**Done when:** All three memory tiers are present in the specialist's prompt; daily summary lands in `summaries` table for an active project.

---

## Phase 6 — Second domain: fitness (1–2 days)

**Goal:** Validate the abstraction by adding a domain that's structurally different from language learning. If it breaks, refactor before going further.

- `nexus/domains/fitness.yaml`
- New stat handlers: `prs_last_30_days`, `weekly_volume_per_muscle_group`, `bodyweight_trend`
- New state-update handler: `epley_estimate`
- Run an end-to-end fitness session in CLI

**Done when:** A user can have one language-learning project and one fitness project under the same account; both work without code changes outside `domains/`, `specialist/stats.py`, and `domains/handlers.py`.

**Refactor budget:** if the abstraction strains here, fix it now — don't paper over it.

---

## Phase 7 — MCP bridge (1–2 days, optional/deferrable)

**Goal:** Expose the DB and stats as MCP tools so external agents can interact with project data.

- `nexus/mcp/server.py` — MCP server, tools:
  - `query_recent(project_id, source, limit)`
  - `semantic_search(project_id, query, k)`
  - `get_structural_stats(project_id, stat_name)`
  - `log_event(...)`, `create_entity(...)`
- Reuse `repository.py` and `specialist/stats.py` — no duplicate logic

**Done when:** Claude Desktop or another MCP client can connect to the local server and run queries against your data.

**Cuts:** Auth is single-user/local-only at this stage.

---

## Phase 8 — Telegram client (2–3 days)

**Goal:** Production interface. Family/friends could use it.

- `nexus/clients/telegram.py` — bot with handlers:
  - `/start` → user lookup or creation, auth via telegram_id
  - `/projects` → list user's projects
  - `/use <project>` → set active project for the chat
  - `/architect` → kick off interview (in-chat)
  - default text → forward to specialist agent
- Multi-user routing already trivial because everything is keyed on `project_id` + `user_id`

**Done when:** You can fully use the system from Telegram on your phone.

---

## Phase 9 — Polish & ops (ongoing)

- Cost tracking per project (`observability/costs.py`)
- Structured logging review
- Backup/export commands (dump a project's data to JSON)
- Privacy: per-project encryption key for sensitive content (defer until concrete need)
- Observability dashboard (optional — Grafana over postgres metrics)

---

## Things explicitly NOT in this plan

- LLM-generated SQLAlchemy models or any runtime codegen — replaced by DomainConfig.
- Per-domain Docker provisioning step — replaced by a single fixed schema.
- Schema-per-project hard isolation — `project_id` filtering until proven insufficient.
- Web UI — Telegram and CLI cover the targeted use cases.
- Multi-LLM routing inside a single conversation — single provider per project for now.

## Risks worth flagging

1. **Architect quality.** A bad interview produces a bad DomainConfig and the specialist behaves oddly. Mitigation: pydantic validation + always-on schema preview before saving + ability to edit YAML by hand.
2. **Tool reliability.** LLMs sometimes call tools with wrong shapes. Mitigation: pydantic-validated tool inputs; on validation error, return error to LLM and let it retry once.
3. **Embedding cost.** Re-embedding everything on every change is expensive. Mitigation: only embed on insert, not on every state update; have a `reindex` command for big changes.
4. **State update bugs.** A buggy `updates_state` handler corrupts entity state silently. Mitigation: handlers are pure functions, tested directly; events are immutable so state can be rebuilt by replay if needed.
5. **Domain abstraction leakage.** Fitness or a future domain may not fit. Mitigation: Phase 6 is explicitly the stress test; if it breaks, fix the abstraction before adding domain #3.

## Suggested first three sessions

1. **Phase 0 + Phase 1**: get the repo and DB walking. Concrete, low-risk, lots of typing.
2. **Phase 2**: the Architect. Most novel piece — worth tackling while energy is high.
3. **Phase 3**: end-to-end specialist. Once this is up, every later phase is "extend a working system" rather than "build something new."
