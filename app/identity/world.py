"""World OIDC client: PKCE auth-code and device authorization flows."""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import jwt
import requests


class WorldIdentityError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorldConfig:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    audience: str
    acr: str = "orb-v3"
    device_grant_enabled: bool = True

    @classmethod
    def from_env(cls) -> "WorldConfig":
        import os
        return cls(
            issuer=os.environ.get("WORLD_ISSUER", "https://id.world.org").rstrip("/"),
            client_id=os.environ.get("WORLD_CLIENT_ID", "").strip(),
            client_secret=os.environ.get("WORLD_CLIENT_SECRET", "").strip(),
            redirect_uri=os.environ.get("WORLD_REDIRECT_URI", "http://127.0.0.1:8090/auth/world/callback"),
            audience=os.environ.get("WORLD_AUDIENCE", "").strip(),
            acr=os.environ.get("WORLD_ACR", "orb-v3").strip() or "orb-v3",
            device_grant_enabled=os.environ.get("WORLD_DEVICE_GRANT_ENABLED", "1").lower() in {"1", "true", "yes"},
        )


@dataclass(frozen=True)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int
    interval: int


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def create_pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


class WorldOIDCClient:
    def __init__(self, config: WorldConfig | None = None, *, session=None,
                 discovery: dict | None = None, jwks: dict | None = None):
        self.config = config or WorldConfig.from_env()
        self.session = session or requests.Session()
        self._discovery = discovery
        self._jwks = jwks

    def discovery(self) -> dict:
        if self._discovery is None:
            try:
                response = self.session.get(
                    f"{self.config.issuer}/.well-known/openid-configuration", timeout=10)
                response.raise_for_status()
                self._discovery = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise WorldIdentityError(f"World OIDC discovery failed: {str(exc)[:160]}") from exc
        return self._discovery

    def authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        if not self.config.client_id:
            raise WorldIdentityError("WORLD_CLIENT_ID is not configured")
        endpoint = self.discovery().get("authorization_endpoint")
        if not endpoint:
            raise WorldIdentityError("World discovery has no authorization endpoint")
        params = {
            "client_id": self.config.client_id,
            "response_type": "code",
            "redirect_uri": self.config.redirect_uri,
            "scope": "openid",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "login",
            "acr_values": self.config.acr,
        }
        return f"{endpoint}?{urlencode(params)}"

    def _client_auth(self) -> tuple[dict, tuple[str, str] | None]:
        if self.config.client_secret:
            return {}, (self.config.client_id, self.config.client_secret)
        return {"client_id": self.config.client_id}, None

    def exchange_code(self, code: str, code_verifier: str) -> dict:
        endpoint = self.discovery().get("token_endpoint")
        if not endpoint:
            raise WorldIdentityError("World discovery has no token endpoint")
        data, auth = self._client_auth()
        data.update({
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": self.config.redirect_uri,
            "code_verifier": code_verifier,
        })
        try:
            response = self.session.post(endpoint, data=data, auth=auth, timeout=10)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise WorldIdentityError(f"World token exchange failed: {str(exc)[:160]}") from exc
        return payload

    def start_device_flow(self, *, action_hash: str | None = None) -> DeviceAuthorization:
        if not self.config.device_grant_enabled:
            raise WorldIdentityError("World device grant is disabled")
        endpoint = self.discovery().get("device_authorization_endpoint")
        if not endpoint:
            raise WorldIdentityError("World discovery has no device authorization endpoint")
        data = {"client_id": self.config.client_id, "scope": "openid"}
        if action_hash:
            data["nonce"] = action_hash
        try:
            response = self.session.post(endpoint, data=data, timeout=10)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise WorldIdentityError(f"World device authorization failed: {str(exc)[:160]}") from exc
        try:
            return DeviceAuthorization(
                device_code=payload["device_code"], user_code=payload["user_code"],
                verification_uri=payload.get("verification_uri") or payload["verification_url"],
                verification_uri_complete=payload.get("verification_uri_complete"),
                expires_in=int(payload["expires_in"]), interval=int(payload.get("interval", 5)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorldIdentityError("World device response is incomplete") from exc

    def poll_device(self, device_code: str) -> dict:
        endpoint = self.discovery().get("token_endpoint")
        if not endpoint:
            raise WorldIdentityError("World discovery has no token endpoint")
        data, auth = self._client_auth()
        data.update({"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": device_code})
        try:
            response = self.session.post(endpoint, data=data, auth=auth, timeout=10)
            if response.status_code >= 400:
                payload = response.json()
                if payload.get("error") in {"authorization_pending", "slow_down"}:
                    return payload
                response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise WorldIdentityError(f"World device polling failed: {str(exc)[:160]}") from exc

    def validate_id_token(self, id_token: str, *, expected_nonce: str | None = None,
                          now: int | None = None, jwks: dict | None = None) -> dict:
        discovery = self.discovery()
        issuer = discovery.get("issuer") or self.config.issuer
        audience = self.config.audience or self.config.client_id
        keyset = jwks or self._jwks
        if keyset is None:
            jwks_uri = discovery.get("jwks_uri")
            if not jwks_uri:
                raise WorldIdentityError("World discovery has no JWKS URI")
            try:
                response = self.session.get(jwks_uri, timeout=10)
                response.raise_for_status()
                keyset = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise WorldIdentityError(f"World JWKS fetch failed: {str(exc)[:160]}") from exc
        try:
            header = jwt.get_unverified_header(id_token)
            if header.get("alg") != "RS256" or not header.get("kid"):
                raise WorldIdentityError("World ID token must use RS256 with a key id")
            jwk = next(key for key in keyset.get("keys", []) if key.get("kid") == header["kid"])
            public_key = jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
            claims = jwt.decode(
                id_token, public_key, algorithms=["RS256"], audience=audience,
                issuer=issuer, options={"require": ["iss", "sub", "aud", "exp", "iat", "auth_time", "jti"]},
            )
        except StopIteration as exc:
            raise WorldIdentityError("World ID token key id is not trusted") from exc
        except jwt.PyJWTError as exc:
            raise WorldIdentityError(f"World ID token validation failed: {str(exc)[:160]}") from exc
        if expected_nonce is not None and claims.get("nonce") != expected_nonce:
            raise WorldIdentityError("World ID token nonce does not match the action")
        if claims.get("acr") and claims["acr"] != self.config.acr:
            raise WorldIdentityError("World ID token assurance level is not accepted")
        if now is not None and int(claims["exp"]) <= int(now):
            raise WorldIdentityError("World ID token is expired")
        return claims
