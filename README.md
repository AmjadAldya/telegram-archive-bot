# Telegram Archive Bot

Telegram Archive Bot is a Python/Pyrogram service that queues archive jobs, persists progress in SQLite, and archives media into a job-specific folder. It supports admin-only archive control, retry/cancel commands, and Alembic-based database migrations.

## Features

- Pyrogram bot startup with `/start`
- Admin-only `/archive` command
- Admin-only `/cancel <job_id>` and `/retry <job_id>` commands
- Persistent archive job tracking in SQLite
- Media downloads saved under `data/archive/<job_id>`
- Resume support through stored progress markers
- Alembic migrations for repeatable schema evolution
- Docker and Docker Compose deployment
- CI checks for tests, linting, formatting, and type checking

## Requirements

- Python 3.10+
- Telegram `API_ID` and `API_HASH`
- Telegram bot token
- At least one admin ID in `ADMIN_ID` or `ADMIN_IDS`

## Local setup

1. Copy `.env.example` to `.env` and fill in your Telegram credentials.
2. Install dependencies:

```bash
pip install -e ".[dev]"
```

3. Apply the database migration:

```bash
alembic upgrade head
```

4. Run the bot:

```bash
python -m app.main
```

## Commands

- `/start` confirms the bot is running.
- `/archive` queues an archive job for the configured chat, defaulting to `me`.
- `/archive <chat>` queues a job for a specific chat reference.
- `/cancel <job_id>` cancels a queued job or requests cancellation for a running job.
- `/retry <job_id>` requeues a failed job if it still has retry budget left.
- `/status` shows the latest archive jobs.

## Docker

Build and run with Compose:

```bash
docker compose up --build
```

If you are deploying a fresh database, run migrations first:

```bash
docker compose run --rm bot alembic upgrade head
```

SQLite data is stored in the `archive-data` volume.

## Deployment notes

- Keep the `.env` file out of Git.
- Run `alembic upgrade head` whenever schema changes are introduced.
- Use `/status` after deployment to verify that the worker is processing queued jobs.
