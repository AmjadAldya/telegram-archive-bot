from __future__ import annotations

from app.services.auth import is_admin


def test_is_admin_uses_configured_admin_ids() -> None:
    assert is_admin(123456789) is True
    assert is_admin(None) is False
    assert is_admin(1) is False
