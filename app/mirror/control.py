from __future__ import annotations

import asyncio

_pause_event = asyncio.Event()
_pause_event.set()
_restricted = False
_restricted_reason: str | None = None


def pause() -> None:
    _pause_event.clear()


def resume() -> None:
    global _restricted, _restricted_reason
    _restricted = False
    _restricted_reason = None
    _pause_event.set()


def is_paused() -> bool:
    return not _pause_event.is_set()


async def wait_if_paused() -> None:
    await _pause_event.wait()


def mark_restricted(reason: str) -> None:
    global _restricted, _restricted_reason
    _restricted = True
    _restricted_reason = reason
    pause()


def is_restricted() -> bool:
    return _restricted


def restricted_reason() -> str | None:
    return _restricted_reason
