"""app.identity.world: WorldClient against an in-process fake OIDC provider.

No network: every HTTP call goes to FakeProvider, which serves discovery,
JWKS, the token endpoint and the device authorization endpoint.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.identity import pkce
from app.identity.world import (
    DEVICE_GRANT, ID_TOKEN_ERROR_CODES, DevicePoll, DeviceStart, IdClaims, IdTokenError,
    WorldClient, WorldError,
)

ISSUER = "https://idp.test"
CLIENT_ID = "app_client"
CLIENT_SECRET = "s3cret/with:chars"
REDIRECT = "https://agents.test/auth/world/callback"
ACR = "https://world.org/oidc/acr/orb-v3"
NOW = 1_800_000_000
NONCE = "n" * 43


def _rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


KEY = _rsa_key()
OTHER_KEY = _rsa_key()


def _jwk(private_key, kid):
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return jwk


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeProvider:
    """Stands in for `requests`: routes get()/post() to canned endpoints."""

    def __init__(self):
        self.discovery = {
            "issuer": ISSUER,
            "authorization_endpoint": ISSUER + "/api/v1/authorize",
            "token_endpoint": ISSUER + "/api/v1/token",
            "device_authorization_endpoint": ISSUER + "/api/v1/device_authorization",
            "jwks_uri": ISSUER + "/.well-known/jwks.json",
        }
        self.keys = [_jwk(KEY, "k1")]
        self.token_responses: list[tuple[int, dict]] = []
        self.device_response = (200, {
            "device_code": "dev-123", "user_code": "ABCD-EFGH",
            "verification_uri": ISSUER + "/device",
            "verification_uri_complete": ISSUER + "/device?user_code=ABCD-EFGH",
            "expires_in": 600, "interval": 5,
        })
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict, dict]] = []

    def get(self, url, timeout=None):
        self.gets.append(url)
        if url == ISSUER + "/.well-known/openid-configuration":
            return FakeResponse(200, self.discovery)
        if url == self.discovery["jwks_uri"]:
            return FakeResponse(200, {"keys": list(self.keys)})
        return FakeResponse(404, {})

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append((url, dict(data or {}), dict(headers or {})))
        if url == self.discovery["token_endpoint"]:
            return FakeResponse(*self.token_responses.pop(0))
        if url == self.discovery["device_authorization_endpoint"]:
            return FakeResponse(*self.device_response)
        return FakeResponse(404, {})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("WORLD_ISSUER", "WORLD_CLIENT_ID", "WORLD_CLIENT_SECRET",
                "WORLD_REDIRECT_URI", "WORLD_REQUIRED_ACR"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def idp():
    return FakeProvider()


@pytest.fixture()
def client(idp):
    c = WorldClient(issuer=ISSUER, client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                    redirect_uri=REDIRECT, http=idp)
    c.clock = lambda: NOW
    return c


def claims(**overrides):
    base = {
        "iss": ISSUER, "sub": "pairwise-sub-1", "aud": CLIENT_ID,
        "exp": NOW + 300, "iat": NOW - 10, "auth_time": NOW - 10,
        "jti": "jti-1", "nonce": NONCE, "acr": ACR, "amr": ["orb"],
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not DROP}


DROP = object()


def sign(payload, *, key=KEY, kid="k1", alg="RS256"):
    return jwt.encode(payload, key, algorithm=alg, headers={"kid": kid} if kid else None)


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def alg_none_token():
    return f"{_b64({'alg': 'none', 'kid': 'k1', 'typ': 'JWT'})}.{_b64(claims())}."


def hs256_confusion_token():
    """HS256 token whose HMAC secret is the provider's RSA public key PEM."""
    pem = KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    header = _b64({"alg": "HS256", "kid": "k1", "typ": "JWT"})
    body = _b64(claims())
    sig = hmac.new(pem, f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"


def tampered_token():
    header, body, sig = sign(claims()).split(".")
    return f"{header}.{_b64(claims(sub='someone-else'))}.{sig}"


VALIDATE_DEFAULTS = {"expected_nonce": NONCE, "not_before": NOW - 60, "max_age_s": 300}

# (case id, token factory, validate kwargs overrides, expected code)
REJECTIONS = [
    ("alg-none", alg_none_token, {}, "BAD_ALG"),
    ("hs256-key-confusion", hs256_confusion_token, {}, "BAD_ALG"),
    ("wrong-key", lambda: sign(claims(), key=OTHER_KEY), {}, "BAD_SIGNATURE"),
    ("tampered-payload", tampered_token, {}, "BAD_SIGNATURE"),
    ("unknown-kid", lambda: sign(claims(), kid="nope"), {}, "BAD_SIGNATURE"),
    ("no-kid", lambda: sign(claims(), kid=None), {}, "BAD_SIGNATURE"),
    ("garbage", lambda: "not.a.jwt", {}, "BAD_SIGNATURE"),
    ("wrong-issuer", lambda: sign(claims(iss="https://evil.test")), {}, "BAD_ISSUER"),
    ("wrong-audience", lambda: sign(claims(aud="other_client")), {}, "BAD_AUDIENCE"),
    ("multi-aud-no-azp", lambda: sign(claims(aud=[CLIENT_ID, "other"])), {}, "BAD_AUDIENCE"),
    ("multi-aud-bad-azp", lambda: sign(claims(aud=[CLIENT_ID, "other"], azp="other")), {},
     "BAD_AUDIENCE"),
    ("azp-mismatch", lambda: sign(claims(azp="other")), {}, "BAD_AUDIENCE"),
    ("expired", lambda: sign(claims(exp=NOW - 61)), {}, "EXPIRED"),
    ("iat-in-future", lambda: sign(claims(iat=NOW + 61)), {}, "EXPIRED"),
    ("nonce-mismatch", lambda: sign(claims(nonce="x" * 43)), {}, "NONCE_MISMATCH"),
    ("nonce-missing", lambda: sign(claims(nonce=DROP)), {}, "NONCE_MISMATCH"),
    ("auth-before-flow", lambda: sign(claims(auth_time=NOW - 66)), {}, "STALE_AUTH"),
    ("auth-too-old", lambda: sign(claims(auth_time=NOW - 301)),
     {"not_before": None}, "STALE_AUTH"),
    ("acr-mismatch", lambda: sign(claims(acr="urn:weak")), {"require_acr": ACR}, "ACR_MISMATCH"),
    ("acr-missing", lambda: sign(claims(acr=DROP)), {"require_acr": ACR}, "ACR_MISMATCH"),
    ("missing-sub", lambda: sign(claims(sub=DROP)), {}, "MISSING_CLAIM"),
    ("missing-exp", lambda: sign(claims(exp=DROP)), {}, "MISSING_CLAIM"),
    ("missing-iat", lambda: sign(claims(iat=DROP)), {}, "MISSING_CLAIM"),
    ("missing-aud", lambda: sign(claims(aud=DROP)), {}, "MISSING_CLAIM"),
    ("missing-auth-time", lambda: sign(claims(auth_time=DROP)), {}, "MISSING_CLAIM"),
    ("string-exp", lambda: sign(claims(exp=str(NOW + 300))), {}, "MISSING_CLAIM"),
]


@pytest.mark.parametrize("token_factory,overrides,code",
                         [r[1:] for r in REJECTIONS], ids=[r[0] for r in REJECTIONS])
def test_validate_rejects(client, token_factory, overrides, code):
    with pytest.raises(IdTokenError) as exc:
        client.validate_id_token(token_factory(), **{**VALIDATE_DEFAULTS, **overrides})
    assert exc.value.code == code


def test_every_error_code_is_exercised():
    assert {r[3] for r in REJECTIONS} == ID_TOKEN_ERROR_CODES


def test_validate_accepts_good_token(client):
    got = client.validate_id_token(sign(claims()), require_acr=ACR, **VALIDATE_DEFAULTS)
    assert got == IdClaims(sub="pairwise-sub-1", iss=ISSUER, aud=CLIENT_ID, exp=NOW + 300,
                           iat=NOW - 10, jti="jti-1", nonce=NONCE, auth_time=NOW - 10,
                           acr=ACR, amr=["orb"])


@pytest.mark.parametrize("overrides", [
    {"exp": NOW - 59},                                   # inside exp leeway
    {"iat": NOW + 59},                                   # inside iat leeway
    {"auth_time": NOW - 64},                             # not_before - 5 s skew
    {"aud": [CLIENT_ID, "other"], "azp": CLIENT_ID},     # multi-aud with azp
    {"aud": [CLIENT_ID]},                                # single-element list, no azp
])
def test_validate_boundaries_accept(client, overrides):
    client.validate_id_token(sign(claims(**overrides)), **VALIDATE_DEFAULTS)


def test_device_flow_without_nonce_skips_binding(client):
    got = client.validate_id_token(sign(claims(nonce=DROP)), expected_nonce=None,
                                   not_before=NOW - 60, max_age_s=300)
    assert got.nonce is None


def test_required_acr_from_env(idp, monkeypatch):
    monkeypatch.setenv("WORLD_REQUIRED_ACR", ACR)
    c = WorldClient(issuer=ISSUER, client_id=CLIENT_ID, client_secret=CLIENT_SECRET, http=idp)
    c.clock = lambda: NOW
    c.validate_id_token(sign(claims()), **VALIDATE_DEFAULTS)
    with pytest.raises(IdTokenError) as exc:
        c.validate_id_token(sign(claims(acr="urn:weak")), **VALIDATE_DEFAULTS)
    assert exc.value.code == "ACR_MISMATCH"


def test_rejects_jwk_that_is_not_rs256(client, idp):
    idp.keys = [{**_jwk(KEY, "k1"), "alg": "RS512"}]
    with pytest.raises(IdTokenError) as exc:
        client.validate_id_token(sign(claims()), **VALIDATE_DEFAULTS)
    assert exc.value.code == "BAD_ALG"


# ── discovery / JWKS caching ──────────────────────────────────────────────

def test_discovery_cached_for_an_hour(client, idp):
    t = [NOW]
    client.clock = lambda: t[0]
    client.discovery()
    client.discovery()
    t[0] += 3599
    client.discovery()
    assert len(idp.gets) == 1
    t[0] += 2
    client.discovery()
    assert len(idp.gets) == 2


def test_discovery_rejects_issuer_mismatch(client, idp):
    idp.discovery["issuer"] = "https://evil.test"
    with pytest.raises(WorldError):
        client.discovery()


def test_jwks_cached_and_refetched_once_on_unknown_kid(client, idp):
    jwks_uri = idp.discovery["jwks_uri"]
    t = [NOW]
    client.clock = lambda: t[0]
    client.validate_id_token(sign(claims()), **VALIDATE_DEFAULTS)
    client.validate_id_token(sign(claims()), **VALIDATE_DEFAULTS)
    assert idp.gets.count(jwks_uri) == 1

    # Provider rotates to k2. After the refetch cooldown, a k2 token triggers
    # exactly one refetch and then validates.
    idp.keys = [_jwk(KEY, "k1"), _jwk(OTHER_KEY, "k2")]
    t[0] += 61
    client.validate_id_token(sign(claims(), key=OTHER_KEY, kid="k2"), **VALIDATE_DEFAULTS)
    assert idp.gets.count(jwks_uri) == 2

    # An unknown kid refetches at most once, and not again inside the cooldown.
    t[0] += 61
    for _ in range(3):
        with pytest.raises(IdTokenError) as exc:
            client.validate_id_token(sign(claims(), kid="k9"), **VALIDATE_DEFAULTS)
        assert exc.value.code == "BAD_SIGNATURE"
    assert idp.gets.count(jwks_uri) == 3


# ── web flow ──────────────────────────────────────────────────────────────

def test_authorize_url_params(client):
    verifier = pkce.new_verifier()
    url = client.authorize_url(state="st", nonce=NONCE, code_challenge=pkce.challenge_s256(verifier),
                               acr_values=ACR)
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == ISSUER + "/api/v1/authorize"
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q == {
        "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT,
        "scope": "openid", "state": "st", "nonce": NONCE,
        "code_challenge": pkce.challenge_s256(verifier), "code_challenge_method": "S256",
        "prompt": "login", "max_age": "0", "acr_values": ACR,
    }


def test_authorize_url_omits_acr_by_default_and_requires_binding(client):
    q = parse_qs(urlsplit(client.authorize_url(state="s", nonce="n", code_challenge="c")).query)
    assert "acr_values" not in q
    with pytest.raises(ValueError):
        client.authorize_url(state="s", nonce="", code_challenge="c")


def test_pkce_s256_rfc7636_vector():
    # RFC 7636 appendix B
    assert pkce.challenge_s256("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    v = pkce.new_verifier()
    assert 43 <= len(v) <= 128 and v != pkce.new_verifier()


def _basic(headers):
    scheme, value = headers["Authorization"].split(" ", 1)
    assert scheme == "Basic"
    return base64.b64decode(value).decode()


def test_exchange_code_uses_client_secret_basic(client, idp):
    idp.token_responses.append((200, {"id_token": "tok", "token_type": "Bearer"}))
    assert client.exchange_code("code-1", "verifier-1")["id_token"] == "tok"
    url, data, headers = idp.posts[-1]
    assert url == ISSUER + "/api/v1/token"
    assert data == {"grant_type": "authorization_code", "code": "code-1",
                    "redirect_uri": REDIRECT, "code_verifier": "verifier-1"}
    assert "client_secret" not in data
    # id and secret are form-urlencoded before base64 (RFC 6749 section 2.3.1)
    assert _basic(headers) == "app_client:s3cret%2Fwith%3Achars"


def test_exchange_code_error(client, idp):
    idp.token_responses.append((400, {"error": "invalid_grant"}))
    with pytest.raises(WorldError) as exc:
        client.exchange_code("bad", "v")
    assert exc.value.error == "invalid_grant"


def test_unconfigured_client_refuses_token_calls(idp):
    c = WorldClient(issuer=ISSUER, redirect_uri=REDIRECT, http=idp)
    assert not c.configured
    with pytest.raises(WorldError):
        c.exchange_code("c", "v")


def test_config_from_env(monkeypatch, idp):
    monkeypatch.setenv("WORLD_CLIENT_ID", "env_id")
    monkeypatch.setenv("WORLD_CLIENT_SECRET", "env_secret")
    monkeypatch.setenv("WORLD_REDIRECT_URI", REDIRECT)
    c = WorldClient(http=idp)
    assert (c.issuer, c.client_id, c.client_secret, c.redirect_uri) == \
        ("https://sandbox.auth.world.org", "env_id", "env_secret", REDIRECT)


# ── device flow ───────────────────────────────────────────────────────────

def test_device_authorize(client, idp):
    start = client.device_authorize(nonce=NONCE)
    assert start == DeviceStart("dev-123", "ABCD-EFGH", ISSUER + "/device",
                                ISSUER + "/device?user_code=ABCD-EFGH", 600, 5)
    url, data, headers = idp.posts[-1]
    assert url == ISSUER + "/api/v1/device_authorization"
    assert data == {"scope": "openid", "nonce": NONCE}
    assert _basic(headers).startswith("app_client:")


def test_device_authorize_without_nonce(client, idp):
    client.device_authorize()
    assert "nonce" not in idp.posts[-1][1]


def test_device_poll_mapping(client, idp):
    client.device_authorize()
    idp.token_responses += [
        (400, {"error": "authorization_pending"}),
        (400, {"error": "slow_down"}),
        (400, {"error": "slow_down"}),
        (400, {"error": "authorization_pending"}),
        (200, {"id_token": "tok"}),
    ]
    assert client.poll_device("dev-123") == DevicePoll("pending", interval=5)
    assert client.poll_device("dev-123") == DevicePoll("slow_down", error="slow_down", interval=10)
    assert client.poll_device("dev-123") == DevicePoll("slow_down", error="slow_down", interval=15)
    assert client.poll_device("dev-123") == DevicePoll("pending", interval=15)
    assert client.poll_device("dev-123") == DevicePoll("approved", id_token="tok")
    _, data, _ = idp.posts[-1]
    assert data == {"grant_type": DEVICE_GRANT, "device_code": "dev-123"}


@pytest.mark.parametrize("status,body,expected", [
    (400, {"error": "access_denied"}, "denied"),
    (400, {"error": "expired_token"}, "expired"),
    (400, {"error": "invalid_grant"}, "error"),
    (500, ValueError("not json"), "error"),
    (200, {"access_token": "no id token"}, "error"),
])
def test_device_poll_terminal_states(client, idp, status, body, expected):
    idp.token_responses.append((status, body))
    assert client.poll_device("dev-x").status == expected
