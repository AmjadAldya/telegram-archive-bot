# Telegram Media Mirror

A Python/Pyrogram **userbot** that automatically mirrors media (photos,
videos, documents, audio, voice notes, video notes, animations) from a
protected/private source group or channel to another channel or group -
continuously, with duplicate protection and Telegram-flood-safe throttling.

It uses a real Telegram **user account session** (not a Bot API token),
because bots cannot join or read history in most private/protected groups
the way an existing member account can. The process runs entirely
server-side: once it's started, it keeps working whether or not Telegram is
open anywhere, on your phone or otherwise - it's a separate, independent
connection to Telegram, not something running inside the app on your device.

## Features

- Interactive setup: after logging in, list the groups/channels you're a
  member of and pick the source and destination straight from Telegram, no
  chat IDs to look up by hand. Your choice is saved in the database, so it
  survives restarts.
- Real-time mirroring: new media posted in the source chat is copied to the
  destination chat automatically, no commands required.
- One-time resumable backlog sync of the source chat's existing history once
  a source/destination are set, so nothing already posted is missed.
- Duplicate protection: every transferred file's Telegram `file_unique_id` is
  recorded per destination chat, so the same media is never sent twice - even
  across restarts or a full history rescan.
- Anti-ban throttling: a single serialized sender with a randomized delay
  between transfers, automatic `FloodWait`/`SlowmodeWait` backoff, and an
  immediate, safe stop if Telegram restricts the account (`PeerFlood`,
  ban, deactivation).
- Media-only: text, stickers, polls, contacts, locations, and link previews
  are ignored; the set of mirrored media types is configurable.
- Owner-only control commands (`/chats`, `/setsource`, `/setdest`, `/status`,
  `/pause`, `/resume`, `/resync`) usable only by the logged-in account itself.
- Alembic-managed SQLite schema for the mirror config, dedup ledger, and sync
  progress.
- Docker/Compose deployment (with `restart: unless-stopped`) and CI checks
  (tests, lint, format, type check).

## How it works

1. [app/bot/client.py](app/bot/client.py) logs in with a pre-generated
   session string and starts the Pyrogram client.
2. [app/mirror/runtime.py](app/mirror/runtime.py) holds the currently
   configured source/destination pair (loaded from the database) and
   (re)starts the backlog scan whenever it's set or changed.
3. [app/mirror/backlog.py](app/mirror/backlog.py) scans the source chat's
   history once (resumable), transferring any media not already recorded.
4. [app/bot/handlers.py](app/bot/handlers.py) registers a live handler that
   fires on every new media message; [app/mirror/listener.py](app/mirror/listener.py)
   checks it against the currently configured source chat.
5. [app/mirror/transfer.py](app/mirror/transfer.py) is the single choke point
   for every transfer: it checks the dedup ledger, waits for the rate
   limiter, serializes the actual send behind a lock, retries on
   `FloodWait`, and records the result - used by both the live handler and
   the backlog scan.
6. [app/mirror/media.py](app/mirror/media.py) decides whether a message
   carries mirror-worthy media and extracts its stable file identifier.

Media is moved with `copy_message`, which Telegram handles server-side (no
download/re-upload through this process) and which does not add a "Forwarded
from" tag to the destination message.

## Requirements

- Python 3.10+
- A Telegram `API_ID` and `API_HASH` (from https://my.telegram.org)
- A `SESSION_STRING` for a real user account that is already a member of the
  source chat (see below)

## Generating a session string

Run this once, locally (not inside a headless container), to log in
interactively and print a session string:

```bash
pip install -e ".[dev]"
python -m scripts.generate_session
```

It will ask for your phone number, the login code Telegram texts you, and
your two-step-verification password if you have one. Copy the printed
`SESSION_STRING` into your `.env`. Treat it like a password - anyone with it
can act as your account.

## Local setup

1. Copy `.env.example` to `.env` and fill in `API_ID`, `API_HASH`, and
   `SESSION_STRING`. `SOURCE_CHAT_ID`/`DEST_CHAT_ID` are optional - see below.
2. Install dependencies:

```bash
pip install -e ".[dev]"
```

3. Apply the database migration:

```bash
alembic upgrade head
```

4. Run it:

```bash
python -m app.main
```

5. Pick the source and destination from inside Telegram (see next section).

## Picking the source and destination

Send these commands as the logged-in account - the simplest way is from
Saved Messages ("me"), so they don't post visibly in any group:

1. `/chats` - lists the groups/channels you're a member of, with a number
   next to each one. Long lists are paginated (`/chats 2`, `/chats 3`, ...).
2. `/setsource <number>` - picks the source (the protected group/channel to
   read from) using the number from `/chats`.
3. `/setdest <number>` - picks the destination the same way.

You can also skip straight to `/setsource -1001234567890` or
`/setsource @some_channel` if you already know the chat. As soon as both are
set, the backlog sync starts automatically. Run `/setsource`/`/setdest`
again any time to repoint the mirror - the old backlog scan is cancelled and
a new one starts for the new pair.

If you'd rather configure it non-interactively (e.g. for a scripted first
deploy), you can still set `SOURCE_CHAT_ID`/`DEST_CHAT_ID` in `.env`; they're
only used once, to seed the database the first time the bot runs with no
configuration saved yet. After that, `/setsource`/`/setdest` are always the
source of truth and the env vars are ignored.

## Configuration

| Variable | Required | Description |
| --- | --- | --- |
| `API_ID`, `API_HASH` | yes | Telegram app credentials |
| `SESSION_STRING` | yes | User session from `scripts/generate_session.py` |
| `SOURCE_CHAT_ID` / `DEST_CHAT_ID` | no | One-time seed only; prefer `/chats` + `/setsource` + `/setdest` |
| `MIN_DELAY_SECONDS` / `MAX_DELAY_SECONDS` | no | Randomized delay before each transfer (default `3`-`7`) |
| `FLOOD_WAIT_MAX_RETRIES` | no | Max automatic retries on `FloodWait` before giving up on a message (default `5`) |
| `MEDIA_TYPES` | no | Comma-separated subset of `photo,video,document,audio,voice,video_note,animation` |
| `SYNC_HISTORY_ON_START` | no | Run the resumable backlog scan once a pair is configured (default `true`) |
| `DATABASE_URL`, `DATA_DIR`, `LOG_LEVEL` | no | Storage and logging |

## Commands

Sent as the logged-in account, ideally from Saved Messages ("me") to avoid
noise in the source/destination chats:

- `/chats [page]` - list your groups/channels to pick from.
- `/setsource <number|id|@username>` - set the chat to mirror media from.
- `/setdest <number|id|@username>` - set the chat to mirror media into.
- `/status` - shows the configured pair, backlog progress, pause state.
- `/pause` - stops transferring media until `/resume`.
- `/resume` - resumes transferring media.
- `/resync` - resets the backlog cursor and rescans the full source history
  from the newest message (safe: the dedup ledger prevents re-sending
  anything already mirrored).
- `/help` - lists these commands.

## Staying within Telegram's limits

- Only one media transfer is ever in flight at a time, spaced by a randomized
  delay (`MIN_DELAY_SECONDS`-`MAX_DELAY_SECONDS`).
- `FloodWait`/`SlowmodeWait` responses are honored automatically (sleep for
  exactly what Telegram asks, plus a small buffer) and retried up to
  `FLOOD_WAIT_MAX_RETRIES` times.
- If Telegram signals the account is flood-restricted, banned, or
  deactivated, the mirror stops immediately instead of retrying - check
  `/status` for the reason.
- Media is copied server-side (`copy_message`), which is both faster and
  lighter than downloading and re-uploading files.
- If you still see frequent `FloodWait` warnings in the logs, raise
  `MIN_DELAY_SECONDS`/`MAX_DELAY_SECONDS`.

## Keeping it running 24/7

This is a personal, single-account tool, meant to run unattended on a server
or always-on machine you control - not on your phone.

- **Docker (recommended):** `docker compose up -d --build`. The `mirror`
  service has `restart: unless-stopped`, so it survives crashes and reboots
  of the host as long as Docker itself starts on boot.
- **systemd (bare metal/VPS):** see [deploy/telegram-mirror.service](deploy/telegram-mirror.service)
  for a ready-to-adapt unit file (`Restart=always`).
- Closing Telegram on your phone/desktop does not affect this process at
  all - they're independent connections to your account. If the mirror
  itself ever stops (crash, host reboot, out of retries after a flood
  restriction), only the process supervisor (Docker/systemd) restarting it,
  or you starting it manually, brings it back; nothing about the Telegram
  app matters here.

## Docker

```bash
docker compose up --build
```

If you are deploying a fresh database, run migrations first:

```bash
docker compose run --rm mirror alembic upgrade head
```

SQLite data is stored in the `mirror-data` volume. `SESSION_STRING` should be
generated once locally and passed in via `.env` - never generate it inside
the container.

To update to a new version of the code on the server:

```bash
git pull
docker compose up -d --build
```

### Automated deploy via GitHub Actions (manual trigger)

`.github/workflows/deploy.yml` lets you redeploy `main` to the droplet by
clicking **Run workflow** in the GitHub Actions tab - it never runs on push,
only on demand. It SSHes into the droplet and runs `git reset --hard
origin/main`, rebuilds the image, applies any new Alembic migration against
that freshly built image, then starts the container.

Before using it, add these repository secrets under **Settings → Secrets and
variables → Actions → New repository secret**. To collect all five values in
one shot, run [deploy/setup_deploy_secrets.sh](deploy/setup_deploy_secrets.sh)
**on the droplet**, from inside the cloned repo directory:

```bash
bash deploy/setup_deploy_secrets.sh
```

It generates a dedicated SSH deploy key (separate from your personal one),
authorizes it for the current user, and prints all five secret values ready
to paste in. Safe to re-run - it reuses the key if one already exists.

| Secret | Value |
|---|---|
| `DO_HOST` | Droplet IP or domain |
| `DO_USER` | SSH username used for deploys |
| `DO_SSH_KEY` | Private key (PEM) for that user - generate a **dedicated deploy key**, don't reuse your personal key |
| `DO_PORT` | SSH port (usually `22`) |
| `DO_APP_PATH` | Absolute path to the cloned repo on the droplet, e.g. `/home/deploy/telegram-archive-bot` |

Recommended: create a separate low-privilege `deploy` user on the droplet
(only able to `git pull` and run `docker compose` in this one directory)
rather than using `root`'s key. The droplet needs its own `.env` (with the
real `SESSION_STRING`, etc.) already in place at `DO_APP_PATH` - the
workflow only pulls code and rebuilds, it never touches `.env`.

## Deployment notes

- Keep `.env` out of Git; `SESSION_STRING` is as sensitive as a password.
- Run `alembic upgrade head` whenever schema changes are introduced.
- Use `/status` after deployment to confirm the backlog sync and live mirror
  are both running.
- This project is intended for personal use on your own account and chats
  you already belong to. Automating a personal Telegram account carries
  account-level risk independent of this code (Telegram's ToS restricts
  automated behavior on user accounts); the throttling here reduces but
  cannot eliminate that risk.
