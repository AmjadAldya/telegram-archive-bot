from __future__ import annotations

from app.mirror import runtime
from app.mirror.throttle import AccountRestrictedError
from app.mirror.transfer import transfer_message
from app.services.logger import logger


async def handle_incoming_media(client, message) -> None:
    """Entry point for every incoming media message, from any chat.

    Sources are chosen at runtime (/addsource, /removesource, the panel), so
    this checks the cache of *enabled* sources on each call and ignores
    everything else. Several of these can start concurrently (Pyrogram runs one
    task per update); the lock inside transfer_message keeps the sends serialised.
    """
    destination = runtime.destination()
    if destination is None or not runtime.is_enabled_source(message.chat.id):
        return

    try:
        await transfer_message(client, message, destination.chat_id, kind="live")
    except AccountRestrictedError:
        logger.critical("Live mirror stopped: Telegram restricted this account")
    except Exception:  # noqa: BLE001 - never let one message crash the update handler
        logger.exception("Failed to mirror message %s", message.id)
