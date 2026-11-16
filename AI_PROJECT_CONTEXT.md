# AI Project Context

## Current State
- Project: telegram-archive-bot (media mirror userbot with an Arabic, owner-only admin panel)
- Type: Python Telegram **userbot** built with Pyrogram 2.0.x (user session, not a Bot API token), plus an optional control bot (`BOT_TOKEN`) for the panel
- Status: runnable service. Mirrors media from **many source chats into one destination**, with live mirroring, resumable per-source backlog, paste-a-link transfers, a live monitor with skip, a dedup ledger, anti-flood throttling, SQLite via Alembic, Docker/systemd deployment, CI
- Design notes for the admin v2 work: [docs/ADMIN_V2.md](docs/ADMIN_V2.md)

## Why a userbot (and why download + upload)
A Bot API token cannot join or read private/protected groups. The account must already be a member of every source. Transfers **download then upload again** (not `copy_message`) - that is what makes protected chats work. Authentication is a pre-generated `SESSION_STRING` ([scripts/generate_session.py](scripts/generate_session.py)). The control bot (if any) cannot read source chats: every read of a source goes through the userbot.

## Layout
- `app/bot/` - `client.py` (startup/shutdown, scoped command menu, env seed), `handlers.py` (all owner-only commands + callbacks), `panel.py` (status data + screens), `live.py` (the self-editing /live message), `linkflow.py` (paste-a-link queue and results), `texts.py` (Arabic strings/formatters)
- `app/mirror/` - `transfer.py` (the single send path), `guard.py` (timeouts/stall/skip watchdog), `jobs.py` (in-memory job registry, progress math, recent errors), `runtime.py` (destination + sources + backlog manager), `backlog.py` (per-source scan), `listener.py` (live routing), `links.py` (t.me parser), `sources.py` (resolve/verify a source), `dialogs.py` (chat picker), `limits.py` (/limit values), `media.py`, `throttle.py`, `control.py`
- `app/database/` - `models.py`, `repositories.py`; migrations in `migrations/versions/` (`0001` ledger+sync_state, `0002` mirror_config, `0003_admin_v2` sources/skipped_media/bot_settings)

## Data model
- `mirror_config` (singleton): the **destination**. Its legacy `source_*` columns are no longer read (kept, and refreshed by the 0003 downgrade).
- `sources`: chat_id (unique), title, `enabled` (false = paused), `history_sync`, `state` (active/error), `last_error`, created_at
- `transferred_media`: dedup ledger, unique on `(dest_chat_id, file_unique_id)` - **not** per source, so the same media from two sources is archived once
- `sync_state`: per `(source, destination)` backlog cursor (`resume_before_message_id`) and counters
- `skipped_media`: skipped files (user_skip / timeout / auto_skip); never in the ledger; retriable
- `bot_settings`: `/limit` values

## Runtime flow
1. `run_bot()`: Alembic upgrade, sweep `DATA_DIR/tmp`, start userbot, warm the peer cache, load destination/sources/limits, start the control bot and publish the Arabic menu (scoped to the owner chat; falls back to default scope), register handlers, start the link queue, seed from env only if the DB is empty, start the backlog if `SYNC_HISTORY_ON_START`.
2. Live: `listener.handle_incoming_media` -> `transfer_message` for messages from an *enabled* source.
3. Backlog: one loop over enabled sources with `history_sync`, sequentially, each resumable; `/resync [source]` forces it regardless of the flags.
4. Links: any owner message with `t.me` message links -> `LinkQueue` -> `run_link` -> `transfer_message(ignore_type_filter=True)`.
5. `transfer_message` (queued -> download -> upload): dedup, pause check, one lock, rate limiter, FloodWait handling, `guard.run_guarded` (size-scaled timeout, stall, skip), retries (default 2) then skip, ledger write, temp dir always removed.

## Invariants to keep
- `transfer_message()` is the only place that sends; do not add a second send path.
- Everything is owner-only: handlers use `owner_filter & scope`; non-owners are ignored silently. Fallback mode (no `BOT_TOKEN`) must stay limited to Saved Messages.
- Temp download paths must be **absolute** (Pyrogram resolves relative ones against `sys.argv[0]`'s directory); `tmp_root()` guarantees it.
- Never log message content or file names; ids, sizes and codes only. File names live in memory (`Job.file_name`) for /live only.
- Source/destination are mutable at runtime: read them via `runtime`, never cache them as constants.
- Migrations must be additive/idempotent and have a working downgrade; an older image refuses to start on a newer schema, so rollback = downgrade first.
- Tests must not depend on a developer's `.env` (conftest pins the relevant env vars) and must reset module-level asyncio state (conftest does).

## Configuration
See the README table. Required: `API_ID`, `API_HASH`, `SESSION_STRING`. Notable optional: `BOT_TOKEN`, `SYNC_HISTORY_ON_START`, `MEDIA_TYPES`, `LARGE_FILE_MB`, `TRANSFER_*`, `PROGRESS_LOG_SECONDS`, `LIVE_REFRESH_SECONDS`. Example: [.env.example](.env.example).

## Deployment Surface
- [Dockerfile](Dockerfile) (multi-stage, TgCrypto compiled in a throwaway stage), [docker-compose.yml](docker-compose.yml) (service `mirror`, volume `mirror-data`, `restart: unless-stopped`), [deploy/](deploy/) scripts and systemd unit
- CI: [.github/workflows/ci.yml](.github/workflows/ci.yml) (pytest, ruff check/format, mypy). Deploy is manual only ([deploy.yml](.github/workflows/deploy.yml), `workflow_dispatch`).

## Known limitations / follow-ups
- Pyrogram 2.0.106 creates a new media session (and, for another DC, a new auth key) for every downloaded file; reusing sessions would need patching a private method and a live-account experiment - not implemented.
- A retried upload can, rarely, double-post if the first attempt reached Telegram but its response was lost.
- Recent errors in the panel are in-memory (reset on restart).
- Behavior against real Telegram (`stop_transmission` mid-transfer, scoped command menu, live message edits) is covered by mocks only.
