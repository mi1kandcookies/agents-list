"""Official x402 v2 exact/EVM adapters used by the hiring flow.

The app does not expose a public facilitator. A private facilitator URL is
required for real settlement. The local mock mode lives in the hiring route
and is explicitly disabled unless ``HIRE_PAYMENT_MODE=mock``.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import timezone

from chain.config import get_address, get_chain_config


class X402ConfigurationError(RuntimeError):
    pass


class X402PaymentError(RuntimeError):
    pass


def network_id() -> str:
    return get_chain_config().caip2


def requirements_for_intent(intent):
    from x402.schemas import PaymentRequirements

    expires_at = intent.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return PaymentRequirements(
        scheme="exact",
        network=intent.network,
        asset=intent.token_address,
        amount=str(intent.amount_atomic),
        payTo=intent.pay_to,
        maxTimeoutSeconds=max(60, int((expires_at.timestamp() - time.time()))),
        extra={"name": "USDC", "version": "2"},
    )


def payment_required_for_intent(intent):
    from x402.schemas import PaymentRequired, ResourceInfo

    return PaymentRequired(
        resource=ResourceInfo(
            url=f"/api/hiring/intents/{intent.id}/work",
            description="QA test plan generated from the server-owned API specification",
            mimeType="application/json",
            serviceName="Agent's List specialist hire",
        ),
        accepts=[requirements_for_intent(intent)],
    )


def _server(network: str | None = None):
    url = os.environ.get("PAYMENT_FACILITATOR_URL", "").strip()
    if not url:
        raise X402ConfigurationError("PAYMENT_FACILITATOR_URL is not configured")
    from x402.http import FacilitatorConfig, HTTPFacilitatorClientSync
    from x402.mechanisms.evm.exact import ExactEvmServerScheme
    from x402.server import x402ResourceServerSync

    server = x402ResourceServerSync(HTTPFacilitatorClientSync(FacilitatorConfig(url=url)))
    server.register(network or network_id(), ExactEvmServerScheme())
    server.initialize()
    return server


@dataclass
class Settlement:
    success: bool
    transaction: str = ""
    network: str = ""
    payer: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "status": "settled" if self.success else "settlement_pending",
            "transaction": self.transaction,
            "network": self.network,
            "payer": self.payer,
            "error": self.error,
            "provider": "x402/exact",
        }


@dataclass
class Verification:
    success: bool
    payer: str = ""
    error: str = ""


def verify_payment(payload, requirements) -> Verification:
    """Run the official facilitator's read-only verification before work."""
    try:
        server = _server(requirements.network)
        verified = server.verify_payment(payload, requirements)
        if not verified.is_valid:
            return Verification(False, payer=verified.payer or "",
                                error=verified.invalid_reason or verified.invalid_message or "payment rejected")
        return Verification(True, payer=verified.payer or "")
    except Exception as exc:
        return Verification(False, error=f"x402 facilitator unavailable: {str(exc)[:180]}")


def settle_payment(payload, requirements) -> Settlement:
    """Settle only after the read-only specialist result is buffered."""
    try:
        server = _server(requirements.network)
        settled = server.settle_payment(payload, requirements)
        return Settlement(bool(settled.success), transaction=settled.transaction,
                          network=settled.network, payer=settled.payer or "",
                          error=settled.error_message or settled.error_reason or "")
    except Exception as exc:
        return Settlement(False, network=requirements.network,
                          error=f"x402 facilitator unavailable: {str(exc)[:180]}")


def verify_and_settle(payload, requirements) -> Settlement:
    """Compatibility helper for callers that do not have a handler stage."""
    verified = verify_payment(payload, requirements)
    if not verified.success:
        return Settlement(False, network=requirements.network, payer=verified.payer,
                          error=verified.error)
    return settle_payment(payload, requirements)


class GuardedEvmSigner:
    """Signer wrapper that checks the exact EIP-712 data before delegation."""

    def __init__(self, account, *, expected_from: str, expected_to: str,
                 expected_amount: int, expected_chain_id: int,
                 expected_asset: str):
        from x402.mechanisms.evm.signers import EthAccountSigner

        self._delegate = EthAccountSigner(account)
        self._expected_from = expected_from.lower()
        self._expected_to = expected_to.lower()
        self._expected_amount = int(expected_amount)
        self._expected_chain_id = int(expected_chain_id)
        self._expected_asset = expected_asset.lower()

    @property
    def address(self) -> str:
        return self._delegate.address

    def sign_typed_data(self, domain, types, primary_type, message):
        def domain_value(snake: str, camel: str):
            if isinstance(domain, dict):
                return domain.get(camel, domain.get(snake))
            return getattr(domain, snake, None)

        if primary_type != "TransferWithAuthorization":
            raise X402PaymentError("signer refused an unexpected typed-data primary type")
        if str(domain_value("chain_id", "chainId")) != str(self._expected_chain_id):
            raise X402PaymentError("signer refused an unexpected payment chain")
        if str(domain_value("verifying_contract", "verifyingContract") or "").lower() != self._expected_asset:
            raise X402PaymentError("signer refused an unexpected payment token")
        if str(message.get("from", "")).lower() != self._expected_from:
            raise X402PaymentError("signer refused an unexpected payer")
        if str(message.get("to", "")).lower() != self._expected_to:
            raise X402PaymentError("signer refused an unexpected recipient")
        if int(message.get("value", -1)) != self._expected_amount:
            raise X402PaymentError("signer refused an unexpected amount")
        if domain_value("name", "name") != "USDC" or domain_value("version", "version") != "2":
            raise X402PaymentError("signer refused an unexpected USDC EIP-712 domain")
        return self._delegate.sign_typed_data(domain, types, primary_type, message)


def make_buyer_client(intent, account, before_payment_hook=None):
    from x402 import x402ClientSync
    from x402.mechanisms.evm.exact import ExactEvmScheme

    token = intent.token_address or get_address("USDC") or ""
    signer = GuardedEvmSigner(
        account,
        expected_from=intent.payer,
        expected_to=intent.pay_to,
        expected_amount=int(intent.amount_atomic),
        expected_chain_id=intent.chain_id,
        expected_asset=token,
    )
    client = x402ClientSync()
    client.register(intent.network, ExactEvmScheme(signer=signer))
    if before_payment_hook:
        client.on_before_payment_creation(before_payment_hook)
    return client
