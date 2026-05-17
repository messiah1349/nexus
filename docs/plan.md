# Nexus — Implementation Plan (Draft v0.2)

## Strategy

Build vertically, not horizontally. Every phase ends with something **runnable end-to-end** — even if it covers only a sliver of the eventual feature set. Phase 0 + Phase 1 are done; v1 is five phases away from being a usable Telegram bot.

Each phase has:
- **Goal**: what new behavior the system gains
- **Done when**: testable definition of done
- **Cuts**: what we explicitly defer

The plan distinguishes **v1** (phases 2–5) from **v2** (phase 6+), to keep MVP scope tight.

---

## Phase 0 — Repo bootstrap ✓

Done. Repo, uv, docker-compose, CLI skeleton, smoke tests.

## Phase 1 — DB foundation ✓

Done. Migration 0001 created all 10 tables of the original schema (users, projects, entities, events, event_entities, messages, summaries, embeddings — plus alembic_version). Repository helpers and CLI commands for user/project CRUD. Integration tests pass.

Note: the v1 path uses only a subset of those tables (users, projects, messages, summaries) plus two new tables added in migration 0002 below. The rest (`entities`, `events`, `event_entities`, `embeddings`) are reserved for v2.

---

## Phase 2 — Plans + Sessions + Architect (3–5 days)

**Goal:** Architect interviews the user and produces a `DomainConfig` plus one or more initial `plans`. Sessions table exists; plan/session repo helpers exist. CLI can run the architect.

- Migration **0002**: add `plans` and `sessions` tables; add `session_id` column to `messages` and `summaries`; backfill not needed (no production data).
- `nexus/db/models.py` updated; `nexus/db/repository.py` gains: `create_plan`, `get_active_plans`, `supersede_plan`, `create_session`, `end_session`, `set_session_summary`, `recent_summaries`, `list_messages_for_session`.
- `nexus/domains/base.py` — pydantic `DomainConfig` schema (simplified per `docs/schema.md`).
- `nexus/domains/language_learning.yaml`, `nexus/domains/fitness.yaml` — gold-standard configs.
- `nexus/domains/registry.py` — load + validate YAML configs.
- `nexus/llm/base.py` + `nexus/llm/anthropic.py` — provider-agnostic chat interface (no tool-use needed yet).
- `nexus/architect/interview.py` — multi-turn interview loop. Asks about goals, schedule, level. Produces:
  - `DomainConfig` (validated against pydantic schema)
  - One or more `Plan`s with items (yearly + weekly + optional level_check)
- `nexus/architect/persist.py` — writes config to `projects.config` and plan rows to `plans`.
- CLI: `nexus architect run --user-id ... --domain language_learning` runs the interview interactively, prints plan IDs at the end.
- Integration tests: architect produces valid configs and plans from canned transcripts.

**Done when:** The architect, given a user and a domain, leaves the DB in a state where there is a project with a valid `config` and at least one active plan, and a session can be started against it. All written from CLI; Telegram comes in Phase 4.

**Cuts:** No specialist runtime yet. The architect itself is a session of `kind='architect'` for uniformity, but doesn't need to be summarized in v1 — its output (plan + config) is its summary.

---

## Phase 3 — Specialist v1: no-tool chat loop (3–4 days)

**Goal:** End-to-end conversation works from CLI. Session lifecycle works. At session end, a summary is generated and a plan-item progress update applied.

- `nexus/specialist/session.py` — session lifecycle: `open_or_resume_session(project_id)`, `end_session(session_id, reason)`. Lazy open + idle-timeout close.
- `nexus/specialist/context.py` — context assembly at session start: load active plans, last K session summaries, this session's messages. Build system prompt.
- `nexus/specialist/agent.py` — main loop:
  1. On user message: open or resume session, persist user message.
  2. Build context (system prompt + chat history). Cache the system prompt; only append messages on subsequent turns.
  3. `llm.chat(...)` — no tools.
  4. Persist assistant message.
- `nexus/specialist/summarizer.py` — at session end:
  1. Load all session messages.
  2. LLM call with summarize prompt (domain-specific via `config.summary.prompt_style`).
  3. Output: `(content, focus_tags, plan_item_status_update?, plan_revision?)`.
  4. Write `summaries`, update `sessions`, patch plan items, optionally supersede plan.
- `nexus/specialist/prompts.py` — base + per-domain summarize prompts.
- CLI: `nexus chat --project-id ...` interactive REPL. `/end` to close session and trigger summary; otherwise idle timeout fires on next start.
- CLI: `nexus session list --project-id ...` shows recent sessions + their summary status.

**Done when:** You can run a multi-turn lesson via CLI, type `/end`, and immediately see (a) a new `summaries` row, (b) sessions.status flipped to `completed`, (c) the relevant plan item marked `completed`. Cross-day: starting a new session on Day 2 loads the Day 1 summary into the context.

**Cuts:** No mid-session retrieval. No semantic search. No structured entity/event extraction. No Telegram — that's Phase 4. The agent's prompt is pure prose; no tool schemas in the system prompt.

---

## Phase 4 — Telegram client (2–3 days)

**Goal:** Telegram is the primary user-facing client. Uses the same specialist + architect under the hood as the CLI.

- `nexus/clients/base.py` — `ClientAdapter` protocol (only designed *now* because we have a second client to compare against; designing it earlier would be guessing).
- Refactor `nexus/clients/cli.py` to implement `ClientAdapter`.
- `nexus/clients/telegram.py` — bot with handlers:
  - `/start` → user lookup or creation, auth via `users.telegram_id`
  - `/projects` → list user's projects
  - `/use <project>` → set `users.settings.active_project_per_chat[chat_id]`
  - `/architect` → run architect in-chat
  - `/end` → end current session
  - default text → forward to `specialist.agent`
- Idle-timeout job runs on a small in-process scheduler (APScheduler or simple asyncio loop): every minute, finds sessions where `status='active' AND now() - max(messages.occurred_at) > timeout_minutes` and ends them.

**Done when:** You can run an end-to-end lesson from your phone, including the architect onboarding interview, getting a session summary back, and resuming the next day.

**Cuts:** Single-language UI (English). No voice. No attachments.

---

## Phase 5 — Second domain: fitness (1–2 days)

**Goal:** Validate the abstraction. If language-learning-shaped assumptions leaked in, fix them now — not later.

- `nexus/domains/fitness.yaml`, gold-standard plan templates the architect can use.
- New summarize prompt: `fitness`. Captures sets/reps narratively in the summary text rather than as structured events (those are v2).
- Run an end-to-end fitness session via CLI and Telegram.

**Done when:** A user can have one language-learning project and one fitness project under the same account; both work without any code changes outside `nexus/domains/` and `nexus/specialist/prompts.py`.

**Refactor budget:** if the abstraction strains here, fix it now. Don't ship v1 with two domains that share code by coincidence rather than design.

---

## V2 — Deferred features (planned, not scoped)

These have schema support already (tables exist from migration 0001) but no v1 code path. Sequence is approximate; reorder based on what users actually need after v1 ships.

### V2.1 — Structured extraction at session end

At session end, alongside the summary, the LLM emits structured "things that happened": new `entities` (vocab_word, exercise, ...), new `events` (vocab_review, workout_set, body_measurement), state mutations on existing entities. Writes into `entities`, `events`, `event_entities`. Per-domain extraction prompts; per-domain state-update rules (e.g., `spaced_repetition_v1`, `epley_estimate`).

### V2.2 — Embedding pipeline

Embed messages and summaries on insert. Backfill via `nexus reindex`. Adds value to V2.3.

### V2.3 — Mid-session retrieval

The agent gains one tool: `recall(query, scope?)`. Used during a session when the agent decides it needs older context not in the system-prompt window. Result injected as a system note, surfaces in the next assistant turn.

### V2.4 — Structural stats

Per-domain stat functions: `vocab_mastery_distribution`, `daily_practice_streak`, `weekly_volume_per_muscle_group`, etc. Computed on demand; results join the system-prompt "current state" block.

### V2.5 — Periodic reflective summaries

Weekly + monthly summary scopes. Background scheduler. Adds another tier above session summaries.

### V2.6 — MCP server

Expose project data via MCP tools (`semantic_search`, `query_recent`, `get_structural_stats`, `log_event`). Lets external agents read/write the project's memory.

---

## Things explicitly NOT in this plan

- LLM-generated SQLAlchemy models or any runtime codegen.
- Per-domain Docker provisioning step.
- Schema-per-project hard isolation — `project_id` filtering until proven insufficient.
- Web UI in v1.
- Tool-calling per turn in v1 (deferred to V2.1).

## Risks worth flagging

1. **Architect interview quality.** A bad interview produces a bad plan. Mitigation: pydantic validation, plan preview before save, easy plan-revision flow in v2.
2. **Session boundary ambiguity.** If the idle-timeout is too short, users get fragmented sessions; if too long, summaries are stale. Mitigation: per-domain default (20 min language, 60 min fitness), user-overrideable in `users.settings`.
3. **Plan staleness.** Without v2 structured extraction, the plan doesn't "know" that Maria already aced cooking verbs unless the summarizer flips the item status. Mitigation: the summarize prompt explicitly asks for plan-item progress; review summaries during early v1 use and tune the prompt.
4. **Summary quality drift over time.** Summaries are the *primary* form of long-term memory in v1. If they degrade, the system degrades. Mitigation: keep all raw messages forever (they're cheap); v2 can re-summarize from the raw transcript.
5. **Domain abstraction leakage.** Phase 5 (fitness) is the explicit stress test. Reserve refactor budget there.

## Suggested first-three-sessions order

1. **Phase 2**: plans + sessions migration + architect. The most novel piece, worth tackling while energy is high.
2. **Phase 3**: specialist v1 loop. Once this works the v1 feedback loop is closed.
3. **Phase 4**: Telegram. By the time this lands you can dogfood from your phone.
