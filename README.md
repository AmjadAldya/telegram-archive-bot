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

- **Multiple sources, one destination.** Mirror media from any number of
  groups/channels into a single archive channel. The same file arriving from
  two sources is archived once (dedup is per destination + Telegram
  `file_unique_id`).
- **Arabic admin panel** in a chat with your own control bot: `/panel` with a
  live status summary and inline buttons, `/help` grouped by topic, a command
  menu shown only in your chat, confirmations before anything destructive.
- **Paste a `t.me` link, get the media archived** - no command needed. Works
  for any chat the account can read, whether or not it is a configured source.
- **Live monitor** (`/live`): one message that edits itself with progress, speed,
  ETA and the queue, with Skip / Pause / Refresh buttons.
- **Skip** the current file cleanly at any time; skipped files are listed in
  `/skipped` and can be retried. Large files trigger a notice with a Skip
  button (and an optional auto-skip limit).
- Real-time mirroring plus a resumable backlog scan per source.
- Reliability: per-file timeouts that scale with file size, stall detection,
  automatic retries, FloodWait handling, and an immediate safe stop if Telegram
  restricts the account.
- Media-only: text, stickers, polls, contacts and link previews are ignored;
  the mirrored media types are configurable.
- Owner-only: every command and button is ignored (silently) for anyone but
  the account behind `SESSION_STRING`.
- Alembic-managed SQLite schema, Docker/Compose deployment, CI checks.

## How it works

1. [app/bot/client.py](app/bot/client.py) logs in with a pre-generated session
   string (the "userbot"), starts the control bot if `BOT_TOKEN` is set, and
   wires everything together.
2. [app/bot/handlers.py](app/bot/handlers.py) registers the owner-only commands
   and buttons; [panel.py](app/bot/panel.py), [live.py](app/bot/live.py) and
   [linkflow.py](app/bot/linkflow.py) build the screens, the live monitor and the
   link queue. The control bot cannot read source chats - every read goes
   through the userbot.
3. [app/mirror/runtime.py](app/mirror/runtime.py) holds the destination and the
   sources and runs one backlog loop over the enabled sources, one after another,
   each from its own cursor in `sync_state`.
4. [app/mirror/listener.py](app/mirror/listener.py) routes live media from any
   enabled source.
5. [app/mirror/transfer.py](app/mirror/transfer.py) is the single choke point for
   every transfer (live, backlog, pasted link): dedup, rate limit, one-at-a-time
   lock, FloodWait retry, per-file timeout ([guard.py](app/mirror/guard.py)), skip,
   ledger write. [jobs.py](app/mirror/jobs.py) tracks the jobs for the monitor.

Media is **downloaded by the userbot and uploaded again** (not `copy_message`),
which is what makes protected/private chats work. Temp files live under
`DATA_DIR/tmp` and are always removed (and swept at startup). Design notes:
[docs/ADMIN_V2.md](docs/ADMIN_V2.md).

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

## Controlling the mirror from a real bot (recommended)

By default, control commands go through the userbot's own Saved Messages -
simple, but it mixes bot replies into your personal notes and cannot show
inline buttons. For a cleaner, more professional setup, create a real
Telegram bot to act as a dedicated control panel:

1. Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`,
   and follow the prompts (any name/username you like).
2. Copy the token it gives you into `.env` as `BOT_TOKEN=123456:...`.
3. Open the bot and press **Start**, then start (or restart) the mirror. It
   registers an Arabic "/" command menu scoped to your chat.

Only the Telegram account behind `SESSION_STRING` can use these commands,
no matter who else finds or messages the bot - every handler is filtered to
that one user ID. Leave `BOT_TOKEN` unset to keep using Saved Messages
instead; nothing else changes.

## Local setup

1. Copy `.env.example` to `.env` and fill in `API_ID`, `API_HASH`, and
   `SESSION_STRING`. `BOT_TOKEN`, `SOURCE_CHAT_ID`, and `DEST_CHAT_ID` are
   optional - see above and below.
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

## Setting up the destination and sources

Send these to your control bot (or from the account's own Saved Messages if you
did not set `BOT_TOKEN`):

1. `/setdest` - tap the channel to archive into. Only chats where the account is
   owner or admin are offered. Changing an existing destination asks for
   confirmation (the dedup ledger is per destination).
2. `/addsource` - tap a group/channel, or send `/addsource <number | -100... id |
   @username | invite link>`. The account must be able to read the chat; for an
   invite link it asks before joining. `/sources` lists them; `/removesource`
   deletes one (confirmation first; archived files and dedup history are kept).

If `SYNC_HISTORY_ON_START=true` the backlog of each new source is scanned
automatically; with `false` only new media is mirrored and `/resync [source]`
starts a history scan on demand. `SOURCE_CHAT_ID`/`DEST_CHAT_ID` in `.env` only
seed an *empty* database once.

## Configuration

| Variable | Required | Description |
| --- | --- | --- |
| `API_ID`, `API_HASH` | yes | Telegram app credentials |
| `SESSION_STRING` | yes | User session from `scripts/generate_session.py` |
| `BOT_TOKEN` | no | From @BotFather; gives the panel its own chat + command menu |
| `SOURCE_CHAT_ID` / `DEST_CHAT_ID` | no | One-time seed for an empty DB only |
| `MIN_DELAY_SECONDS` / `MAX_DELAY_SECONDS` | no | Randomized delay before each transfer (default `3`-`7`) |
| `FLOOD_WAIT_MAX_RETRIES` | no | Automatic retries on `FloodWait` (default `5`) |
| `MEDIA_TYPES` | no | Subset of `photo,video,document,audio,voice,video_note,animation` (applies to live/backlog; pasted links accept every type) |
| `SYNC_HISTORY_ON_START` | no | Scan source history automatically (default `true`) |
| `LARGE_FILE_MB` | no | Notice + Skip button for files at/above this size (default `500`; `0` = off; changeable with `/limit`) |
| `TRANSFER_TIMEOUT_BASE_SECONDS` | no | Base time allowed per download/upload stage (default `120`); the limit grows with file size |
| `TRANSFER_MIN_SPEED_KBPS` | no | Pessimistic speed used to scale the timeout by size (default `128`) |
| `TRANSFER_STALL_SECONDS` | no | Give up on a transfer that moved no bytes for this long (default `120`) |
| `TRANSFER_MAX_RETRIES` | no | Retries after a timeout/stall before skipping the file (default `2`) |
| `PROGRESS_LOG_SECONDS` | no | How often progress is logged for long transfers (default `30`) |
| `LIVE_REFRESH_SECONDS` | no | `/live` refresh interval (default `3`, minimum `1`) |
| `DATABASE_URL`, `DATA_DIR`, `LOG_LEVEL` | no | Storage and logging |

## Commands

Everything below is owner-only and answers in Arabic. Names are English so they
stay tappable; the menu in Telegram shows short Arabic descriptions.

**Sources** - `/addsource` (alias `/setsource`), `/sources`, `/removesource`, `/chats`  
**Destination** - `/setdest`  
**Sync** - `/resync [source]`, `/pause`, `/resume`  
**Transfer** - paste any `t.me` link (or `/convert <link>`): `t.me/c/<id>/<msg>`,
`t.me/c/<id>/<topic>/<msg>`, `t.me/<username>/<msg>`, with or without
`?single` / `?comment=`; several links in one message are processed in order.
Only the linked message is transferred; an already-archived file offers a
"transfer anyway" button.  
**Monitor** - `/live`, `/queue`, `/skip`, `/skipped`, `/limit [MB]` (`/limit auto <MB>`
for automatic skipping above a size)  
**Status** - `/panel` (buttons), `/status` (text), `/help`

Without `BOT_TOKEN` the same commands work from Saved Messages only (inline
buttons need the control bot).

## Staying within Telegram's limits

- Only one media transfer is ever in flight, spaced by a randomized delay.
- `FloodWait`/`SlowmodeWait` are honored automatically and retried; the wait is
  not counted against the per-file timeout.
- A transfer that times out or stalls is retried (default twice) and then
  skipped, so one bad file never blocks the queue; see `/skipped`.
- If Telegram flags the account as restricted/banned, the mirror stops
  immediately - check `/status` for the reason.
- If you still see frequent `FloodWait` warnings, raise
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

### Upgrading safely (database backup and rollback)

The schema migration runs automatically at startup (and is idempotent), so back
up the database **before** starting a new version, from the same volume the
service uses:

```bash
docker compose stop mirror
mkdir -p backup
docker compose run --rm --no-deps -v "$PWD/backup:/backup" mirror python -c \
  "import sqlite3; s=sqlite3.connect('data/telegram_mirror.db'); d=sqlite3.connect('/backup/telegram_mirror.pre-upgrade.db'); s.backup(d); d.close(); print('backup ok')"
docker tag "$(docker compose images -q mirror)" telegram-archive-bot-mirror:rollback
```

**Rolling back:** an older image refuses to start against a newer schema
(`Can't locate revision ...`), so first downgrade with the *new* image, then
start the old one:

```bash
docker compose run --rm mirror alembic downgrade 0002_mirror_config
docker tag telegram-archive-bot-mirror:rollback telegram-archive-bot-mirror:latest
docker compose up -d --no-build mirror
```

(or restore `backup/telegram_mirror.pre-upgrade.db` into the volume - that loses
anything archived after the upgrade). Sources beyond the first exist only in the
new schema and are dropped by a downgrade.

### First deploy on a fresh server (private repo)

This repository is private, so a plain `git clone https://...` on a new
server fails with `Authentication failed` (GitHub dropped password auth for
git operations years ago). [deploy/bootstrap_deploy.sh](deploy/bootstrap_deploy.sh)
sets up a dedicated, **read-only** SSH deploy key just for pulling this repo
(separate from any key used to log into the server itself), walks you
through adding it as a Deploy Key on GitHub, then clones and deploys. Use
this the first time you deploy to a new server; after that,
`manual_deploy.sh`/`find_and_redeploy.sh` below reuse the same key.

### First deploy / manual redeploy

[deploy/manual_deploy.sh](deploy/manual_deploy.sh) does a full deploy in one
step - clones the repo if it isn't there yet (or pulls `main` if it is),
creates `.env` from the template on first run, builds the image, applies
Alembic migrations, and starts the service. Paste it into a shell on the
server and run it, or save it and run `bash manual_deploy.sh`. This is the
same thing the GitHub Actions workflow below does automatically - use this
script for the first deploy, or any time you'd rather trigger it by hand.

Don't remember where a previous deployment lives on the server?
[deploy/find_and_redeploy.sh](deploy/find_and_redeploy.sh) looks for a
running/stopped container that matches this project, reads its Docker
Compose project directory straight from the container so you don't need to
know the path, stops it, then does the same pull/build/migrate/start steps
as `manual_deploy.sh`. Falls back to searching common directories, then to
asking for the path manually, if nothing matches.

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
