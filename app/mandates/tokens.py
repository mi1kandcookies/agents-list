"""Mandate tokens (docs/decisions/0001-custody-chain.md §5).

A mandate is an ES256 JWS with header ``{"alg":"ES256","typ":"mandate+jwt","kid":…}``
where ``kid`` is the RFC 7638 thumbprint of the signing key. Claims::

    {"iss":"agents-list","jti":"MND-…","sub":"AGT-…","hum":"<sha256(world_sub)>",
     "root":"MND-…","par":null,"dep":0,"apr":"APR-…","eng":"ENG-…","sow":"0x…",
     "cap":{"budget_micro":…,"categories":[…],"max_depth":2,"per_tx_max_micro":…,"payees":null},
     "iat":…,"nbf":…,"exp":…}

This module only signs, verifies and compares caps; the database-aware rules
(spent budget, sibling reservations, revocation, bans) live in ``service``.

Signing key: ``MANDATE_SIGNING_KEY`` (PEM, from app config or the environment;
literal ``\\n`` sequences are accepted). Without it, outside production, a
P-256 key is generated once into ``<instance>/mandate_key.pem``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ISSUER = "agents-list"
TYP = "mandate+jwt"
ALG = "ES256"
KEY_FILENAME = "mandate_key.pem"

CODES = frozenset({"MANDATE_INVALID", "MANDATE_EXCEEDED", "MANDATE_EXPIRED", "MANDATE_REVOKED",
                   "DEPTH_EXCEEDED", "CATEGORY_NOT_ALLOWED", "BANNED"})


class MandateError(Exception):
    """A mandate check failed. ``code`` is one of ``CODES``."""

    def __init__(self, code: str, message: str | None = None):
        if code not in CODES:
            raise ValueError(f"unknown mandate error code {code!r}")
        self.code = code
        self.message = message or code
        super().__init__(f"{code}: {self.message}")


# ── Signing key ──────────────────────────────────────────────────────────────

_KEYS: dict[str, ec.EllipticCurvePrivateKey] = {}


def _load_pem(pem: str | bytes) -> ec.EllipticCurvePrivateKey:
    if isinstance(pem, str):
        pem = pem.replace("\\n", "\n").encode()
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise RuntimeError("MANDATE_SIGNING_KEY must be a P-256 (ES256) private key")
    return key


def generate_pem() -> str:
    """A fresh unencrypted PKCS#8 P-256 private key as PEM."""
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def _instance_key_pem(instance_path: str) -> str:
    """Read ``<instance>/mandate_key.pem``, creating it (mode 0600) on first use."""
    path = Path(instance_path) / KEY_FILENAME
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        pem = generate_pem()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # another process won the race; use its key
        else:
            with os.fdopen(fd, "w") as fh:
                fh.write(pem)
    return path.read_text()


def signing_key() -> ec.EllipticCurvePrivateKey:
    """The process's mandate signing key (see module docstring)."""
    from flask import current_app, has_app_context

    pem = None
    instance_path = None
    production = os.environ.get("FLASK_ENV", "development") == "production"
    if has_app_context():
        pem = current_app.config.get("MANDATE_SIGNING_KEY")
        instance_path = current_app.instance_path
        production = str(current_app.config.get("ENV_NAME", "")).lower() == "production"
    pem = pem or os.environ.get("MANDATE_SIGNING_KEY") or None
    if not pem:
        if production:
            raise RuntimeError("MANDATE_SIGNING_KEY must be set in production")
        if instance_path is None:
            raise RuntimeError("no MANDATE_SIGNING_KEY and no app instance folder for a dev key")
        pem = _instance_key_pem(instance_path)
    key = _KEYS.get(pem)
    if key is None:
        key = _KEYS[pem] = _load_pem(pem)
    return key


def _b64url_int(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()


def key_thumbprint(public_key: ec.EllipticCurvePublicKey) -> str:
    """RFC 7638 JWK thumbprint (SHA-256, base64url) of a P-256 public key."""
    nums = public_key.public_numbers()
    jwk = {"crv": "P-256", "kty": "EC", "x": _b64url_int(nums.x), "y": _b64url_int(nums.y)}
    digest = hashlib.sha256(json.dumps(jwk, separators=(",", ":"), sort_keys=True).encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# ── Claims ───────────────────────────────────────────────────────────────────

def hum_hash(world_sub: str) -> str:
    """The ``hum`` claim: sha256 hex of the human's pairwise ``sub``; the raw
    ``sub`` never appears in a token."""
    return hashlib.sha256(world_sub.encode("utf-8")).hexdigest()


def normalize_categories(categories) -> list[str]:
    """Strip, drop empties and de-duplicate, keeping first-seen order."""
    if isinstance(categories, str):
        categories = [categories]
    out: list[str] = []
    for item in categories or ():
        value = str(item).strip()
        if value and value not in out:
            out.append(value)
    return out


def check_attenuation(parent_cap: dict, parent_exp: int, child_cap: dict, child_exp: int) -> None:
    """Caps can only narrow from parent to child. Budget against the parent's
    *remaining* allowance is a database check (``service.attenuate``)."""
    if int(child_cap["budget_micro"]) > int(parent_cap["budget_micro"]):
        raise MandateError("MANDATE_EXCEEDED", "child budget exceeds parent budget")
    if int(child_cap["per_tx_max_micro"]) > int(parent_cap["per_tx_max_micro"]):
        raise MandateError("MANDATE_EXCEEDED", "child per-transaction cap exceeds parent cap")
    if int(child_cap["max_depth"]) > int(parent_cap["max_depth"]):
        raise MandateError("DEPTH_EXCEEDED", "child max depth exceeds parent max depth")
    if int(child_exp) > int(parent_exp):
        raise MandateError("MANDATE_EXCEEDED", "child expiry is later than parent expiry")
    extra = set(child_cap["categories"]) - set(parent_cap["categories"])
    if extra:
        raise MandateError("CATEGORY_NOT_ALLOWED",
                           f"categories not in parent mandate: {sorted(extra)}")
    if parent_cap.get("payees") is not None:
        child_payees = child_cap.get("payees")
        if child_payees is None or not set(child_payees) <= set(parent_cap["payees"]):
            raise MandateError("MANDATE_EXCEEDED", "child payees exceed parent payees")


# ── Encode / decode ──────────────────────────────────────────────────────────

def encode(claims: dict, key: ec.EllipticCurvePrivateKey | None = None) -> str:
    key = key or signing_key()
    headers = {"typ": TYP, "kid": key_thumbprint(key.public_key())}
    return jwt.encode(claims, key, algorithm=ALG, headers=headers)


def decode(token: str, *, verify_exp: bool = True,
           key: ec.EllipticCurvePrivateKey | None = None) -> dict:
    """Verify signature, header and issuer; return the claims.

    Raises ``MandateError`` with ``MANDATE_EXPIRED`` for a past ``exp`` and
    ``MANDATE_INVALID`` for anything else.
    """
    key = key or signing_key()
    public = key.public_key()
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise MandateError("MANDATE_INVALID", f"malformed mandate token: {str(exc)[:120]}") from exc
    if header.get("alg") != ALG or header.get("typ") != TYP:
        raise MandateError("MANDATE_INVALID", "unexpected mandate token header")
    if header.get("kid") != key_thumbprint(public):
        raise MandateError("MANDATE_INVALID", "unknown mandate signing key")
    try:
        claims = jwt.decode(token, public, algorithms=[ALG], issuer=ISSUER,
                            options={"verify_aud": False, "verify_exp": verify_exp,
                                     "require": ["exp", "iat", "nbf", "jti", "sub", "iss"]})
    except jwt.ExpiredSignatureError as exc:
        raise MandateError("MANDATE_EXPIRED", "mandate token expired") from exc
    except jwt.PyJWTError as exc:
        raise MandateError("MANDATE_INVALID", f"invalid mandate token: {str(exc)[:120]}") from exc
    cap = claims.get("cap")
    if not isinstance(cap, dict) or not str(claims.get("jti", "")).startswith("MND-"):
        raise MandateError("MANDATE_INVALID", "mandate token is missing its caps")
    return claims
