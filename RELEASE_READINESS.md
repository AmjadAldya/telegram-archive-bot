# Release Readiness

## Ready

- Bot startup and shutdown are wired through Pyrogram.
- `/start`, `/archive`, `/cancel`, `/retry`, and `/status` handlers are implemented.
- Archive jobs are persisted in SQLite through Alembic-managed schema.
- Media files are downloaded into a job-specific archive directory.
- Resume data is stored on each progress update.
- Failed jobs can be retried within a bounded retry budget.
- Queued and running jobs can be cancelled safely within the current single-worker architecture.
- A background worker processes queued jobs and re-seeds pending work on startup.
- Docker and Compose use a persistent data volume.
- Tests, linting, formatting, and type checking pass.

## Optional Next Steps

- Add richer export or upload formats.
- Add metrics or health endpoints if deployment grows.
- Add a multi-instance queue backend if concurrency requirements increase.
- Add operator-level filtering or per-chat permissions if access control becomes more complex.

## Deploy

1. Set the Telegram and admin variables in `.env`.
2. Run `alembic upgrade head`.
3. Run `docker compose up --build` or `python -m app.main`.
4. Confirm the bot responds to `/start` and `/status`.
5. Queue a job with `/archive` and use `/cancel` or `/retry` as needed.
