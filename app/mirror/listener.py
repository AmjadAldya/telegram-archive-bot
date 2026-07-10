from __future__ import annotations

from app.mirror import runtime
from app.mirror.throttle import AccountRestrictedError
from app.mirror.transfer import transfer_message
from app.services.logger import logger


async def handle_incoming_media(client, message) -> None:
    """Entry point for every incoming media message, from any chat.

    The source chat is chosen at runtime via /setsource, not a static
    filter, so this checks the currently configured pair on each call and
    ignores anything that isn't from the configured source.

    Pyrogram dispatches one task per incoming update, so several of these can
    start concurrently; the shared lock inside transfer_message keeps the
    actual sends serialized.
    """
    pair = runtime.current()
    if pair is None or message.chat.id != pair.source_chat_id:
        return

    try:
        await transfer_message(client, message, pair.dest_chat_id)
    except AccountRestrictedError:
        logger.critical("Live mirror stopped: Telegram restricted this account")
    except Exception:
        logger.exception("Failed to mirror message %s", message.id)
