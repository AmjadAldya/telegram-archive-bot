from __future__ import annotations

from pyrogram import filters

from app.archive.service import (
    cancel_archive_job,
    list_recent_archive_jobs,
    retry_archive_job,
    submit_archive_job,
)
from app.bot.client import bot
from app.services.auth import ensure_admin
from app.services.queue import pending_tasks


@bot.on_message(filters.command("start"))
async def start(_, message):
    await message.reply("Archive Bot running.")


@bot.on_message(filters.command("archive"))
async def archive(_, message):
    if not await ensure_admin(message):
        return

    command = getattr(message, "command", []) or []
    source_chat_id = command[1] if len(command) > 1 else None

    job = await submit_archive_job(message.from_user.id, source_chat_id)
    await message.reply(
        f"Queued archive job {job.id} for {job.source_chat_id}. Pending tasks: {pending_tasks()}."
    )


@bot.on_message(filters.command("cancel"))
async def cancel(_, message):
    if not await ensure_admin(message):
        return

    command = getattr(message, "command", []) or []
    if len(command) < 2:
        await message.reply("Usage: /cancel <job_id>")
        return

    try:
        job = await cancel_archive_job(command[1])
    except RuntimeError as exc:
        await message.reply(str(exc))
        return

    await message.reply(f"Archive job {job.id} is now {job.status.value}.")


@bot.on_message(filters.command("retry"))
async def retry(_, message):
    if not await ensure_admin(message):
        return

    command = getattr(message, "command", []) or []
    if len(command) < 2:
        await message.reply("Usage: /retry <job_id>")
        return

    try:
        job = await retry_archive_job(command[1])
    except RuntimeError as exc:
        await message.reply(str(exc))
        return

    await message.reply(
        f"Archive job {job.id} requeued. Retry count: {job.retry_count}/{job.max_retries}."
    )


@bot.on_message(filters.command("status"))
async def status(_, message):
    if not await ensure_admin(message):
        return

    jobs = await list_recent_archive_jobs(limit=5)
    if not jobs:
        await message.reply("No archive jobs have been recorded yet.")
        return

    lines = ["Recent archive jobs:"]
    for job in jobs:
        lines.append(job.summary())

    await message.reply("\n".join(lines))
