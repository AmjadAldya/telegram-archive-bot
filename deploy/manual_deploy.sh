#!/usr/bin/env bash
# One-shot manual deploy: run this ON THE SERVER to pull the latest code
# from `main` and (re)start the bot with Docker Compose. Works whether the
# repo is already cloned there (updates it) or not (clones it fresh).
#
# Usage: paste this whole script into a shell on the server and run it,
# or save it and run: bash manual_deploy.sh
#
# This is the same thing .github/workflows/deploy.yml does automatically
# once its GitHub secrets are set up (see deploy/setup_deploy_secrets.sh) -
# use this script for a first deploy, or any time you'd rather trigger it
# by hand from the server instead of from GitHub Actions.

set -euo pipefail

REPO_URL="https://github.com/AmjadAldya/telegram-archive-bot.git"
DEFAULT_PATH="$HOME/telegram-archive-bot"

read -rp "App directory on this server [$DEFAULT_PATH]: " APP_PATH
APP_PATH="${APP_PATH:-$DEFAULT_PATH}"

if [ -d "$APP_PATH/.git" ]; then
    echo "==> Found an existing clone at $APP_PATH, updating it..."
    cd "$APP_PATH"
    git fetch origin main
    git reset --hard origin/main
else
    echo "==> No clone found at $APP_PATH, cloning fresh..."
    echo "    (If this repo is private and cloning fails, either use an SSH"
    echo "     remote you already have access to, or a personal access token:"
    echo "     git clone https://<token>@github.com/AmjadAldya/telegram-archive-bot.git)"
    git clone "$REPO_URL" "$APP_PATH"
    cd "$APP_PATH"
fi

if [ ! -f .env ]; then
    echo "==> No .env found at $APP_PATH/.env - creating one from .env.example."
    cp .env.example .env
    echo "    Fill in API_ID / API_HASH / SESSION_STRING now:"
    "${EDITOR:-nano}" .env
fi

echo "==> Building the image..."
docker compose build

echo "==> Applying database migrations against the freshly built image..."
docker compose run --rm mirror alembic upgrade head

echo "==> Starting the service..."
docker compose up -d

echo "==> Cleaning up old images..."
docker image prune -f

cat <<EOF

Done. The bot is running at $APP_PATH.

Follow logs:
  cd $APP_PATH && docker compose logs -f

Configure the mirror from Telegram (Saved Messages):
  /chats          -> list your groups/channels
  /setsource <n>  -> pick the source
  /setdest <n>    -> pick the destination
EOF
