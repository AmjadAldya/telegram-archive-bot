from __future__ import annotations

from app.config.settings import DEST_CHAT_ID
from app.mirror.throttle import AccountRestrictedError
from app.mirror.transfer import transfer_message
from app.services.logger import logger


async def handle_incoming_media(client, message) -> None:
    """Entry point for new messages arriving in the source chat.

    Pyrogram dispatches one task per incoming update, so several of these can
    start concurrently; the shared lock inside transfer_message keeps the
    actual sends serialized.
    """
    try:
        await transfer_message(client, message, DEST_CHAT_ID)
    except AccountRestrictedError:
        logger.critical("Live mirror stopped: Telegram restricted this account")
    except Exception:
        logger.exception("Failed to mirror message %s", message.id)
