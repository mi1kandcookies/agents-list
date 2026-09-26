"""Creation and integrity helpers for immutable paid-task intents."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from app.models import HireIntent

POLICY_VERSION = "hire-intent-v1"
INTENT_TTL = timedelta(minutes=5)


def task_hash(task: str) -> str:
    return "0x" + hashlib.sha256(str(task).encode("utf-8")).hexdigest()


def _unix(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _canonical(fields: dict) -> bytes:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def immutable_fields(*, agent_public_id: str, ens_name: str | None, endpoint_path: str,
                     task_hash_value: str, network: str, token_address: str,
                     pay_to: str, amount_micro: int, expires_at: datetime) -> dict:
    return {
        "policy_version": POLICY_VERSION,
        "agent_public_id": agent_public_id,
        "ens_name": ens_name or "",
        "endpoint_path": endpoint_path,
        "task_hash": task_hash_value,
        "network": network,
        "token_address": token_address.lower(),
        "pay_to": pay_to.lower(),
        "amount_micro": int(amount_micro),
        "expires_at": _unix(expires_at),
    }


def intent_hash(fields: dict) -> str:
    return "0x" + hashlib.sha256(_canonical(fields)).hexdigest()


def create(*, agent, ens_name: str | None, endpoint_path: str, task: str,
           network: str, token_address: str, pay_to: str, amount_micro: int,
           expires_at: datetime | None = None) -> HireIntent:
    expires_at = expires_at or datetime.now(timezone.utc) + INTENT_TTL
    task_digest = task_hash(task)
    fields = immutable_fields(agent_public_id=agent.public_id, ens_name=ens_name,
                               endpoint_path=endpoint_path, task_hash_value=task_digest,
                               network=network, token_address=token_address,
                               pay_to=pay_to, amount_micro=amount_micro,
                               expires_at=expires_at)
    return HireIntent(
        intent_hash=intent_hash(fields), agent_id=agent.id, agent_public_id=agent.public_id,
        ens_name=ens_name, endpoint_path=endpoint_path, task_text=task,
        task_hash=task_digest, network=network, token_address=token_address.lower(),
        pay_to=pay_to.lower(), amount_micro=int(amount_micro), expires_at=expires_at,
    )


def extension(row: HireIntent) -> dict:
    """x402 ``extensions`` payload understood by generic clients."""
    return {"hireIntent": {
        "id": row.id,
        "hash": row.intent_hash,
        "taskHash": row.task_hash,
        "expiresAt": row.to_dict()["expires_at"],
        "policyVersion": POLICY_VERSION,
    }}


def matches_snapshot(row: HireIntent, *, agent_public_id: str, ens_name: str | None,
                     endpoint_path: str, network: str, token_address: str,
                     pay_to: str, amount_micro: int) -> bool:
    """Return whether the current resolution still matches the quoted intent."""
    fields = immutable_fields(
        agent_public_id=agent_public_id,
        ens_name=ens_name,
        endpoint_path=endpoint_path,
        task_hash_value=row.task_hash,
        network=network,
        token_address=token_address,
        pay_to=pay_to,
        amount_micro=amount_micro,
        expires_at=row.expires_at,
    )
    return row.intent_hash == intent_hash(fields)
