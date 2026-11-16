# Admin v2 — design note

Status: implemented on `feature/admin-v2`. Scope: owner-only admin panel, multi-source
mirroring into one destination, paste-a-link transfer, live monitor with skip, and a more
reliable transfer pipeline.

## Findings that shaped the design

1. **The transfer path is download + upload, not `copy_message`** (the older docs said
   otherwise). It is what makes protected chats work, so it is kept and extended. It also had
   two latent problems that are fixed here: only photo/video/animation were actually sent
   (documents/audio/voice/video notes were downloaded, deleted and *recorded as done*), and
   there was no timeout or progress.
2. **Cross-DC downloads pay a full auth-key exchange for every file.** In Pyrogram 2.0.106
   `Client.get_file` builds a new `Session` per file; for a file on another DC it runs
   `Auth(...).create()` (DH exchange) plus `ExportAuthorization`/`ImportAuthorization`, then
   stops the session. `Client.media_sessions` exists but is never used. Reuse would need
   overriding/patching a ~200-line private method of the library and cannot be tested without
   Telegram, so it is **not enabled**; it is documented as a follow-up (see "Out of scope").
3. **Control-handler scope bug (fixed):** with no `BOT_TOKEN`, the handlers listened on the
   userbot with `filters.me`, so a link pasted into *any* outgoing message would trigger a
   transfer. Fallback mode is now restricted to Saved Messages; bot mode to private chats.

## Architecture

```
app/bot/         handlers.py (wiring) · panel.py (texts/keyboards) · live.py · linkflow.py · texts.py
app/mirror/      transfer.py (single choke point) · guard.py (timeouts/skip) · jobs.py (registry)
                 runtime.py (destination + sources + backlog manager) · backlog.py · listener.py
                 links.py (parser) · sources.py (resolve/verify) · limits.py (runtime settings)
app/database/    models.py · repositories.py · migrations/0003
```

Rules kept from v1: `transfer_message()` is the only place that sends; everything (live,
backlog, link jobs) goes through it, serialised by one lock, spaced by the rate limiter, with
FloodWait handling. The control bot never reads source chats; the userbot does.

## Data model (migration `0003_admin_v2`, additive only)

* `sources(id, chat_id UNIQUE, title, enabled, history_sync, state, last_error, created_at)`.
  Existing `mirror_config.source_*` is copied into it. `state` is health (`active`/`error`);
  "paused" is `enabled = false`.
* `skipped_media(id, source_chat_id, source_message_id, file_unique_id, media_type, file_size,
  reason, created_at, resolved_at)`. Never touches the ledger.
* `bot_settings(key, value)` for `/limit` values.
* `mirror_config` keeps its columns: the destination is still read from it, and the legacy
  source columns are no longer read (the downgrade refreshes them from the current sources).
  `sync_state` and `transferred_media` are untouched; an index on
  `transferred_media(source_chat_id)` is added.

Additive-only on purpose: no table rebuild of the 25k+ row ledger on a live DB, so SQLite
batch mode is not needed for this revision (`env.py` already sets `render_as_batch`).
The migration is idempotent (checks for existing tables/indexes) and has a downgrade that
sets `mirror_config.source_*` from the first enabled source (or clears them if there are
none). Sources beyond the first exist only in v2 and are dropped on downgrade.

**Rollback is not "just run the old image".** `init_db()` runs `alembic upgrade head` on every
start and an old image does not know revision `0003`, so it refuses to start (`Can't locate
revision identified by '0003_admin_v2'` - verified against the production image). Roll back
with `alembic downgrade 0002_mirror_config` using the new image first, or restore the backup.

## Dedup across sources

The ledger key stays `(dest_chat_id, file_unique_id)` and does not include the source, so the
same media arriving from two sources is archived once. Covered by a test.

## Transfer pipeline (`transfer.py` + `guard.py`)

queued → download → upload, each stage under a guard that enforces (temp files live in an
**absolute** `DATA_DIR/tmp`: Pyrogram resolves a relative download directory against
`sys.argv[0]`'s directory, which is not the working directory):
* an overall timeout = `TRANSFER_TIMEOUT_BASE_SECONDS + size / TRANSFER_MIN_SPEED_KBPS`
  (so large files are not killed by a fixed cap), implemented with `asyncio.wait` plus a
  watchdog loop instead of a literal `wait_for` so it can also do the following, and a stall timeout
  when no bytes move for `TRANSFER_STALL_SECONDS`;
* progress logged every 30 s (message id, size, bytes done — never content);
* skip: the progress callback calls `client.stop_transmission()`; the guard also cancels the
  task if it does not stop within a few seconds.
Timeout/stall → up to 2 retries → recorded in `skipped_media` (reason `timeout`) and the
queue moves on. Temp files live in `DATA_DIR/tmp/<job id>/` and are always removed
(also swept at startup). Downloaded size is checked against the expected size.

## Jobs & live monitor

`JobRegistry` (in-memory) tracks every job: stage, bytes, speed (sliding window), ETA.
`/live` owns one message and edits it every ~3 s only if the text changed, backing off on
`FloodWait`, ignoring `MessageNotModified`. Large files (`LARGE_FILE_MB`, default 500,
`/limit`) send a notice with a Skip button without stopping; optional auto-skip is off by default.

## Multi-source runtime

Live handler: routes any media whose chat is an *enabled* source. Backlog: one loop over
enabled sources with `history_sync`, sequential, resumable from each `(source, dest)`
cursor; adding a source never interrupts the running one. `SYNC_HISTORY_ON_START=false`
still disables automatic backlog; `/resync [source]` forces it.

## Links

Any owner message to the control bot containing `t.me` links is processed immediately through
an `asyncio.Queue` (no command, no confirmation). Works for any chat the userbot can read;
only the linked message is transferred; duplicates offer a "transfer anyway" button.

## Deviations from the brief (and why)

* The watchdog is `asyncio.wait` + a polling loop rather than a bare `asyncio.wait_for`.
* Migration `0003` is additive, so it does not need SQLite batch mode.
* `/setsource` now *adds* a source (it used to replace the single one).
* Link transfers ignore `MEDIA_TYPES` (an explicit request beats a filter meant for bulk mirroring).
* A retried upload can, rarely, double-post if the first attempt reached Telegram but its
  response was lost.
* File names are kept in memory only (shown in `/live` and the large-file notice).

## Compatibility

`/chats`, `/status`, `/pause`, `/resume`, `/resync`, `/convert`, `/help` keep working.
`/setsource` is now an alias of `/addsource` (it adds instead of replacing — use
`/removesource` to drop one). `/setdest` is unchanged (plus a confirmation when replacing
an existing destination, because the ledger is per destination).

## Out of scope / follow-ups

* Reusing cross-DC media sessions (finding 2) — needs a live-account experiment.
* Persisting the in-memory recent-error list across restarts.
