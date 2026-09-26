"""ES256 mandate tokens with mechanically attenuating caveats."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import jwt


class MandateError(ValueError):
    pass


@dataclass(frozen=True)
class MandateCaveats:
    budget: int
    categories: tuple[str, ...]
    max_depth: int
    per_tx_max: int
    exp: int

    def normalized(self) -> dict:
        return {
            "budget": int(self.budget),
            "categories": sorted({str(item).strip().lower() for item in self.categories if str(item).strip()}),
            "max_depth": int(self.max_depth),
            "per_tx_max": int(self.per_tx_max),
            "exp": int(self.exp),
        }


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _claims(token: str, key: str, *, verify_exp: bool = True) -> dict:
    try:
        return jwt.decode(token, key, algorithms=["ES256"], options={"verify_aud": False, "verify_exp": verify_exp})
    except jwt.PyJWTError as exc:
        raise MandateError(f"invalid mandate token: {str(exc)[:120]}") from exc


def _check_values(parent: dict, child: MandateCaveats) -> None:
    values = child.normalized()
    if values["budget"] < 0 or values["per_tx_max"] < 0 or values["max_depth"] < 0:
        raise MandateError("mandate caveats must be non-negative")
    if values["budget"] > int(parent["budget"]):
        raise MandateError("child budget exceeds parent budget")
    if values["per_tx_max"] > int(parent["per_tx_max"]):
        raise MandateError("child per-transaction cap exceeds parent cap")
    if values["max_depth"] > int(parent["max_depth"]):
        raise MandateError("child max depth exceeds parent max depth")
    if values["exp"] > int(parent["exp"]):
        raise MandateError("child expiry exceeds parent expiry")
    if not set(values["categories"]).issubset(set(parent.get("categories", []))):
        raise MandateError("child categories exceed parent categories")


def issue_mandate(*, private_key: str, issuer: str, subject: str,
                  caveats: MandateCaveats, parent_token: str | None = None,
                  parent_public_key: str | None = None, now: int | None = None) -> str:
    values = caveats.normalized()
    current = int(now if now is not None else _now())
    if values["exp"] <= current:
        raise MandateError("mandate expiry must be in the future")
    if parent_token:
        if not parent_public_key:
            raise MandateError("parent public key is required for attenuation")
        parent = _claims(parent_token, parent_public_key)
        _check_values(parent, caveats)
        parent_hash = mandate_hash(parent_token)
    else:
        parent_hash = ""
    claims = {
        "typ": "agent-mandate",
        "iss": issuer,
        "sub": subject,
        "jti": uuid.uuid4().hex,
        **values,
        "parent": parent_hash,
    }
    return jwt.encode(claims, private_key, algorithm="ES256")


def validate_mandate(token: str, public_key: str, *, now: int | None = None) -> dict:
    claims = _claims(token, public_key)
    current = int(now if now is not None else _now())
    if claims.get("typ") != "agent-mandate":
        raise MandateError("unexpected mandate type")
    for field in ("budget", "max_depth", "per_tx_max", "exp"):
        if int(claims.get(field, -1)) < 0:
            raise MandateError(f"invalid mandate caveat: {field}")
    if int(claims["exp"]) <= current:
        raise MandateError("mandate expired")
    return claims


def mandate_hash(token: str) -> str:
    return "0x" + hashlib.sha256(token.encode("utf-8")).hexdigest()
