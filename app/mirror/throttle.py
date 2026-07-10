from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, TypeVar

from pyrogram.errors import (
    AuthKeyUnregistered,
    FloodWait,
    PeerFlood,
    SlowmodeWait,
    UserDeactivated,
    UserDeactivatedBan,
)

from app.config.settings import FLOOD_WAIT_MAX_RETRIES, MAX_DELAY_SECONDS, MIN_DELAY_SECONDS
from app.services.logger import logger

T = TypeVar("T")


class AccountRestrictedError(RuntimeError):
    """Raised when Telegram signals the account is banned or otherwise restricted.

    The worker must stop instead of retrying, since retrying a banned/flooded
    account only makes the restriction worse.
    """


class RateLimiter:
    """Serializes outgoing actions with a randomized delay between them.

    Sending Telegram requests back-to-back at machine speed is the single
    biggest trigger for FloodWait and account limits. A single shared limiter,
    combined with a single-worker queue, keeps every transfer spaced out.
    """

    def __init__(self, min_delay: float = MIN_DELAY_SECONDS, max_delay: float = MAX_DELAY_SECONDS):
        self._min_delay = min_delay
        self._max_delay = max_delay

    async def wait(self) -> None:
        delay = random.uniform(self._min_delay, self._max_delay)
        if delay > 0:
            await asyncio.sleep(delay)


rate_limiter = RateLimiter()


async def call_with_flood_wait(
    func: Callable[..., Awaitable[T]],
    *args: Any,
    max_retries: int = FLOOD_WAIT_MAX_RETRIES,
    **kwargs: Any,
) -> T:
    """Call a Pyrogram coroutine, retrying on FloodWait and surfacing hard bans.

    FloodWait/SlowmodeWait tell us exactly how long to sleep, so we honor that
    (plus a small safety buffer) instead of guessing. PeerFlood/deactivation
    errors mean the account is already restricted, so we stop immediately
    rather than hammering Telegram further.
    """
    attempt = 0
    while True:
        try:
            return await func(*args, **kwargs)
        except (FloodWait, SlowmodeWait) as exc:
            attempt += 1
            if attempt > max_retries:
                logger.error("Giving up after %d FloodWait retries", max_retries)
                raise
            wait_seconds = exc.value + random.uniform(1, 3)
            logger.warning(
                "FloodWait: sleeping %.1fs (attempt %d/%d)",
                wait_seconds,
                attempt,
                max_retries,
            )
            await asyncio.sleep(wait_seconds)
        except (PeerFlood, UserDeactivatedBan, UserDeactivated, AuthKeyUnregistered) as exc:
            logger.critical("Account restricted by Telegram, stopping mirror: %s", exc)
            raise AccountRestrictedError(str(exc)) from exc
