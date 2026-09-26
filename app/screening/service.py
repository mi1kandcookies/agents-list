"""
service.py - screen one payment hop and record the verdict
(docs/decisions/0001-custody-chain.md §4).

    from app.screening.service import screen
    v = screen("milestone.release", chain_address=payee, amount_micro=10_000_000,
               engagement_id="ENG-…", agent_id=agent.id)
    v["verdict"]   # PAY | CAP | ASK_HUMAN | REFUSE

Payments settle on Sepolia, but the provider only has mainnet risk data, so
the Sepolia address is screened as a mapped mainnet address:
    1. agents.screening_address of the agent being paid, else
    2. SCREENING_ADDRESS_MAP: a JSON object {sepolia: mainnet}, given inline
       or as a path to a JSON file.
An unmapped address is REFUSE (fail_closed, UNMAPPED_ADDRESS).

Calls per hop:
    payee.onboard                  Deep Scan Address (toxic-score)
    every other hop                Quick Scan Address
    money-moving hops              Scan Token on mainnet USDC (cached 1 h)
    typed_data given / signature   Scan Message with the payload rebuilt for
                                   mainnet (chainId 1, mainnet USDC domain,
                                   mapped addresses; an unmapped signer is
                                   sent as-is, only the payee must be mapped)

Every verdict is persisted as a Screening row. Tests and callers that want a
different screener set ``app.extensions["screener"]`` (e.g. FakeScreener).
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, TypedDict

from app.screening import policy
from app.screening.intercepta import MAINNET_CHAIN_ID, InterceptaClient, InterceptaError

HOPS = frozenset({"payee.onboard", "payer.check", "engagement.fund", "milestone.release",
                  "subhire.hop", "signature"})
MONEY_HOPS = frozenset({"engagement.fund", "milestone.release", "subhire.hop", "signature"})
PROVIDER = "intercepta"
NETWORK = f"eip155:{MAINNET_CHAIN_ID}"
MAINNET_USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
MAINNET_USDC_DOMAIN = {"name": "USD Coin", "version": "2"}
VERDICT_TTL_SECONDS = 300
TOKEN_CACHE_SECONDS = 3600

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ERROR_REASONS = {
    "MISSING_KEY": "PROVIDER_NOT_CONFIGURED",
    "TIMEOUT": "PROVIDER_TIMEOUT",
    "UNREACHABLE": "PROVIDER_UNREACHABLE",
    "HTTP_ERROR": "PROVIDER_HTTP_ERROR",
    "BAD_RESPONSE": "PROVIDER_BAD_RESPONSE",
}


class Verdict(TypedDict):
    id: str
    hop: str
    subject: dict
    verdict: str
    cap_micro: Optional[int]
    reasons: list
    signals: dict
    provider: str
    fail_closed: bool
    latency_ms: int
    created_at: int
    expires_at: int


# ── token scan cache ─────────────────────────────────────────────────────────
_token_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_token_lock = threading.Lock()
ADDRESS_CACHE_SECONDS = 300


def clear_token_cache() -> None:
    with _token_lock:
        _token_cache.clear()


def _cached_token_scan(client: InterceptaClient, address: str) -> dict:
    key = (MAINNET_CHAIN_ID, address.lower())
    now = time.monotonic()
    with _token_lock:
        hit = _token_cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    body = client.scan_token(address, chain_id=MAINNET_CHAIN_ID)  # errors are never cached
    with _token_lock:
        _token_cache[key] = (now + TOKEN_CACHE_SECONDS, body)
    return body


def _cached_address_scan(client: InterceptaClient, source: str, address: str) -> dict:
    """Reuse successful address evidence briefly without caching failures.

    Address risk is provider evidence, not an authorization decision: every
    call still creates a fresh ``Screening`` row and re-runs local policy.
    Keeping the cache on the provider client prevents test fixtures or a
    changed provider client from sharing evidence across applications.
    """
    cache = getattr(client, "_agents_list_address_cache", None)
    lock = getattr(client, "_agents_list_address_cache_lock", None)
    if cache is None or lock is None:
        cache = {}
        lock = threading.Lock()
        setattr(client, "_agents_list_address_cache", cache)
        setattr(client, "_agents_list_address_cache_lock", lock)
    key = (source, address.lower())
    now = time.monotonic()
    with lock:
        hit = cache.get(key)
        if hit and hit[0] > now:
            return copy.deepcopy(hit[1])
    if source == "deep-scan":
        body = client.deep_scan_address(address)
    else:
        body = client.quick_scan_address(address)
    with lock:
        cache[key] = (now + ADDRESS_CACHE_SECONDS, copy.deepcopy(body))
    return body


# ── address mapping ──────────────────────────────────────────────────────────

def load_address_map(raw: Optional[str] = None) -> dict[str, str]:
    """SCREENING_ADDRESS_MAP as {lower sepolia: lower mainnet}. Raises ValueError
    on unreadable JSON or bad addresses (callers fail closed)."""
    raw = (os.environ.get("SCREENING_ADDRESS_MAP", "") if raw is None else raw).strip()
    if not raw:
        return {}
    if not raw.startswith("{"):
        with open(raw, encoding="utf-8") as fh:
            raw = fh.read()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("SCREENING_ADDRESS_MAP must be a JSON object")
    out = {}
    for src, dst in data.items():
        if not (isinstance(src, str) and isinstance(dst, str)
                and _ADDRESS.match(src) and _ADDRESS.match(dst)):
            raise ValueError(f"SCREENING_ADDRESS_MAP entry {src!r} is not address -> address")
        out[src.lower()] = dst.lower()
    return out


def _find_agent(agent_id):
    from app.extensions import db
    from app.models import Agent
    if isinstance(agent_id, str) and agent_id.upper().startswith("AGT-"):
        return Agent.query.filter_by(public_id=agent_id.upper()).first()
    if isinstance(agent_id, str) and agent_id.isdigit():
        agent_id = int(agent_id)
    if isinstance(agent_id, int) and not isinstance(agent_id, bool):
        return db.session.get(Agent, agent_id)
    return None


def mainnet_typed_data(typed_data: dict, *, resolve: Callable[[str], Optional[str]],
                       payment_token: Optional[str]) -> dict:
    """Rebuild an EIP-712 payload for the provider's mainnet view: chainId 1;
    the Sepolia payment token becomes mainnet USDC (its domain name/version
    too); every top-level `address` field of the primary type is mapped."""
    out = copy.deepcopy(typed_data)
    domain = out.setdefault("domain", {})
    domain["chainId"] = int(MAINNET_CHAIN_ID)
    contract = str(domain.get("verifyingContract") or "").lower()
    if contract and payment_token and contract == payment_token.lower():
        domain.update(MAINNET_USDC_DOMAIN, verifyingContract=MAINNET_USDC)
    fields = (out.get("types") or {}).get(out.get("primaryType"), [])
    message = out.get("message") or {}
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = field.get("name")
        if field.get("type") == "address" and isinstance(message.get(name), str):
            mapped = resolve(message[name])
            if mapped:
                message[name] = mapped
    return out


def _payment_token() -> Optional[str]:
    try:
        from chain.config import get_address
        return get_address("USDC")
    except Exception:
        return None


# ── screener ─────────────────────────────────────────────────────────────────

class InterceptaScreener:
    provider = PROVIDER

    def __init__(self, client: Optional[InterceptaClient] = None,
                 address_map: Optional[dict[str, str]] = None):
        self._client = client
        self._address_map = address_map

    def screen(self, hop, *, chain_address, amount_micro, engagement_id=None,
               agent_id=None, typed_data=None) -> Verdict:
        if hop not in HOPS:
            raise ValueError(f"unknown screening hop {hop!r}")
        started = time.monotonic()
        agent = None
        try:
            agent = _find_agent(agent_id)
        except Exception:
            agent = None
        state: dict[str, Any] = {"screened": None, "findings": [], "raw": {}, "signals": {}}
        try:
            self._run(hop, chain_address, amount_micro, agent, typed_data, state)
        except Exception as exc:  # anything unexpected still refuses
            state["findings"].append(policy.fail_closed(
                "SCREENING_ERROR", f"Screening failed: {type(exc).__name__}", "screening"))
        latency_ms = int((time.monotonic() - started) * 1000)
        return self._persist(hop, chain_address, engagement_id, agent_id, agent, state, latency_ms)

    def _run(self, hop, chain_address, amount_micro, agent, typed_data, state) -> None:
        findings, raw = state["findings"], state["raw"]
        try:
            th = policy.Thresholds.from_env()
            address_map = (self._address_map if self._address_map is not None
                           else load_address_map())
            client = self._client or InterceptaClient()
        except (ValueError, OSError) as exc:
            state["thresholds"] = policy.Thresholds()
            findings.append(policy.fail_closed(
                "SCREENING_CONFIG_INVALID", f"Screening configuration invalid: {exc}", "config"))
            return
        state["thresholds"] = th

        if hop == "signature" and not typed_data:
            findings.append(policy.fail_closed(
                "MISSING_TYPED_DATA", "Signature screening needs the EIP-712 payload",
                "scan-message"))
            return
        if typed_data is not None and not isinstance(typed_data, dict):
            findings.append(policy.fail_closed(
                "INVALID_TYPED_DATA", "typed_data must be an EIP-712 object", "scan-message"))
            return
        if not isinstance(chain_address, str) or not _ADDRESS.match(chain_address):
            findings.append(policy.fail_closed(
                "INVALID_ADDRESS", f"{chain_address!r} is not a 0x address", "screening"))
            return
        subject = chain_address.lower()

        def resolve(address: str) -> Optional[str]:
            address = address.lower()
            if address == subject and agent is not None and agent.screening_address:
                return agent.screening_address.lower()
            return address_map.get(address)

        screened = resolve(subject)
        if not screened or not _ADDRESS.match(screened):
            findings.append(policy.fail_closed(
                "UNMAPPED_ADDRESS",
                f"No mainnet screening address for {subject}; set the agent's screening "
                "address or SCREENING_ADDRESS_MAP", "screening"))
            return
        state["screened"] = screened

        try:
            source = "deep-scan" if hop == "payee.onboard" else "quick-scan"
            body = _cached_address_scan(client, source, screened)
            raw[source] = body
            state["signals"]["toxic_score"] = body["toxicScore"]
            state["signals"]["traits"] = [t["name"] for t in body["traits"]]
            findings.extend(policy.assess_address_scan(body, source, th))

            if hop in MONEY_HOPS:
                token = _cached_token_scan(client, MAINNET_USDC)
                raw["scan-token"] = token
                findings.extend(policy.assess_token_scan(token))

            if typed_data:
                rebuilt = mainnet_typed_data(typed_data, resolve=resolve,
                                             payment_token=_payment_token())
                message = rebuilt.get("message") or {}
                owner = message.get("from") or message.get("owner") or screened
                raw["scan-message-request"] = {"from": owner, "chainId": MAINNET_CHAIN_ID,
                                               "message": rebuilt}
                body = client.scan_message(owner=owner, typed_data=rebuilt)
                raw["scan-message"] = body
                state["signals"]["risk_group"] = body["riskGroup"]
                findings.extend(policy.assess_signature_scan(body))
        except InterceptaError as exc:
            code = _ERROR_REASONS[exc.code]
            if exc.code == "HTTP_ERROR" and exc.status is not None:
                code = f"{code}_{exc.status}"
            raw["error"] = {"code": exc.code, "status": exc.status, "message": exc.message,
                            "body": exc.body}
            findings.append(policy.fail_closed(code, f"Intercepta: {exc.message}", PROVIDER))

    def _persist(self, hop, chain_address, engagement_id, agent_id, agent, state,
                 latency_ms) -> Verdict:
        from app.extensions import db
        from app.models import Screening

        th = state.get("thresholds") or policy.Thresholds()
        decision = policy.decide(state["findings"], th)
        now = datetime.now(timezone.utc)
        raw = dict(state["raw"])
        risk_group = state["signals"].get("risk_group")
        if agent is None and agent_id is not None:
            raw["agent_ref"] = str(agent_id)
        row = Screening(
            hop=hop, engagement_id=engagement_id, agent_id=agent.id if agent else None,
            chain_address=str(chain_address).lower()[:64],
            screened_address=state["screened"], network=NETWORK,
            verdict=decision.verdict, cap_micro=decision.cap_micro, reasons=decision.reasons,
            toxic_score=_int_or_none(state["signals"].get("toxic_score")),
            traits=state["signals"].get("traits", []),
            risk_group=risk_group[:32] if risk_group else None, raw=raw or None,
            provider=PROVIDER, fail_closed=decision.fail_closed, latency_ms=latency_ms,
            created_at=now, expires_at=now + timedelta(seconds=VERDICT_TTL_SECONDS),
        )
        db.session.add(row)
        db.session.commit()
        return verdict_from_row(row)


def _int_or_none(value) -> Optional[int]:
    return None if value is None else int(round(value))


def _unix(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _token_risks(raw: Optional[dict]) -> list:
    token = (raw or {}).get("scan-token")
    if not isinstance(token, dict):
        return []
    return [{
        "token": (token.get("token") or {}).get("address", MAINNET_USDC),
        "action": token.get("action"),
        "risk_level": token.get("riskLevel"),
        "detectors": [d.get("code") for d in token.get("detectors", []) if isinstance(d, dict)],
    }]


def verdict_from_row(row) -> Verdict:
    """The §4 verdict dict for a stored Screening row."""
    agent_ref = None
    if row.agent_id is not None:
        from app.extensions import db
        from app.models import Agent
        agent = db.session.get(Agent, row.agent_id)
        agent_ref = (agent.public_id or agent.id) if agent else row.agent_id
    elif isinstance(row.raw, dict):
        agent_ref = row.raw.get("agent_ref")
    return Verdict(
        id=row.id, hop=row.hop,
        subject={"chain_address": row.chain_address, "screened_address": row.screened_address,
                 "network": row.network, "agent_id": agent_ref},
        verdict=row.verdict, cap_micro=row.cap_micro, reasons=list(row.reasons or []),
        signals={"toxic_score": row.toxic_score, "traits": list(row.traits or []),
                 "risk_group": row.risk_group, "token_risks": _token_risks(row.raw)},
        provider=row.provider, fail_closed=bool(row.fail_closed), latency_ms=row.latency_ms or 0,
        created_at=_unix(row.created_at), expires_at=_unix(row.expires_at),
    )


def get_screener():
    """The app's screener: ``app.extensions["screener"]`` if set, else Intercepta."""
    from flask import current_app
    screener = current_app.extensions.get("screener")
    if screener is None:
        screener = InterceptaScreener()
        current_app.extensions["screener"] = screener
    return screener


def screen(hop, *, chain_address, amount_micro, engagement_id=None, agent_id=None,
           typed_data=None) -> Verdict:
    return get_screener().screen(hop, chain_address=chain_address, amount_micro=amount_micro,
                                 engagement_id=engagement_id, agent_id=agent_id,
                                 typed_data=typed_data)
