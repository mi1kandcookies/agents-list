"""Official x402 Python SDK bridge for the paying agent.

The application keeps its small resource-side verifier in :mod:`chain.x402_v2`
because the protected Flask endpoint also has to bind a mandate, persist risk
evidence, and settle through the local escrow abstraction.  The buyer-side
path, however, uses the official SDK for requirement selection, exact EVM
payload construction, and HTTP header encoding.

The SDK's ``on_before_payment_creation`` hook runs before its exact-scheme
client creates a signature.  The exact typed-data screening hook below runs
one step later, after the SDK has assembled the authorization and immediately
before the guarded signer is invoked.  Keeping both checks is intentional:
the first binds the signed request to the approved terms, and the second
gives the risk provider the actual EIP-712 message.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from x402 import x402ClientSync
from x402.http import encode_payment_signature_header
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.schemas import PaymentPayload, PaymentRequired

from chain import x402_v2
from chain.usdc import authorization_typed_data


def _domain_dict(domain: Any) -> dict[str, Any]:
    """Convert the SDK's typed domain object to eth-account's field names."""
    if isinstance(domain, dict):
        return dict(domain)
    return {
        "name": domain.name,
        "version": domain.version,
        "chainId": domain.chain_id,
        "verifyingContract": domain.verifying_contract,
    }


class _GuardedEvmSigner:
    """Adapt an eth-account key to the official SDK with a final signer gate."""

    def __init__(self, account, expected: x402_v2.Expectation, domain: dict[str, Any],
                 before_sign: Callable[[dict], object] | None):
        self._account = account
        self._expected = expected
        self._domain = domain
        self._before_sign = before_sign

    @property
    def address(self) -> str:
        return self._account.address

    def sign_typed_data(self, domain, types, primary_type: str, message: dict[str, Any]) -> bytes:
        # The official SDK gives us the exact values it is about to sign. Build
        # the same full message shape used by the resource-side verifier so the
        # screening adapter observes bytes32 nonce and integer fields exactly.
        dom = _domain_dict(domain)
        permit = dict(message)
        nonce = permit.get("nonce")
        if isinstance(nonce, bytes):
            permit["nonce"] = "0x" + nonce.hex()
        typed = authorization_typed_data(permit, dom)
        if self._before_sign is not None:
            self._before_sign(typed)
        signed = x402_v2.GuardedSigner(self._account, self._expected, self._domain)
        return bytes(signed.sign_typed_data(typed).signature)


def create_payment_payload(
    payment_required: dict[str, Any],
    account,
    *,
    expected: x402_v2.Expectation,
    domain: dict[str, Any] | None = None,
    before_sign: Callable[[dict], object] | None = None,
) -> PaymentPayload:
    """Create an official SDK payload after validating the approved terms.

    ``before_sign`` receives the exact EIP-712 object and may raise to stop the
    payment.  No signature is created when it raises.
    """
    domain = x402_v2._domain(domain)
    required = PaymentRequired.model_validate(payment_required)
    client = x402ClientSync()
    # Circle Sepolia USDC is intentionally explicit rather than a default asset
    # lookup; the official SDK's default-asset list is network-dependent.
    client.set_spend_controls(False)
    client.register(
        x402_v2.SEPOLIA_NETWORK,
        ExactEvmScheme(_GuardedEvmSigner(account, expected, domain, before_sign)),
    )

    def before_creation(ctx) -> None:
        selected = x402_v2.PaymentRequirements.from_dict(
            ctx.selected_requirements.model_dump(by_alias=True)
        )
        x402_v2.preflight(selected, expected, domain=domain)

    # This is the official SDK lifecycle gate. The exact authorization gate is
    # kept in _GuardedEvmSigner because only that point has the typed message.
    client.on_before_payment_creation(before_creation)
    return client.create_payment_payload(required)


def payment_signature_header(payload: PaymentPayload) -> str:
    """Encode a payload using the official SDK's HTTP representation."""
    return encode_payment_signature_header(payload)
