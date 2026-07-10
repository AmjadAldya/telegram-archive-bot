# Release Readiness

## Ready

- Userbot startup and shutdown are wired through Pyrogram, authenticated via
  `SESSION_STRING` (no bot token, no interactive login at runtime).
- Handlers are actually registered: `app/bot/handlers.py` is imported before
  `bot.start()` (this was previously a silent no-op bug in the bot-token
  scaffold, fixed as part of this rewrite).
- Live media handler mirrors new source-chat media to the destination chat
  automatically, no command needed.
- Resumable backlog sync mirrors the source chat's existing history once,
  persisting a cursor so a restart continues instead of rescanning.
- Duplicate media (same Telegram file, same destination) is never
  re-transferred, enforced at the database level via a unique constraint -
  correct even under restarts, concurrent processing, or a forced `/resync`.
- Every transfer is serialized behind a single lock, rate-limited with a
  randomized delay, and retried automatically on `FloodWait`/`SlowmodeWait`.
- `PeerFlood`/ban/deactivation errors stop the mirror immediately instead of
  retrying, and are surfaced through `/status`.
- Owner-only control commands (`/status`, `/pause`, `/resume`, `/resync`,
  `/help`) are gated by `filters.me`.
- Alembic-managed SQLite schema for the dedup ledger and sync state.
- Docker and Compose use a persistent data volume.
- Tests, linting, formatting, and type checking pass.

## Before your first real run

1. Run `python -m scripts.generate_session` locally and put the resulting
   `SESSION_STRING` in `.env` - never generate it inside a container or CI.
2. Make sure the account is already a member of `SOURCE_CHAT_ID` and can post
   to `DEST_CHAT_ID`.
3. Start with the default throttle (`MIN_DELAY_SECONDS=3`,
   `MAX_DELAY_SECONDS=7`) and only lower it if you understand the flood-ban
   risk; raise it if you see `FloodWait` warnings in the logs.
4. Automating a personal Telegram account carries account-level risk
   independent of this code (Telegram's ToS restricts automated behavior on
   user accounts). This project throttles and backs off defensively, but it
   cannot eliminate that risk.

## Optional Next Steps

- Add a metrics or health endpoint if deployment grows.
- Add multi-source/multi-destination routing if one pair stops being enough.
- Add richer `/status` output (e.g. transfer rate, last error timestamp).

## Deploy

1. Set `API_ID`, `API_HASH`, `SESSION_STRING`, `SOURCE_CHAT_ID`, `DEST_CHAT_ID` in `.env`.
2. Run `alembic upgrade head`.
3. Run `docker compose up --build` or `python -m app.main`.
4. Send `/status` (from Saved Messages) to confirm the backlog sync and live
   mirror are both running.
