"""
world.py - OpenID Connect client for World ID.

World ID is a standard OIDC provider. It gives every relying party a pairwise
`sub` for the same human and supports forcing a fresh authentication, which
is what lets a human approve one specific action (the action hash is sent as
the OIDC `nonce`).

Flows:
  - Web: authorization code + PKCE S256, always with prompt=login and
    max_age=0 so the provider re-authenticates the human every time.
  - Device (RFC 8628): for agents and CLIs; the human approves on their phone
    using the user_code / verification_uri_complete.

The token endpoint is authenticated with client_secret_basic. ID tokens are
accepted only when signed with RS256 by a key in the provider's JWKS; see
validate_id_token() for every check.

Environment:
    WORLD_ISSUER          default https://sandbox.auth.world.org
    WORLD_CLIENT_ID       OIDC client id
    WORLD_CLIENT_SECRET   OIDC client secret
    WORLD_REDIRECT_URI    callback URL for the web flow
    WORLD_ALLOWED_HOSTS   comma-separated hosts; when WORLD_REDIRECT_URI is
                          unset, a request to one of these hosts uses
                          https://<host>/auth/world/callback (see
                          derive_redirect_uri)
    WORLD_REQUIRED_ACR    if set, ID tokens must carry exactly this `acr`
                          (e.g. https://world.org/oidc/acr/orb-v3)

`http` is anything with requests-style get(url, timeout=...) and
post(url, data=..., headers=..., timeout=...); tests pass a fake.
"""
from __future__ import annotations

import base64
import hmac
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote, urlencode

import jwt
import requests

DEFAULT_ISSUER = "https://sandbox.auth.world.org"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
ALLOWED_ALG = "RS256"

DISCOVERY_TTL_SECONDS = 3600
JWKS_TTL_SECONDS = 3600
JWKS_REFETCH_COOLDOWN_SECONDS = 60  # bounds refetches caused by unknown kids
LEEWAY_SECONDS = 60                 # exp / iat clock skew
AUTH_TIME_SKEW_SECONDS = 5          # auth_time vs. when we started the flow
DEFAULT_DEVICE_INTERVAL = 5
SLOW_DOWN_STEP = 5                  # RFC 8628 section 3.5
HTTP_TIMEOUT_SECONDS = 10
CALLBACK_PATH = "/auth/world/callback"

ID_TOKEN_ERROR_CODES = frozenset({
    "BAD_SIGNATURE", "BAD_ALG", "BAD_ISSUER", "BAD_AUDIENCE", "EXPIRED",
    "NONCE_MISMATCH", "STALE_AUTH", "ACR_MISMATCH", "MISSING_CLAIM",
})


class WorldError(Exception):
    """Configuration, transport or protocol error talking to the provider."""

    def __init__(self, message: str, *, error: Optional[str] = None, status: Optional[int] = None):
        super().__init__(message)
        self.error = error
        self.status = status


class IdTokenError(Exception):
    """An ID token failed validation. `code` is one of ID_TOKEN_ERROR_CODES."""

    def __init__(self, code: str, message: str = ""):
        if code not in ID_TOKEN_ERROR_CODES:
            raise ValueError(f"unknown IdTokenError code {code!r}")
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DeviceStart:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: Optional[str]
    expires_in: int
    interval: int


@dataclass(frozen=True)
class DevicePoll:
    status: str  # pending | slow_down | approved | denied | expired | error
    id_token: Optional[str] = None
    error: Optional[str] = None
    # Poll interval the caller must use from now on (set on pending/slow_down).
    interval: Optional[int] = None


@dataclass(frozen=True)
class IdClaims:
    sub: str
    iss: str
    aud: Any  # str or list[str], as issued
    exp: int
    iat: int
    jti: Optional[str]
    nonce: Optional[str]
    auth_time: Optional[int]
    acr: Optional[str]
    amr: Optional[list]


def _env(name: str) -> Optional[str]:
    value = os.environ.get(name, "").strip()
    return value or None


def allowed_hosts(raw: Optional[str] = None) -> frozenset[str]:
    """WORLD_ALLOWED_HOSTS as a set of lower-case hosts (host[:port])."""
    raw = _env("WORLD_ALLOWED_HOSTS") if raw is None else raw
    return frozenset(h.strip().lower() for h in (raw or "").split(",") if h.strip())


def derive_redirect_uri(host: Optional[str], allowed: frozenset[str]) -> Optional[str]:
    """``https://<host>/auth/world/callback`` when ``host`` is exactly one of
    ``allowed``, else None.

    Only exact matches count (no wildcards), so a forged Host header can never
    name a callback outside the list; the provider also accepts only callbacks
    registered for the client.
    """
    host = (host or "").strip().lower()
    if not host or host not in allowed:
        return None
    return f"https://{host}{CALLBACK_PATH}"


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class WorldClient:
    def __init__(self, issuer=None, client_id=None, client_secret=None, redirect_uri=None,
                 http=requests):
        self.issuer = (issuer or _env("WORLD_ISSUER") or DEFAULT_ISSUER).rstrip("/")
        self.client_id = client_id or _env("WORLD_CLIENT_ID")
        self.client_secret = client_secret or _env("WORLD_CLIENT_SECRET")
        self.redirect_uri = redirect_uri or _env("WORLD_REDIRECT_URI")
        self.required_acr = _env("WORLD_REQUIRED_ACR")
        self.http = http
        self.clock = time.time  # overridable in tests
        self._lock = threading.Lock()
        self._discovery: Optional[dict] = None
        self._discovery_at = 0.0
        self._jwks: Optional[dict] = None
        self._jwks_at = 0.0
        self._device_intervals: dict[str, int] = {}

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    # ── metadata ────────────────────────────────────────────────────────────

    def discovery(self) -> dict:
        """The provider's openid-configuration, cached for an hour."""
        now = self.clock()
        with self._lock:
            if self._discovery is not None and now - self._discovery_at < DISCOVERY_TTL_SECONDS:
                return self._discovery
        doc = self._get_json(self.issuer + "/.well-known/openid-configuration")
        # OIDC Discovery 1.0 section 4.3: the document must name the issuer we asked.
        if str(doc.get("issuer", "")).rstrip("/") != self.issuer:
            raise WorldError(f"discovery issuer {doc.get('issuer')!r} != {self.issuer!r}")
        with self._lock:
            self._discovery, self._discovery_at = doc, now
        return doc

    def jwks(self, *, force: bool = False) -> dict:
        """The provider's signing keys, cached for an hour.

        validate_id_token() calls this with force=True once when a token names
        a kid we don't know (key rotation); forced refetches are rate-limited.
        """
        now = self.clock()
        with self._lock:
            fresh = self._jwks is not None and now - self._jwks_at < JWKS_TTL_SECONDS
            cooling = self._jwks is not None and now - self._jwks_at < JWKS_REFETCH_COOLDOWN_SECONDS
            if fresh and (not force or cooling):
                return self._jwks
        uri = self.discovery().get("jwks_uri")
        if not uri:
            raise WorldError("discovery document has no jwks_uri")
        keys = self._get_json(uri)
        if not isinstance(keys.get("keys"), list):
            raise WorldError("JWKS has no keys array")
        with self._lock:
            self._jwks, self._jwks_at = keys, now
        return keys

    # ── web flow ────────────────────────────────────────────────────────────

    def authorize_url(self, *, state, nonce, code_challenge, prompt="login", max_age=0,
                      acr_values=None, redirect_uri=None) -> str:
        """Authorization URL for the code flow with PKCE S256.

        ``redirect_uri`` overrides WORLD_REDIRECT_URI (e.g. one derived from
        the request host); exchange_code() must then get the same value."""
        if not (state and nonce and code_challenge):
            raise ValueError("state, nonce and code_challenge are required")
        redirect_uri = redirect_uri or self.redirect_uri
        if not self.client_id or not redirect_uri:
            raise WorldError("WORLD_CLIENT_ID and WORLD_REDIRECT_URI must be set")
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": prompt,
            "max_age": str(int(max_age)),
        }
        if acr_values:
            params["acr_values"] = acr_values
        endpoint = self.discovery()["authorization_endpoint"]
        sep = "&" if "?" in endpoint else "?"
        return endpoint + sep + urlencode(params)

    def exchange_code(self, code, code_verifier, redirect_uri=None) -> dict:
        """Redeem an authorization code; returns the token response."""
        redirect_uri = redirect_uri or self.redirect_uri
        if not redirect_uri:
            raise WorldError("WORLD_REDIRECT_URI must be set")
        status, body = self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        })
        if status != 200:
            err = body.get("error") or f"http_{status}"
            raise WorldError(f"token exchange failed: {err} {body.get('error_description', '')}".strip(),
                             error=err, status=status)
        if not body.get("id_token"):
            raise WorldError("token response has no id_token", error="missing_id_token", status=status)
        return body

    # ── device flow ─────────────────────────────────────────────────────────

    def device_authorize(self, *, nonce=None, scope="openid") -> DeviceStart:
        """Start a device authorization (RFC 8628 section 3.1)."""
        endpoint = self.discovery().get("device_authorization_endpoint")
        if not endpoint:
            raise WorldError("provider does not advertise a device_authorization_endpoint")
        data = {"scope": scope}
        if nonce:
            data["nonce"] = nonce
        status, body = self._post_form(endpoint, data)
        if status != 200:
            err = body.get("error") or f"http_{status}"
            raise WorldError(f"device authorization failed: {err}", error=err, status=status)
        try:
            start = DeviceStart(
                device_code=body["device_code"],
                user_code=body["user_code"],
                verification_uri=body["verification_uri"],
                verification_uri_complete=body.get("verification_uri_complete"),
                expires_in=int(body["expires_in"]),
                interval=int(body.get("interval") or DEFAULT_DEVICE_INTERVAL),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorldError(f"malformed device authorization response: {exc}") from exc
        with self._lock:
            self._device_intervals[start.device_code] = start.interval
        return start

    def poll_device(self, device_code, interval=None) -> DevicePoll:
        """One token-endpoint poll for a device authorization (RFC 8628 section 3.4).

        ``interval`` is the caller's stored poll interval. It is used when this
        process has not seen the device code (another serverless instance
        started it, or this one restarted), so a slow_down is never lost."""
        with self._lock:
            known = self._device_intervals.get(device_code)
        interval = max(known or 0, int(interval or 0)) or DEFAULT_DEVICE_INTERVAL
        try:
            status, body = self._token_request({"grant_type": DEVICE_GRANT, "device_code": device_code})
        except WorldError as exc:
            return DevicePoll("error", error=str(exc), interval=interval)
        if status == 200:
            self._forget_device(device_code)
            if not body.get("id_token"):
                return DevicePoll("error", error="missing_id_token")
            return DevicePoll("approved", id_token=body["id_token"])
        err = body.get("error") or f"http_{status}"
        if err == "authorization_pending":
            return DevicePoll("pending", interval=interval)
        if err == "slow_down":
            interval += SLOW_DOWN_STEP
            with self._lock:
                self._device_intervals[device_code] = interval
            return DevicePoll("slow_down", error=err, interval=interval)
        self._forget_device(device_code)
        if err == "access_denied":
            return DevicePoll("denied", error=err)
        if err == "expired_token":
            return DevicePoll("expired", error=err)
        return DevicePoll("error", error=err)

    # ── ID token validation ────────────────────────────────────────────────

    def validate_id_token(self, id_token, *, expected_nonce, not_before, max_age_s,
                          require_acr=None) -> IdClaims:
        """Verify an ID token and return its claims, or raise IdTokenError.

        expected_nonce  compare against `nonce` (None: no nonce binding, e.g. a
                        device flow started without one)
        not_before      unix time the approval flow started; auth_time must
                        not be older (minus a 5 s skew). None skips the check.
        max_age_s       auth_time must be at most this many seconds ago.
                        None skips the check.
        require_acr     exact `acr` required; None uses WORLD_REQUIRED_ACR,
                        "" disables the check.

        Transport failures fetching discovery/JWKS raise WorldError.
        """
        # Header first: the algorithm is pinned before any key is touched, so
        # "none" and HS256-with-the-RSA-public-key tokens never get verified.
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise IdTokenError("BAD_SIGNATURE", f"malformed token: {exc}") from exc
        alg = header.get("alg")
        if alg != ALLOWED_ALG:
            raise IdTokenError("BAD_ALG", f"alg {alg!r} is not {ALLOWED_ALG}")
        kid = header.get("kid")
        if not kid:
            raise IdTokenError("BAD_SIGNATURE", "token header has no kid")

        key = self._signing_key(kid)
        try:
            claims = jwt.decode(
                id_token, key=key, algorithms=[ALLOWED_ALG],
                # Claims are checked below so each failure gets a precise code.
                options={"verify_signature": True, "verify_exp": False, "verify_nbf": False,
                         "verify_iat": False, "verify_aud": False, "verify_iss": False,
                         "verify_sub": False, "verify_jti": False},
            )
        except jwt.InvalidSignatureError as exc:
            raise IdTokenError("BAD_SIGNATURE", "signature does not verify") from exc
        except jwt.InvalidAlgorithmError as exc:
            raise IdTokenError("BAD_ALG", str(exc)) from exc
        except jwt.PyJWTError as exc:
            raise IdTokenError("BAD_SIGNATURE", f"undecodable token: {exc}") from exc

        for name in ("iss", "sub", "aud", "exp", "iat"):
            if claims.get(name) in (None, "", []):
                raise IdTokenError("MISSING_CLAIM", f"{name} is missing")
        if not isinstance(claims["sub"], str):
            raise IdTokenError("MISSING_CLAIM", "sub is not a string")
        for name in ("exp", "iat"):
            if not _is_int(claims[name]):
                raise IdTokenError("MISSING_CLAIM", f"{name} is not an integer")

        issuer = self.discovery()["issuer"]
        if claims["iss"] != issuer:
            raise IdTokenError("BAD_ISSUER", f"iss {claims['iss']!r} != {issuer!r}")

        aud = claims["aud"]
        auds = [aud] if isinstance(aud, str) else aud
        if not isinstance(auds, list) or self.client_id not in auds:
            raise IdTokenError("BAD_AUDIENCE", "client_id not in aud")
        azp = claims.get("azp")
        if (len(auds) > 1 or azp is not None) and azp != self.client_id:
            raise IdTokenError("BAD_AUDIENCE", f"azp {azp!r} != client_id")

        now = self.clock()
        if now > claims["exp"] + LEEWAY_SECONDS:
            raise IdTokenError("EXPIRED", "token has expired")
        if claims["iat"] > now + LEEWAY_SECONDS:
            raise IdTokenError("EXPIRED", "token issued in the future")

        nonce = claims.get("nonce")
        if expected_nonce is not None:
            if not isinstance(nonce, str) or not hmac.compare_digest(nonce, expected_nonce):
                raise IdTokenError("NONCE_MISMATCH", "nonce does not match the requested action")

        auth_time = claims.get("auth_time")
        if not_before is not None or max_age_s is not None:
            if not _is_int(auth_time):
                raise IdTokenError("MISSING_CLAIM", "auth_time is missing")
            if not_before is not None and auth_time < not_before - AUTH_TIME_SKEW_SECONDS:
                raise IdTokenError("STALE_AUTH", "authentication predates this approval")
            if max_age_s is not None and now - auth_time > max_age_s:
                raise IdTokenError("STALE_AUTH", f"authentication older than {max_age_s}s")

        acr_required = require_acr if require_acr is not None else self.required_acr
        acr = claims.get("acr")
        if acr_required and acr != acr_required:
            raise IdTokenError("ACR_MISMATCH", f"acr {acr!r} != {acr_required!r}")

        return IdClaims(
            sub=claims["sub"], iss=claims["iss"], aud=aud, exp=claims["exp"], iat=claims["iat"],
            jti=claims.get("jti"), nonce=nonce, auth_time=auth_time, acr=acr, amr=claims.get("amr"),
        )

    # ── internals ───────────────────────────────────────────────────────────

    def _signing_key(self, kid: str):
        jwk = self._find_jwk(self.jwks(), kid)
        if jwk is None:
            jwk = self._find_jwk(self.jwks(force=True), kid)
        if jwk is None:
            raise IdTokenError("BAD_SIGNATURE", f"unknown kid {kid!r}")
        if jwk.get("kty") != "RSA" or jwk.get("alg", ALLOWED_ALG) != ALLOWED_ALG \
                or jwk.get("use", "sig") != "sig":
            raise IdTokenError("BAD_ALG", f"key {kid!r} is not an RS256 signing key")
        try:
            return jwt.PyJWK(jwk, algorithm=ALLOWED_ALG).key
        except jwt.PyJWTError as exc:
            raise IdTokenError("BAD_SIGNATURE", f"unusable JWK {kid!r}: {exc}") from exc

    @staticmethod
    def _find_jwk(jwks: dict, kid: str) -> Optional[dict]:
        for jwk in jwks.get("keys", []):
            if isinstance(jwk, dict) and jwk.get("kid") == kid:
                return jwk
        return None

    def _forget_device(self, device_code: str) -> None:
        with self._lock:
            self._device_intervals.pop(device_code, None)

    def _basic_auth(self) -> str:
        if not self.configured:
            raise WorldError("WORLD_CLIENT_ID and WORLD_CLIENT_SECRET must be set")
        # RFC 6749 section 2.3.1: form-urlencode id and secret before base64.
        raw = f"{quote(self.client_id, safe='')}:{quote(self.client_secret, safe='')}"
        return "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")

    def _token_request(self, data: dict) -> tuple[int, dict]:
        return self._post_form(self.discovery()["token_endpoint"], data)

    def _post_form(self, url: str, data: dict) -> tuple[int, dict]:
        headers = {"Authorization": self._basic_auth(), "Accept": "application/json"}
        try:
            resp = self.http.post(url, data=data, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise WorldError(f"POST {url} failed: {exc}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        return resp.status_code, body

    def _get_json(self, url: str) -> dict:
        try:
            resp = self.http.get(url, timeout=HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise WorldError(f"GET {url} failed: {exc}") from exc
        if resp.status_code != 200:
            raise WorldError(f"GET {url} returned {resp.status_code}", status=resp.status_code)
        try:
            body = resp.json()
        except ValueError as exc:
            raise WorldError(f"GET {url} did not return JSON") from exc
        if not isinstance(body, dict):
            raise WorldError(f"GET {url} did not return a JSON object")
        return body
