from __future__ import annotations

from typing import Any

from app.config.settings import ADMIN_IDS


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


async def ensure_admin(message: Any) -> bool:
    user = message.from_user
    if is_admin(user.id if user else None):
        return True

    await message.reply("You are not authorized to use this command.")
    return False
