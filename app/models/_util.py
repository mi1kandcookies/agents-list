"""Column defaults shared by the custody-chain models."""
from __future__ import annotations

from datetime import datetime, timezone

from app.common.ids import new_id


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def id_factory(prefix: str):
    """Column default producing ``<prefix>-XXXXXXXXXXXX``."""
    return lambda: new_id(prefix)
