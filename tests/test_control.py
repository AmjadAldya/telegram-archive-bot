from __future__ import annotations

import asyncio

from app.mirror import control


def test_pause_resume_toggles_paused_state() -> None:
    assert control.is_paused() is False

    control.pause()
    assert control.is_paused() is True

    control.resume()
    assert control.is_paused() is False


def test_wait_if_paused_blocks_until_resumed() -> None:
    async def scenario() -> None:
        control.pause()
        waiter = asyncio.ensure_future(control.wait_if_paused())
        await asyncio.sleep(0)
        assert waiter.done() is False

        control.resume()
        await asyncio.wait_for(waiter, timeout=1)

    asyncio.run(scenario())


def test_mark_restricted_sets_reason_and_pauses() -> None:
    control.mark_restricted("banned")
    assert control.is_restricted() is True
    assert control.restricted_reason() == "banned"
    assert control.is_paused() is True

    control.resume()
    assert control.is_restricted() is False
    assert control.restricted_reason() is None
