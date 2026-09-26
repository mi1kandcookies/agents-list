"""
pkce.py - PKCE (RFC 7636) and random OIDC parameters.

Only the S256 method is supported; "plain" leaks the verifier to anyone who
can read the authorization URL.
"""
from __future__ import annotations

import base64
import hashlib
import secrets


def b64url(data: bytes) -> str:
    """base64url without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def new_verifier(nbytes: int = 48) -> str:
    """A fresh code_verifier: 64 chars from the RFC 7636 unreserved set."""
    verifier = b64url(secrets.token_bytes(nbytes))
    if not 43 <= len(verifier) <= 128:
        raise ValueError("code_verifier must be 43-128 characters")
    return verifier


def challenge_s256(verifier: str) -> str:
    """code_challenge = BASE64URL(SHA256(ASCII(code_verifier)))."""
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def new_state() -> str:
    """Opaque, unguessable value for the OAuth `state` parameter."""
    return secrets.token_urlsafe(32)
