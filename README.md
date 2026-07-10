# Telegram Media Mirror

A Python/Pyrogram **userbot** that automatically mirrors media (photos,
videos, documents, audio, voice notes, video notes, animations) from a
protected/private source group or channel to another channel or group -
continuously, with duplicate protection and Telegram-flood-safe throttling.

It uses a real Telegram **user account session** (not a Bot API token),
because bots cannot join or read history in most private/protected groups
the way an existing member account can.

## Features

- Real-time mirroring: new media posted in the source chat is copied to the
  destination chat automatically, no commands required.
- One-time resumable backlog sync of the source chat's existing history on
  startup, so nothing already posted is missed.
- Duplicate protection: every transferred file's Telegram `file_unique_id` is
  recorded per destination chat, so the same media is never sent twice - even
  across restarts or a full history rescan.
- Anti-ban throttling: a single serialized sender with a randomized delay
  between transfers, automatic `FloodWait`/`SlowmodeWait` backoff, and an
  immediate, safe stop if Telegram restricts the account (`PeerFlood`,
  ban, deactivation).
- Media-only: text, stickers, polls, contacts, locations, and link previews
  are ignored; the set of mirrored media types is configurable.
- Owner-only control commands (`/status`, `/pause`, `/resume`, `/resync`)
  usable only by the logged-in account itself.
- Alembic-managed SQLite schema for the dedup ledger and sync progress.
- Docker/Compose deployment and CI checks (tests, lint, format, type check).

## How it works

1. [app/bot/client.py](app/bot/client.py) logs in with a pre-generated
   session string and starts the Pyrogram client.
2. [app/mirror/backlog.py](app/mirror/backlog.py) scans the source chat's
   history once (resumable), transferring any media not already recorded.
3. [app/bot/handlers.py](app/bot/handlers.py) registers a live handler that
   fires on every new message in the source chat.
4. [app/mirror/transfer.py](app/mirror/transfer.py) is the single choke point
   for every transfer: it checks the dedup ledger, waits for the rate
   limiter, serializes the actual send behind a lock, retries on
   `FloodWait`, and records the result - used by both the live handler and
   the backlog scan.
5. [app/mirror/media.py](app/mirror/media.py) decides whether a message
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

1. Copy `.env.example` to `.env` and fill in `API_ID`, `API_HASH`,
   `SESSION_STRING`, `SOURCE_CHAT_ID`, and `DEST_CHAT_ID`.
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

## Configuration

| Variable | Required | Description |
| --- | --- | --- |
| `API_ID`, `API_HASH` | yes | Telegram app credentials |
| `SESSION_STRING` | yes | User session from `scripts/generate_session.py` |
| `SOURCE_CHAT_ID` | yes | Protected group/channel to read from (ID or `@username`) |
| `DEST_CHAT_ID` | yes | Group/channel to mirror media into |
| `MIN_DELAY_SECONDS` / `MAX_DELAY_SECONDS` | no | Randomized delay before each transfer (default `3`-`7`) |
| `FLOOD_WAIT_MAX_RETRIES` | no | Max automatic retries on `FloodWait` before giving up on a message (default `5`) |
| `MEDIA_TYPES` | no | Comma-separated subset of `photo,video,document,audio,voice,video_note,animation` |
| `SYNC_HISTORY_ON_START` | no | Run the resumable backlog scan on startup (default `true`) |
| `DATABASE_URL`, `DATA_DIR`, `LOG_LEVEL` | no | Storage and logging |

## Commands

Sent as the logged-in account, ideally from Saved Messages ("me") to avoid
noise in the source/destination chats:

- `/status` - shows mirror configuration, backlog progress, pause state.
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

## Deployment notes

- Keep `.env` out of Git; `SESSION_STRING` is as sensitive as a password.
- Run `alembic upgrade head` whenever schema changes are introduced.
- Use `/status` after deployment to confirm the backlog sync and live mirror
  are both running.
