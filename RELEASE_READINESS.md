# Release Readiness

## Ready

- Userbot startup and shutdown are wired through Pyrogram, authenticated via
  `SESSION_STRING` (no bot token, no interactive login at runtime).
- Handlers are actually registered: `app/bot/handlers.py` is imported before
  `bot.start()` (this was previously a silent no-op bug in the bot-token
  scaffold, fixed as part of this rewrite).
- Interactive source/destination selection: `/chats` lists the account's
  groups/channels, `/setsource`/`/setdest` pick from that list (or a raw
  id/`@username`), and the choice is persisted in the database - no chat IDs
  to look up by hand, and it survives restarts.
- Live media handler mirrors new source-chat media to the destination chat
  automatically, no command needed.
- Resumable backlog sync mirrors the source chat's existing history once a
  pair is configured, persisting a cursor so a restart continues instead of
  rescanning; changing the pair cancels the old scan and starts a fresh one.
- Duplicate media (same Telegram file, same destination) is never
  re-transferred, enforced at the database level via a unique constraint -
  correct even under restarts, concurrent processing, or a forced `/resync`.
- Every transfer is serialized behind a single lock, rate-limited with a
  randomized delay, and retried automatically on `FloodWait`/`SlowmodeWait`.
- `PeerFlood`/ban/deactivation errors stop the mirror immediately instead of
  retrying, and are surfaced through `/status`.
- Owner-only control commands (`/chats`, `/setsource`, `/setdest`, `/status`,
  `/pause`, `/resume`, `/resync`, `/help`) are gated by `filters.me`.
- Alembic-managed SQLite schema for the mirror config, dedup ledger, and sync
  state.
- Runs unattended, independent of any Telegram client app: Docker Compose
  uses `restart: unless-stopped` and a persistent data volume; an example
  systemd unit (`Restart=always`) is included for bare-metal/VPS deployment.
- Tests, linting, formatting, and type checking pass.

## Before your first real run

1. Run `python -m scripts.generate_session` locally and put the resulting
   `SESSION_STRING` in `.env` - never generate it inside a container or CI.
2. Start the bot, then from Saved Messages send `/chats`, then `/setsource
   <n>` and `/setdest <n>` to pick your source and destination.
3. Start with the default throttle (`MIN_DELAY_SECONDS=3`,
   `MAX_DELAY_SECONDS=7`) and only lower it if you understand the flood-ban
   risk; raise it if you see `FloodWait` warnings in the logs.
4. Automating a personal Telegram account carries account-level risk
   independent of this code (Telegram's ToS restricts automated behavior on
   user accounts). This project throttles and backs off defensively, but it
   cannot eliminate that risk. It's built for personal use on your own
   account and chats you already belong to, not for operating other
   people's accounts.

## Optional Next Steps

- Add a metrics or health endpoint if deployment grows.
- Add multi-source/multi-destination routing if one pair stops being enough.
- Add richer `/status` output (e.g. transfer rate, last error timestamp).

## Deploy

1. Set `API_ID`, `API_HASH`, `SESSION_STRING` in `.env`.
2. Run `alembic upgrade head`.
3. Run `docker compose up -d --build` (or set up the systemd unit, or run
   `python -m app.main` directly for a quick test).
4. From Saved Messages, send `/chats`, `/setsource <n>`, `/setdest <n>`.
5. Send `/status` to confirm the backlog sync and live mirror are both
   running.
