"""Persistence and attenuation helpers for mandate tokens."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.extensions import db
from app.mandates.tokens import MandateCaveats, issue_mandate, mandate_hash, validate_mandate


def save_mandate(*, mandate_id: str, token: str, issuer_human_id: str,
                 subject_agent_id: int | None, caveats: MandateCaveats,
                 parent_mandate_id: str | None = None, engagement_id: str | None = None):
    from app.models import Mandate

    values = caveats.normalized()
    row = Mandate(
        id=mandate_id, parent_mandate_id=parent_mandate_id,
        engagement_id=engagement_id, issuer_human_id=issuer_human_id,
        subject_agent_id=subject_agent_id, budget_atomic=values["budget"],
        categories_json=json.dumps(values["categories"]), max_depth=values["max_depth"],
        per_tx_max_atomic=values["per_tx_max"],
        expires_at=datetime.fromtimestamp(values["exp"], tz=timezone.utc),
        token_jwt=token, token_hash=mandate_hash(token), state="active",
    )
    db.session.add(row)
    db.session.commit()
    return row


__all__ = ["MandateCaveats", "issue_mandate", "mandate_hash", "save_mandate", "validate_mandate"]
