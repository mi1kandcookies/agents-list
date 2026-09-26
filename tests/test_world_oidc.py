import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from app.identity.world import WorldConfig, WorldIdentityError, WorldOIDCClient, create_pkce_pair


def _keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key())
    return private_pem, {"keys": [{**__import__("json").loads(public_jwk), "kid": "world-test"}]}


def test_pkce_and_authorization_url_are_explicit():
    verifier, challenge = create_pkce_pair()
    assert len(verifier) > 40 and challenge != verifier
    client = WorldOIDCClient(
        WorldConfig("https://world.test", "client", "secret", "http://localhost/cb", "client"),
        discovery={"authorization_endpoint": "https://world.test/authorize"},
    )
    url = client.authorization_url(state="state", nonce="nonce", code_challenge=challenge)
    assert "prompt=login" in url and "acr_values=orb-v3" in url and "code_challenge_method=S256" in url


def test_rs256_id_token_validation_requires_fresh_auth_and_nonce():
    private, jwks = _keys()
    now = int(time.time())
    client = WorldOIDCClient(
        WorldConfig("https://world.test", "client", "", "http://localhost/cb", "client"),
        discovery={"issuer": "https://world.test"}, jwks=jwks,
    )
    token = jwt.encode({"iss": "https://world.test", "sub": "pairwise-1", "aud": "client",
                        "exp": now + 300, "iat": now, "auth_time": now, "jti": "jti-1",
                        "nonce": "action-hash", "acr": "orb-v3"}, private, algorithm="RS256",
                       headers={"kid": "world-test"})
    claims = client.validate_id_token(token, expected_nonce="action-hash", now=now, jwks=jwks)
    assert claims["sub"] == "pairwise-1"
    with pytest.raises(WorldIdentityError, match="nonce"):
        client.validate_id_token(token, expected_nonce="wrong", now=now, jwks=jwks)
