#!/usr/bin/env bash
# For when the bot is already running on this server somewhere but you
# don't remember the path: finds it, stops it, pulls the latest `main`,
# rebuilds, migrates, and starts it again.
#
# Paste this whole script into a shell on the server and run it.

set -uo pipefail

REPO_URL="https://github.com/AmjadAldya/telegram-archive-bot.git"
APP_PATH=""

echo "==> Looking for a running/stopped container that looks like this bot..."
CANDIDATES=$(docker ps -a --format '{{.ID}} {{.Names}} {{.Image}}' 2>/dev/null \
    | grep -iE 'mirror|archive|telegram' || true)

if [ -n "$CANDIDATES" ]; then
    echo "Found:"
    echo "$CANDIDATES"
    echo ""
    while IFS= read -r line; do
        CID=$(echo "$line" | awk '{print $1}')
        WORKDIR=$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$CID" 2>/dev/null || true)
        if [ -n "$WORKDIR" ] && [ "$WORKDIR" != "<no value>" ] && [ -d "$WORKDIR" ]; then
            APP_PATH="$WORKDIR"
            echo "==> Using compose project directory from container $CID: $APP_PATH"
            break
        fi
    done <<<"$CANDIDATES"
fi

if [ -z "$APP_PATH" ]; then
    echo "==> No running container pointed to a directory. Searching common locations..."
    for base in "$HOME" /root /opt /srv /home/*; do
        [ -d "$base" ] || continue
        FOUND=$(find "$base" -maxdepth 4 -iname "docker-compose.yml" 2>/dev/null \
            | xargs -r grep -l "mirror-data\|telegram-mirror\|telegram-archive-bot" 2>/dev/null \
            | head -1)
        if [ -n "$FOUND" ]; then
            APP_PATH=$(dirname "$FOUND")
            echo "==> Found it at: $APP_PATH"
            break
        fi
    done
fi

if [ -z "$APP_PATH" ]; then
    echo "==> Still couldn't find it automatically."
    read -rp "Enter the app directory manually (or leave empty to clone fresh): " APP_PATH
fi

if [ -n "$APP_PATH" ] && [ -d "$APP_PATH/.git" ]; then
    echo "==> Stopping the current deployment at $APP_PATH..."
    (cd "$APP_PATH" && docker compose down) || echo "    (nothing was running, continuing)"

    echo "==> Pulling the latest main..."
    cd "$APP_PATH"
    git fetch origin main
    git reset --hard origin/main
else
    APP_PATH="${APP_PATH:-$HOME/telegram-archive-bot}"
    echo "==> Cloning fresh into $APP_PATH..."
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
