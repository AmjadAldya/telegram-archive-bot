# AI Project Context

## Current State
- Project: telegram-archive-bot (media mirror userbot)
- Type: Python Telegram **userbot** built with Pyrogram (user session, not a Bot API token)
- Status: runnable service that mirrors media automatically from an interactively-chosen source chat to an interactively-chosen destination chat
- Scope: interactive source/destination selection, live media mirroring, resumable backlog sync, dedup ledger, anti-flood throttling, owner-only control commands, SQLite persistence via Alembic, Docker/systemd deployment, CI checks

## Why a userbot instead of a bot
A Bot API token cannot join or read history in most private/protected groups
the way an existing member account can. The account used here must already
be a member of the source chat. Authentication is via a pre-generated
Pyrogram `SESSION_STRING` (see [scripts/generate_session.py](scripts/generate_session.py)),
not `BOT_TOKEN`. The process runs server-side and independently of any
Telegram client app - closing Telegram on a phone/desktop does not affect it.

## What Is Implemented
- Startup flow in [app/main.py](app/main.py) and [app/bot/client.py](app/bot/client.py)
- Owner-only control commands in [app/bot/handlers.py](app/bot/handlers.py) (`/chats`, `/setsource`, `/setdest`, `/status`, `/pause`, `/resume`, `/resync`, `/help`), gated by `filters.me` (only the logged-in account can trigger them)
- Interactive chat discovery/selection in [app/mirror/dialogs.py](app/mirror/dialogs.py): lists the account's groups/channels (`fetch_mirrorable_chats`), paginates them (`format_page`), and resolves a `/setsource`/`/setdest` argument (index from the last listing, raw id, or `@username`) to a concrete chat (`resolve_reference`)
- Persisted, mutable source/destination config in [app/mirror/runtime.py](app/mirror/runtime.py): caches the current pair in-process, persists it via `MirrorRepository`, and (re)starts the backlog task whenever the pair is set or changed
- Live media handler in [app/bot/handlers.py](app/bot/handlers.py) (registered broadly on `filters.media`, not a static per-chat filter) wired to [app/mirror/listener.py](app/mirror/listener.py), which checks the *current* source chat from `runtime.current()` on every call
- Resumable backlog scan in [app/mirror/backlog.py](app/mirror/backlog.py), keyed off `runtime.current()` rather than static settings
- Single choke-point transfer logic in [app/mirror/transfer.py](app/mirror/transfer.py): dedup check, rate limit, serialized send, FloodWait retry, ledger write
- Media type detection/filtering in [app/mirror/media.py](app/mirror/media.py)
- Rate limiting and FloodWait/ban handling in [app/mirror/throttle.py](app/mirror/throttle.py)
- Pause/resume/restricted-state flags in [app/mirror/control.py](app/mirror/control.py)
- Dedup ledger, sync progress, and mirror config persistence in [app/database/models.py](app/database/models.py) (`TransferredMedia`, `SyncState`, `MirrorConfig`) and [app/database/repositories.py](app/database/repositories.py) (`MirrorRepository`)
- Alembic bootstrap in [app/database/base.py](app/database/base.py) and migrations in [migrations](migrations) (`0001_mirror_state`, `0002_mirror_config`)
- Logging in [app/services/logger.py](app/services/logger.py)
- One-time interactive session generator: [scripts/generate_session.py](scripts/generate_session.py)
- Example systemd unit for always-on deployment without Docker: [deploy/telegram-mirror.service](deploy/telegram-mirror.service)

## Runtime Flow
1. [app/bot/client.py](app/bot/client.py) initializes the database via Alembic, logs in with `SESSION_STRING`, and imports [app/bot/handlers.py](app/bot/handlers.py) (deferred, to avoid a circular import) so handlers actually register before `bot.start()`.
2. After login, `runtime.load()` reads any previously saved source/destination pair from the database. If none exists yet and `SOURCE_CHAT_ID`/`DEST_CHAT_ID` are set in `.env`, they're resolved via `get_chat()` and used as a one-time seed; otherwise the mirror stays unconfigured until `/setsource`/`/setdest` are used.
3. `/chats` lists the account's groups/channels (paginated); `/setsource <n>` and `/setdest <n>` (or a raw id/`@username`) persist the choice via `runtime.set_source`/`set_dest` and call `runtime.ensure_backlog_running()`.
4. Whenever both are set (or changed), a backlog task scans the source chat from newest to oldest, transferring any media not yet in the dedup ledger, and persists a resume cursor after every message; changing the pair again cancels the in-flight scan and starts a fresh one for the new pair.
5. The live handler in [app/bot/handlers.py](app/bot/handlers.py) is registered on `filters.media` for *any* chat (not a static per-chat filter, since the source can change at runtime) and delegates to [app/mirror/listener.py](app/mirror/listener.py), which drops anything not from `runtime.current().source_chat_id`.
6. Both paths funnel through `transfer_message()` in [app/mirror/transfer.py](app/mirror/transfer.py), which is the only place that calls `copy_message`. It is serialized by an `asyncio.Lock` so live and backlog transfers never race, rate-limited by a randomized delay, and wrapped in automatic `FloodWait` retry.
7. Successful transfers are recorded in `transferred_media` keyed by `(dest_chat_id, file_unique_id)`; a DB unique constraint makes this correct even under concurrent/duplicate processing.
8. If Telegram raises `PeerFlood`/ban/deactivation errors, the mirror pauses itself and reports the reason via `/status` instead of retrying blindly.

## Configuration
- Required variables: `API_ID`, `API_HASH`, `SESSION_STRING`
- Optional one-time seed: `SOURCE_CHAT_ID`, `DEST_CHAT_ID` (real config lives in the `mirror_config` DB table, set via `/setsource`/`/setdest`)
- Throttling: `MIN_DELAY_SECONDS`, `MAX_DELAY_SECONDS`, `FLOOD_WAIT_MAX_RETRIES`
- Filtering: `MEDIA_TYPES`
- Behavior: `SYNC_HISTORY_ON_START`
- Storage: `DATABASE_URL`, defaulting to `sqlite:///./data/telegram_mirror.db`
- Runtime options: `LOG_LEVEL`, `DATA_DIR`
- Control commands always require `filters.me` (only the logged-in account owner can trigger them) - there is no separate admin allowlist
- Example template: [.env.example](.env.example)

## Deployment Surface
- Container image: [Dockerfile](Dockerfile)
- Compose runtime: [docker-compose.yml](docker-compose.yml) (service `mirror`, volume `mirror-data`, `restart: unless-stopped`)
- systemd alternative: [deploy/telegram-mirror.service](deploy/telegram-mirror.service) (`Restart=always`)
- CI: [.github/workflows/ci.yml](.github/workflows/ci.yml)
- Package metadata and dev tooling: [pyproject.toml](pyproject.toml)
- User docs: [README.md](README.md)
- Release checklist: [RELEASE_READINESS.md](RELEASE_READINESS.md)

## Notes For Future Changes
- `transfer_message()` in [app/mirror/transfer.py](app/mirror/transfer.py) must remain the only call site for `copy_message` - it's what guarantees serialization, dedup, and flood-wait handling stay consistent between the live handler and the backlog scan. Do not add a second send path.
- Dedup is keyed on Telegram's `file_unique_id`, not on message ID - this is what makes backlog rescans (`/resync`) and restarts idempotent.
- The source/destination pair is mutable at runtime (`/setsource`/`/setdest` can be re-run any time), so nothing should cache it as a static import-time constant - always read it via `app.mirror.runtime.current()`.
- Keep mirror/sync/config state in the database, not in memory, except for the transient pause/restricted flags in [app/mirror/control.py](app/mirror/control.py) and the in-process pair cache + backlog task handle in [app/mirror/runtime.py](app/mirror/runtime.py), which are intentionally process-local (reloaded from the DB on startup).
- If schema changes are added later, add a new Alembic revision and keep `alembic upgrade head` as the deploy step.
- Prior scaffold (v2.x) used a `BOT_TOKEN`-based bot that downloaded media to local disk on an admin `/archive` command; that mode has been fully replaced, not kept alongside this one.
