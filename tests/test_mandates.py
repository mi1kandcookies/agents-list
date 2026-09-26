from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

import pytest

from app.mandates.tokens import MandateCaveats, MandateError, issue_mandate, validate_mandate


def _keys():
    private = ec.generate_private_key(ec.SECP256R1())
    private_pem = private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    public_pem = private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    return private_pem, public_pem


def test_child_mandate_can_only_attenuate_parent():
    private, public = _keys()
    parent = issue_mandate(
        private_key=private, issuer="human-1", subject="agent-a",
        caveats=MandateCaveats(100_000, ("engineering", "data"), 3, 50_000, 2_000_000_100),
        now=2_000_000_000,
    )
    child = issue_mandate(
        private_key=private, issuer="agent-a", subject="agent-b",
        caveats=MandateCaveats(40_000, ("engineering",), 2, 10_000, 2_000_000_050),
        parent_token=parent, parent_public_key=public, now=2_000_000_000,
    )
    assert validate_mandate(child, public, now=2_000_000_000)["budget"] == 40_000
    with pytest.raises(MandateError, match="budget"):
        issue_mandate(
            private_key=private, issuer="agent-a", subject="agent-b",
            caveats=MandateCaveats(100_001, ("engineering",), 2, 10_000, 2_000_000_050),
            parent_token=parent, parent_public_key=public, now=2_000_000_000,
        )


def test_mandate_rejects_expired_token():
    private, public = _keys()
    token = issue_mandate(
        private_key=private, issuer="human-1", subject="agent-a",
        caveats=MandateCaveats(10, ("ops",), 1, 10, 100), now=1,
    )
    with pytest.raises(MandateError, match="expired"):
        validate_mandate(token, public, now=100)
