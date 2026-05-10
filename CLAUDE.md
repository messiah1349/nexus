# Nexus — Personal Agent Factory

Multi-user platform for siloed personal-assistant projects (language learning, fitness, habits, ...). The user creates one project per domain; each project has its own conversation, memory, and structured data — but all projects share a single fixed Postgres schema.

## Source of truth

Design lives in `docs/`. Read these before making non-trivial changes:

- `docs/schema.md` — fixed schema, domain-config format, worked examples
- `docs/structure.md` — directory layout and module responsibilities
- `docs/plan.md` — phased implementation plan, current phase, what's deferred

## Key architectural decisions (do not silently revisit)

1. **No LLM-generated SQLAlchemy models, no per-project Docker provisioning.** Domain variation is a validated `DomainConfig` JSON stored in `projects.config`. The Architect produces config; it does not generate code.
2. **One fixed schema serves every domain.** Five core tables: `entities`, `events`, `messages`, `summaries`, `embeddings`, plus `users`, `projects`, `event_entities`. Per-domain shape lives in JSONB `attributes`/`state`/`payload` columns whose structure is described by the project's `DomainConfig`.
3. **Three-tier memory**: episodic (recent messages + events), semantic (pgvector over embeddings), structural (typed aggregations declared in `DomainConfig`).
4. **Telegram is the primary client.** CLI exists for development iteration, not as a separate product. The client layer is modular — adding a future web frontend should not require touching `specialist/`.
5. **LLM provider is pluggable.** Anthropic is the first implementation. The interface in `nexus/llm/` is provider-agnostic and translates tool-use schemas at the boundary. Don't leak Anthropic-specific shapes into `specialist/` or `architect/`.
6. **Multi-tenant from day one.** Every row carries `project_id`; "siloed" is enforced by `WHERE project_id = ?`, not by separate databases.

## What this project is *not*

- Not a single-user toy — multi-user is in the schema and the data path.
- Not Anthropic-locked — the LLM abstraction matters, build accordingly.
- Not codegen-driven — if you find yourself writing a `.py` file generator, stop and re-read `docs/schema.md`.

## Initial domains

`language_learning` and `fitness`. Fitness lands in Phase 7 specifically as a stress test of the abstraction — if it doesn't fit cleanly, fix the abstraction before adding domain #3.

## Tooling

- Python 3.12+, `uv` for package management
- SQLAlchemy 2.0 async + Alembic (real migrations, not codegen)
- pgvector for embeddings
- pydantic v2 for config + tool schemas
- typer for CLI, python-telegram-bot for Telegram
- pytest + integration tests against compose-managed postgres

## Conventions

- Don't leak SQL outside `nexus/db/repository.py`. Other modules use repository functions.
- Don't leak provider shapes outside `nexus/llm/`. Other modules use the abstract client.
- Soft delete via `archived_at` columns; never hard-delete by default.
- All times in `timestamptz`. Convert to user TZ at presentation only.

## Git workflow (overrides Claude Code default)

This project authorizes Claude to commit and push without asking each time. Specifically:

- **Commit after every coherent change.** When a task in the task list is completed, or any logical unit of work is finished, create a commit. Don't leave uncommitted work sitting between user prompts.
- **Push after significant milestones**, especially phase completions, to `origin` (`git@github.com:messiah1349/nexus.git`). Smaller commits stay local until they accumulate or a phase ends.
- **Standard safety still applies:** no `--no-verify`, no `git add -A` of untracked sensitive files, no force-push to `main`, no amending pushed commits, no destructive operations (`reset --hard`, `clean -f`, branch deletion) without explicit instruction. Prefer adding files by name. Always create new commits rather than amending.
- **Commit message style:** Conventional-Commits–ish prefix (`feat:`, `chore:`, `docs:`, `fix:`, `refactor:`, `test:`) plus a short imperative subject. Body explains the *why*, not the *what*. End with the standard `Co-Authored-By` footer.
- **Branching:** work on `main` for Phase 0–9 unless explicitly asked otherwise. Feature branches arrive when there's a real reason (parallel work, RFC-style review).
