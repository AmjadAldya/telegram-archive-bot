#!/usr/bin/env bash
# First deploy on a fresh server, for a PRIVATE repo: sets up a dedicated,
# read-only SSH deploy key for pulling from GitHub (separate from any key
# used to log into this server), then clones and deploys.
#
# Paste this whole script into a shell on the server and run it. It will
# pause once, part-way through, so you can add the printed public key as a
# Deploy Key on GitHub before it continues.

set -uo pipefail

REPO_SSH_URL="git@github-telegram-archive-bot:AmjadAldya/telegram-archive-bot.git"
REPO_SETTINGS_URL="https://github.com/AmjadAldya/telegram-archive-bot/settings/keys"
DEFAULT_PATH="$HOME/telegram-archive-bot"
KEY_PATH="$HOME/.ssh/telegram-archive-bot-deploy-key"

echo "==> Setting up a dedicated SSH deploy key for GitHub..."
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"

if [ -f "$KEY_PATH" ]; then
    echo "    Key already exists at $KEY_PATH, reusing it."
else
    ssh-keygen -t ed25519 -f "$KEY_PATH" -N "" -C "telegram-archive-bot-deploy" -q
    echo "    Generated new key at $KEY_PATH"
fi

SSH_CONFIG="$HOME/.ssh/config"
touch "$SSH_CONFIG"
chmod 600 "$SSH_CONFIG"
if ! grep -q "^Host github-telegram-archive-bot$" "$SSH_CONFIG" 2>/dev/null; then
    cat >>"$SSH_CONFIG" <<EOF

Host github-telegram-archive-bot
    HostName github.com
    User git
    IdentityFile $KEY_PATH
    IdentitiesOnly yes
EOF
    echo "    Added an SSH config entry (github-telegram-archive-bot) so this key"
    echo "    is only ever used for this one repo, not any other GitHub access."
else
    echo "    SSH config entry already present."
fi

cat <<EOF

================================================================
 Add this as a Deploy Key on GitHub (read-only is enough - do
 NOT check "Allow write access"):

   $REPO_SETTINGS_URL  ->  Add deploy key

 Public key:
----------------------------------------------------------------
$(cat "${KEY_PATH}.pub")
----------------------------------------------------------------
================================================================

EOF
read -rp "Press Enter once you've added it on GitHub to continue... " _

echo "==> Testing the connection..."
ssh -o StrictHostKeyChecking=accept-new -T git@github-telegram-archive-bot 2>&1 | sed 's/^/    /' || true
echo "    (\"successfully authenticated\" above is good - GitHub always refuses a shell, that's expected)"

read -rp "App directory on this server [$DEFAULT_PATH]: " APP_PATH
APP_PATH="${APP_PATH:-$DEFAULT_PATH}"

if [ -d "$APP_PATH/.git" ]; then
    echo "==> Existing clone found at $APP_PATH - pointing it at the SSH deploy key and updating..."
    cd "$APP_PATH"
    git remote set-url origin "$REPO_SSH_URL"
    git fetch origin main
    git reset --hard origin/main
else
    echo "==> Cloning into $APP_PATH..."
    git clone "$REPO_SSH_URL" "$APP_PATH"
    cd "$APP_PATH"
fi

if [ ! -f "$APP_PATH/docker-compose.yml" ]; then
    echo ""
    echo "ERROR: $APP_PATH/docker-compose.yml still doesn't exist - the clone did not"
    echo "succeed. Scroll up for the actual git/ssh error and fix that before rerunning"
    echo "this script (nothing below this point ran)."
    exit 1
fi

cd "$APP_PATH"

if [ ! -f .env ]; then
    echo "==> No .env found - creating one from .env.example."
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
