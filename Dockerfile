# syntax=docker/dockerfile:1

# ---- builder: resolve deps into a self-contained .venv ----------------------
FROM python:3.12-slim AS builder

# uv: fast, reproducible installs from uv.lock. Copy just the static binary.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Layer 1: deps only (cached unless lockfile changes). --no-install-project
# skips the app itself so this layer doesn't bust on every source edit.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Layer 2: the app source + install the project into the venv.
COPY nexus ./nexus
COPY alembic ./alembic
COPY alembic.ini README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- runtime: slim image with just python + the prebuilt venv ---------------
FROM python:3.12-slim AS runtime

# Run as non-root.
RUN useradd --create-home --uid 10001 nexus
WORKDIR /app

# Bring over the venv and app from the builder. No uv, no build tools shipped.
COPY --from=builder --chown=nexus:nexus /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER nexus

# Apply migrations, then start the long-polling bot. alembic upgrade is
# idempotent, so this is safe on every (re)start.
CMD ["sh", "-c", "alembic upgrade head && nexus bot"]
