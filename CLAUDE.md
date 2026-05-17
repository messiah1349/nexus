# Nexus — Personal Agent Factory

Multi-user platform for siloed personal-assistant projects (language learning, fitness, habits, ...). The user creates one project per domain; each project has its own conversation, memory, and structured data — but all projects share a single fixed Postgres schema.

## Source of truth

Design lives in `docs/`. Read these before making non-trivial changes:

- `docs/schema.md` — fixed schema, domain-config format, v1 vs v2 scope
- `docs/structure.md` — directory layout and module responsibilities
- `docs/plan.md` — phased implementation plan, current phase, what's deferred to v2
- `docs/use_case_language_learning.md` — concrete trace of the v1 interaction model

## Key architectural decisions (do not silently revisit)

1. **No LLM-generated SQLAlchemy models, no per-project Docker provisioning.** Domain variation is a validated `DomainConfig` JSON stored in `projects.config`. The Architect produces config; it does not generate code.
2. **One fixed schema serves every domain.** Core tables: `users`, `projects`, `plans`, `sessions`, `messages`, `summaries`, plus `entities`, `events`, `event_entities`, `embeddings` (these last four exist in the schema but are not written to in v1 — they're reserved for v2 structured extraction).
3. **Plan-driven, session-bounded interactions.** A project has one or more `plans` (yearly / weekly / level-check, statuses: `active` / `completed` / `superseded` / `archived`). User interactions happen inside `sessions`. Each session ends with a single summary saved to `summaries` and, where relevant, a plan-item progress update or a plan revision.
4. **No mid-turn tool calls in v1.** The specialist is a pure-chat agent: load context once at session start, append each turn to the conversation, respond, persist. No `create_entity` / `log_event` / `update_state` tools — those are v2. Messages persist continuously per turn (one `INSERT` each) for crash safety; structured outputs happen only at session end.
5. **Three-tier memory in v1 is shallow on purpose.** Episodic = the current session's messages, plus the last K session summaries. Structural = the active plan's state. Semantic (vector search) is deferred to v2 — for now the agent works off summaries-as-text, not embeddings.
6. **Telegram is the primary client.** CLI exists for development iteration. The client layer is modular — adding a future web frontend should not require touching `specialist/`.
7. **LLM provider is pluggable.** Anthropic is the first implementation. The interface in `nexus/llm/` is provider-agnostic and translates tool-use / chat shapes at the boundary. Don't leak Anthropic-specific shapes into `specialist/` or `architect/`.
8. **Multi-tenant from day one.** Every row carries `project_id`; "siloed" is enforced by `WHERE project_id = ?`, not by separate databases.

## What this project is *not* (in v1)

- Not a tool-calling agent. The specialist doesn't write to `entities` / `events` during a chat.
- Not a single-user toy — multi-user is in the schema and the data path.
- Not Anthropic-locked — the LLM abstraction matters, build accordingly.
- Not codegen-driven — if you find yourself writing a `.py` file generator, stop and re-read `docs/schema.md`.

## Deferred to v2 (in `docs/plan.md`)

- Per-turn structured extraction (entities, events with payloads)
- Mid-session retrieval / semantic search
- Periodic (weekly / monthly) reflective summaries beyond per-session ones
- Domain-specific structural stats (vocab mastery distribution, weekly volume, etc.)
- MCP server exposing project data to external agents

## Initial domains

`language_learning` and `fitness`. Fitness lands as the abstraction stress test — if it doesn't fit cleanly, fix the abstraction before adding domain #3.

## Tooling

- Python 3.12+, `uv` for package management
- SQLAlchemy 2.0 async + Alembic (real migrations, not codegen)
- pgvector for embeddings (table reserved for v2)
- pydantic v2 for config validation
- typer for CLI, python-telegram-bot for Telegram
- pytest + integration tests against compose-managed postgres

## Conventions

- Don't leak SQL outside `nexus/db/repository.py`. Other modules use repository functions.
- Don't leak provider shapes outside `nexus/llm/`. Other modules use the abstract client.
- Soft delete via `archived_at` columns; never hard-delete by default. Plans use `status='superseded'` plus a `superseded_by` FK to track revisions.
- All times in `timestamptz`. Convert to user TZ at presentation only.
- Summaries are scope-typed (`session` in v1; `daily` / `weekly` / `topical` reserved for v2).

## Git workflow (overrides Claude Code default)

This project authorizes Claude to commit and push without asking each time. Specifically:

- **Commit after every coherent change.** When a task in the task list is completed, or any logical unit of work is finished, create a commit. Don't leave uncommitted work sitting between user prompts.
- **Push after significant milestones**, especially phase completions, to `origin` (`git@github.com:messiah1349/nexus.git`). Smaller commits stay local until they accumulate or a phase ends.
- **Standard safety still applies:** no `--no-verify`, no `git add -A` of untracked sensitive files, no force-push to `main`, no amending pushed commits, no destructive operations (`reset --hard`, `clean -f`, branch deletion) without explicit instruction. Prefer adding files by name. Always create new commits rather than amending.
- **Commit message style:** Conventional-Commits–ish prefix (`feat:`, `chore:`, `docs:`, `fix:`, `refactor:`, `test:`) plus a short imperative subject. Body explains the *why*, not the *what*. End with the standard `Co-Authored-By` footer.
- **Branching:** work on `main` for v1 unless explicitly asked otherwise. Feature branches arrive when there's a real reason (parallel work, RFC-style review).
