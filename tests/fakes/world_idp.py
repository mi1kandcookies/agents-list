"""In-process fake of the World ID OIDC provider.

Serves discovery, JWKS, token (authorization_code + device_code) and device
authorization by intercepting ``requests.get`` / ``requests.post`` for URLs
under its issuer; everything else passes through untouched. ID tokens are
real RS256 JWTs signed with a key generated per test.

    idp.mint_id_token(nonce="…", acr="…")            # any claim overridable
    code = idp.issue_code(nonce="…")                  # web flow
    idp.approve_device(device_code, nonce="…")        # device flow outcomes:
    idp.deny_device(code) / idp.expire_device(code) / idp.slow_down(code)
"""
from __future__ import annotations

import base64
import json
import secrets
import time
from urllib.parse import parse_qs, quote

import jwt
import requests
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://idp.test"
CLIENT_ID = "app_test_client"
CLIENT_SECRET = "test-secret"
REDIRECT_URI = "http://localhost/auth/world/callback"


def _response(url: str, status: int, body: dict) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(body).encode()
    resp.headers["Content-Type"] = "application/json"
    resp.url = url
    resp.encoding = "utf-8"
    return resp


class FakeWorldIdP:
    def __init__(self, issuer: str = ISSUER, client_id: str = CLIENT_ID,
                 client_secret: str = CLIENT_SECRET):
        self.issuer = issuer
        self.client_id = client_id
        self.client_secret = client_secret
        self.requests: list[dict] = []   # every intercepted call
        self.codes: dict[str, dict] = {}  # authorization code → claims
        self.devices: dict[str, dict] = {}  # device_code → {"status", "claims"}
        self.default_claims: dict = {}
        self.rotate_key()

    # ── keys and tokens ──────────────────────────────────────────────────────
    def rotate_key(self) -> str:
        """Generate a new signing key (new kid); the JWKS serves only it."""
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "kid-" + secrets.token_hex(4)
        return self.kid

    @property
    def discovery_doc(self) -> dict:
        i = self.issuer
        return {
            "issuer": i,
            "authorization_endpoint": f"{i}/authorize",
            "token_endpoint": f"{i}/token",
            "jwks_uri": f"{i}/jwks.json",
            "device_authorization_endpoint": f"{i}/device",
            "response_types_supported": ["code"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
            "code_challenge_methods_supported": ["S256"],
        }

    @property
    def jwks(self) -> dict:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self._key.public_key()))
        jwk.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    def mint_id_token(self, *, drop=(), key=None, kid=None, alg="RS256", **claims) -> str:
        """Sign an ID token. Defaults are a fresh, valid login; ``claims``
        override them (None removes one), ``drop`` removes by name, ``key``
        signs with a foreign key (bad signature), ``alg`` changes the header."""
        now = int(time.time())
        body = {
            "iss": self.issuer, "aud": self.client_id, "sub": "0x" + "5" * 64,
            "iat": now, "exp": now + 300, "auth_time": now,
            "jti": secrets.token_hex(12), "acr": None, "amr": ["orb"],
        }
        body.update(self.default_claims)
        body.update(claims)
        for name in drop:
            body.pop(name, None)
        body = {k: v for k, v in body.items() if v is not None}
        signing_key = key if key is not None else self._key
        if alg == "HS256":
            signing_key = self.client_secret
        return jwt.encode(body, signing_key, algorithm=alg, headers={"kid": kid or self.kid})

    # ── flow control ─────────────────────────────────────────────────────────
    def issue_code(self, **claims) -> str:
        code = "code-" + secrets.token_hex(8)
        self.codes[code] = claims
        return code

    def approve_device(self, device_code: str, **claims) -> None:
        self.devices[device_code].update(status="approved", claims=claims)

    def deny_device(self, device_code: str) -> None:
        self.devices[device_code]["status"] = "access_denied"

    def expire_device(self, device_code: str) -> None:
        self.devices[device_code]["status"] = "expired_token"

    def slow_down(self, device_code: str) -> None:
        self.devices[device_code]["status"] = "slow_down"

    # ── HTTP ─────────────────────────────────────────────────────────────────
    def handles(self, url: str) -> bool:
        return url.startswith(self.issuer + "/")

    def _client_ok(self, auth, headers) -> bool:
        if isinstance(auth, tuple):
            return auth == (self.client_id, self.client_secret)
        header = (headers or {}).get("Authorization", "")
        # RFC 6749 §2.3.1: id and secret are form-urlencoded before base64.
        raw = f"{quote(self.client_id, safe='')}:{quote(self.client_secret, safe='')}"
        expected = base64.b64encode(raw.encode()).decode()
        return header == f"Basic {expected}"

    def handle(self, method: str, url: str, *, data=None, headers=None, auth=None, **_) -> requests.Response:
        if isinstance(data, (str, bytes)):
            data = {k: v[0] for k, v in parse_qs(data if isinstance(data, str) else data.decode()).items()}
        data = dict(data or {})
        self.requests.append({"method": method, "url": url, "data": data, "headers": headers, "auth": auth})
        path = url[len(self.issuer):].split("?", 1)[0]
        if method == "GET" and path == "/.well-known/openid-configuration":
            return _response(url, 200, self.discovery_doc)
        if method == "GET" and path == "/jwks.json":
            return _response(url, 200, self.jwks)
        if method == "POST" and path == "/device":
            return self._device_start(url, data)
        if method == "POST" and path == "/token":
            if not self._client_ok(auth, headers):
                return _response(url, 401, {"error": "invalid_client"})
            return self._token(url, data)
        return _response(url, 404, {"error": "not_found"})

    def _device_start(self, url, data) -> requests.Response:
        device_code = "dev-" + secrets.token_hex(8)
        user_code = secrets.token_hex(2).upper() + "-" + secrets.token_hex(2).upper()
        self.devices[device_code] = {"status": "authorization_pending", "claims": {},
                                     "nonce": data.get("nonce"), "scope": data.get("scope")}
        return _response(url, 200, {
            "device_code": device_code, "user_code": user_code,
            "verification_uri": f"{self.issuer}/activate",
            "verification_uri_complete": f"{self.issuer}/activate?user_code={user_code}",
            "expires_in": 180, "interval": 5,
        })

    def _token(self, url, data) -> requests.Response:
        grant = data.get("grant_type", "")
        if grant == "authorization_code":
            claims = self.codes.pop(data.get("code"), None)
            if claims is None:
                return _response(url, 400, {"error": "invalid_grant"})
        elif grant == "urn:ietf:params:oauth:grant-type:device_code":
            device = self.devices.get(data.get("device_code"))
            if device is None:
                return _response(url, 400, {"error": "invalid_grant"})
            if device["status"] != "approved":
                return _response(url, 400, {"error": device["status"]})
            claims = dict(device["claims"])
            if device.get("nonce") and "nonce" not in claims:
                claims["nonce"] = device["nonce"]
            device["status"] = "expired_token"  # a device code is redeemable once
        else:
            return _response(url, 400, {"error": "unsupported_grant_type"})
        return _response(url, 200, {
            "access_token": secrets.token_hex(16), "token_type": "Bearer", "expires_in": 300,
            "id_token": self.mint_id_token(**claims),
        })

    def install(self, monkeypatch) -> "FakeWorldIdP":
        """Route requests.get/post for this issuer to the fake."""
        real_get, real_post = requests.get, requests.post

        def fake_get(url, *args, **kwargs):
            return self.handle("GET", url, **kwargs) if self.handles(url) else real_get(url, *args, **kwargs)

        def fake_post(url, *args, **kwargs):
            if self.handles(url):
                if args:
                    kwargs.setdefault("data", args[0])
                return self.handle("POST", url, **kwargs)
            return real_post(url, *args, **kwargs)

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", fake_post)
        for name, value in (("WORLD_ISSUER", self.issuer), ("WORLD_CLIENT_ID", self.client_id),
                            ("WORLD_CLIENT_SECRET", self.client_secret),
                            ("WORLD_REDIRECT_URI", REDIRECT_URI)):
            monkeypatch.setenv(name, value)
        return self
