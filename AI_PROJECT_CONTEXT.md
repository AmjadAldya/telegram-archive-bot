# AI Project Context

## Current State
- Project: telegram-archive-bot
- Type: Python Telegram bot built with Pyrogram
- Status: runnable single-service bot with persistent archive job tracking, Alembic migrations, admin gating, and filesystem-based media downloads
- Scope: Telegram command handling, archive job queueing, resume state, SQLite persistence, cancellation, retry, Docker deployment, and CI checks

## What Is Implemented
- Startup flow in [app/main.py](app/main.py) and [app/bot/client.py](app/bot/client.py)
- Admin-only command handling in [app/bot/handlers.py](app/bot/handlers.py)
- Persistent job model and repository in [app/database/models.py](app/database/models.py) and [app/database/repositories.py](app/database/repositories.py)
- Alembic bootstrap in [app/database/base.py](app/database/base.py) and migration scripts in [migrations](migrations)
- Queue and worker flow in [app/services/queue.py](app/services/queue.py) and [app/services/worker.py](app/services/worker.py)
- Archive execution in [app/archive/engine.py](app/archive/engine.py)
- Media downloads in [app/archive/downloader.py](app/archive/downloader.py)
- Resume helpers in [app/archive/resume.py](app/archive/resume.py)
- Retry and cancellation services in [app/archive/service.py](app/archive/service.py)
- Admin authorization helper in [app/services/auth.py](app/services/auth.py)
- Logging in [app/services/logger.py](app/services/logger.py)

## Runtime Flow
1. [app/bot/client.py](app/bot/client.py) initializes the database via Alembic and starts the Pyrogram client.
2. [app/bot/handlers.py](app/bot/handlers.py) accepts `/archive`, `/cancel`, `/retry`, and `/status` only from configured admins.
3. [app/archive/service.py](app/archive/service.py) creates a database job, enqueues it, retries failed jobs, or records cancellation requests.
4. [app/services/worker.py](app/services/worker.py) seeds pending jobs on startup and processes queued jobs one at a time.
5. [app/archive/engine.py](app/archive/engine.py) walks chat history, downloads media into `data/archive/<job_id>`, and updates job progress.
6. [app/bot/handlers.py](app/bot/handlers.py) renders recent job summaries for `/status`.

## Configuration
- Required variables: `API_ID`, `API_HASH`, `BOT_TOKEN`
- Admin configuration: `ADMIN_ID` and/or `ADMIN_IDS`
- Storage: `DATABASE_URL`, defaulting to `sqlite:///./data/telegram_archive.db`
- Runtime options: `ARCHIVE_CHAT_ID`, `LOG_LEVEL`, `DATA_DIR`
- Example template: [.env.example](.env.example)

## Deployment Surface
- Container image: [Dockerfile](Dockerfile)
- Compose runtime: [docker-compose.yml](docker-compose.yml)
- CI: [.github/workflows/ci.yml](.github/workflows/ci.yml)
- Package metadata and dev tooling: [pyproject.toml](pyproject.toml)
- User docs: [README.md](README.md)
- Release checklist: [RELEASE_READINESS.md](RELEASE_READINESS.md)

## Validation Status
- Static checks: passed in the last validation run
- Tests: passed in the last validation run
- Type checks: passed in the last validation run
- Known remaining product gaps: no upload/export pipeline, no multi-instance queue backend, and no metrics or health endpoint

## Notes For Future Changes
- Prefer the repository and worker flow over direct command-triggered processing.
- Keep archive state in the database, not in memory.
- Avoid reintroducing placeholder modules unless they are wired into runtime behavior.
- If schema changes are added later, add a new Alembic revision and keep `alembic upgrade head` as the deploy step.
