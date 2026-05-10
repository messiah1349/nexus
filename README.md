# Nexus

Multi-user personal agent factory. One Postgres schema, many siloed projects (language learning, fitness, ...), each driven by a `DomainConfig` rather than generated code.

See `docs/` for design (`schema.md`, `structure.md`, `plan.md`) and `CLAUDE.md` for architectural invariants.

## Quick start (Phase 0)

```bash
# 1. Install deps
uv sync

# 2. Bring up postgres
docker compose up -d

# 3. Smoke test
uv run nexus hello
uv run pytest
```

## Status

Phase 0 (bootstrap). See `docs/plan.md` for the phase ladder.
