#!/usr/bin/env bash
# Run this ON THE SERVER (the droplet), from inside the cloned repo
# directory, to generate everything .github/workflows/deploy.yml needs:
# a dedicated SSH deploy key, plus the DO_HOST/DO_USER/DO_PORT/DO_APP_PATH
# values. It prints all five GitHub Actions secret values at the end -
# copy each one into Settings -> Secrets and variables -> Actions.
#
# Usage:
#   cd /path/to/telegram-archive-bot   # where you cloned the repo on the server
#   bash deploy/setup_deploy_secrets.sh
#
# Safe to re-run: it reuses an existing deploy key instead of replacing it.

set -euo pipefail

APP_PATH="${1:-$(pwd)}"
KEY_NAME="github-deploy-telegram-archive-bot"
KEY_PATH="$HOME/.ssh/${KEY_NAME}"

echo "==> Checking the app directory ($APP_PATH)..."
if [ ! -d "$APP_PATH/.git" ]; then
    echo "    Warning: $APP_PATH doesn't look like a git clone (no .git directory)." >&2
    echo "    Run this script from inside the cloned repo, or pass the path as an argument." >&2
fi
if [ ! -f "$APP_PATH/.env" ]; then
    echo "    Warning: $APP_PATH/.env not found. The deploy workflow never creates it -" >&2
    echo "    copy .env.example to .env and fill it in before the first deploy." >&2
fi
if ! command -v docker >/dev/null 2>&1; then
    echo "    Warning: 'docker' was not found on PATH. Install Docker + the compose plugin" >&2
    echo "    before running the deploy workflow." >&2
elif ! docker compose version >/dev/null 2>&1; then
    echo "    Warning: 'docker compose' (the plugin, v2) was not found. The workflow needs it." >&2
fi

echo "==> Preparing a dedicated SSH deploy key (separate from your personal key)..."
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"

if [ -f "$KEY_PATH" ]; then
    echo "    Key already exists at $KEY_PATH, reusing it."
else
    ssh-keygen -t ed25519 -f "$KEY_PATH" -N "" -C "github-actions-deploy" -q
    echo "    Generated new key at $KEY_PATH"
fi

touch "$HOME/.ssh/authorized_keys"
chmod 600 "$HOME/.ssh/authorized_keys"
if ! grep -qF "$(cat "${KEY_PATH}.pub")" "$HOME/.ssh/authorized_keys" 2>/dev/null; then
    cat "${KEY_PATH}.pub" >>"$HOME/.ssh/authorized_keys"
    echo "    Authorized it for SSH login as $(whoami)."
else
    echo "    Already authorized for SSH login as $(whoami)."
fi

echo "==> Detecting server connection details..."
DO_HOST=$(curl -fs -4 --max-time 5 https://ifconfig.me 2>/dev/null \
    || curl -fs -4 --max-time 5 https://icanhazip.com 2>/dev/null \
    || hostname -I 2>/dev/null | awk '{print $1}')
DO_USER=$(whoami)
DO_PORT=$(grep -iE '^[[:space:]]*Port[[:space:]]+[0-9]+' /etc/ssh/sshd_config 2>/dev/null \
    | awk '{print $2}' | tail -1)
DO_PORT="${DO_PORT:-22}"

if [ "$DO_USER" = "root" ]; then
    echo "    Note: running as root. The README recommends a separate low-privilege" >&2
    echo "    'deploy' user limited to this directory instead - optional, but safer." >&2
fi

cat <<EOF

================================================================
 GitHub repository secrets
 (Settings -> Secrets and variables -> Actions -> New repository secret)
================================================================

DO_HOST
$DO_HOST

DO_USER
$DO_USER

DO_PORT
$DO_PORT

DO_APP_PATH
$APP_PATH

DO_SSH_KEY   <- paste the ENTIRE block below, including the BEGIN/END lines
----------------------------------------------------------------
$(cat "$KEY_PATH")
----------------------------------------------------------------

This output includes a private key (DO_SSH_KEY) that grants SSH access to
this server as '$DO_USER'. Paste each value into its matching secret, then
clear your terminal scrollback / close this session.
EOF
