#!/usr/bin/env bash
#
# deploy.sh — provision and start the Nexus Telegram bot on a fresh server.
#
# Idempotent: safe to re-run after a `git pull` to ship updates.
# Assumes Debian/Ubuntu. Run from the repo root:  bash deploy/deploy.sh
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

echo "==> Nexus bot deploy (repo: $REPO_DIR)"

# 1. System deps: docker (for postgres) + uv (for the python app) ------------
if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker"
  curl -fsSL https://get.docker.com | sh
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "==> Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv lands in ~/.local/bin — make it available for the rest of this script
  export PATH="$HOME/.local/bin:$PATH"
fi

# 2. .env must exist and carry the secrets ----------------------------------
if [ ! -f .env ]; then
  echo "==> No .env found — creating from .env.example. EDIT IT before the bot will work:"
  cp .env.example .env
  echo "    Set TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY (or GEMINI_API_KEY), and"
  echo "    keep POSTGRES_URL pointing at the docker postgres below."
  echo "    Then re-run this script."
  exit 1
fi

# 3. Postgres via docker compose --------------------------------------------
echo "==> Bringing up Postgres"
docker compose up -d
echo "==> Waiting for Postgres to be healthy"
until docker inspect --format '{{.State.Health.Status}}' nexus-postgres 2>/dev/null | grep -q healthy; do
  sleep 2
done

# 4. Python deps + DB migrations --------------------------------------------
echo "==> Installing python deps"
uv sync

echo "==> Applying database migrations"
uv run alembic upgrade head

# 5. Start the bot -----------------------------------------------------------
echo
echo "==> Setup complete."
echo "    To run the bot in the foreground (for a quick test):"
echo "        uv run nexus bot"
echo
echo "    For a durable, auto-restarting service, install the systemd unit:"
echo "        bash deploy/install-service.sh"
