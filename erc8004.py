"""
erc8004.py — ERC-8004 Trustless Agents adapter.

The ERC-8004 draft (EIP-8004) proposes a minimal interface for on-chain
agent identity and reputation that any registry/scorer can implement:

    getIdentity(uint256 agentId) -> (address owner, string name, string endpoint)
    getScore(uint256 agentId)    -> (uint256 score, uint8 tier, uint256 tasks)
    getReputation(uint256 agentId) -> (bytes32 merkle, uint64 lastUpdate)

AgentHire's deployed ReputationContract and AgentRegistry pre-date the
draft and expose `getCreditProfile` / `getAgent` instead. This module
maps between the two so the Python SDK exposes the standard surface
even though the underlying contracts don't.

Anything consuming this adapter can speak ERC-8004 terminology without
caring that the on-chain contracts use different function selectors.

References:
- Draft discussion: github.com/ethereum/ERCs/issues/8004 (tracking)
- AgentRegistry: 0x6B71...BCbB (Fuji)
- ReputationContract: 0x40ef...603A (Fuji)
"""
from __future__ import annotations
import hashlib

# Canonical ERC-8004 function selectors (first 4 bytes of keccak256 signature)
# We expose these so clients can treat this adapter as ERC-8004-compliant
# at the integration boundary.
def _selector(sig: str) -> bytes:
    try:
        from eth_utils import keccak
        return keccak(text=sig)[:4]
    except Exception:
        return hashlib.sha3_256(sig.encode()).digest()[:4]


SELECTORS = {
    "getIdentity":        _selector("getIdentity(uint256)"),
    "getScore":           _selector("getScore(uint256)"),
    "getReputation":      _selector("getReputation(uint256)"),
    "getCategoryScore":   _selector("getCategoryScore(uint256,uint256)"),
    "supportsInterface":  _selector("supportsInterface(bytes4)"),
}

# XOR of identity + score + reputation selectors = canonical IERC8004 interfaceId
INTERFACE_ID = bytes(
    a ^ b ^ c for a, b, c in zip(
        SELECTORS["getIdentity"], SELECTORS["getScore"], SELECTORS["getReputation"]
    )
)
# Extended interface id (includes category scores) = xor with getCategoryScore too
EXTENDED_INTERFACE_ID = bytes(a ^ b for a, b in zip(INTERFACE_ID, SELECTORS["getCategoryScore"]))


class ERC8004Adapter:
    """Wraps an OnChain instance and exposes ERC-8004 standard getters.

    Usage:
        from onchain import OnChain
        from erc8004 import ERC8004Adapter
        oc = OnChain.from_env()
        std = ERC8004Adapter(oc)
        identity = std.get_identity(agent_id)
        score    = std.get_score(agent_id)
        rep      = std.get_reputation(agent_id)
    """

    def __init__(self, onchain):
        self.oc = onchain

    @property
    def interface_id(self) -> str:
        return "0x" + INTERFACE_ID.hex()

    def supports_interface(self, iface_id: str | bytes) -> bool:
        """ERC-165-style probe at the SDK layer. The deployed contract does
        not implement ERC-165 itself, so we answer here based on what the
        adapter can service. Recognizes both the canonical IERC8004 id and
        our extended id (adds getCategoryScore)."""
        if isinstance(iface_id, str):
            iface_id = bytes.fromhex(iface_id.replace("0x", ""))
        return iface_id in (INTERFACE_ID, EXTENDED_INTERFACE_ID,
                            b"\x01\xff\xc9\xa7")  # ERC-165 itself

    # ── IERC8004 surface ──────────────────────────────────────────────────

    def get_identity(self, agent_id: int) -> dict:
        """Delegates to AgentRegistry.getAgent. Returns the ERC-8004-shaped
        (owner, name, endpoint) tuple as a dict."""
        reg = self.oc._contracts["AgentRegistry"]
        tup = reg.functions.getAgent(int(agent_id)).call()
        return {
            "agentId": int(tup[0]),
            "owner":   tup[1],
            "name":    tup[2],
            "endpoint": tup[3],
            "source": "erc8004-adapter:AgentRegistry.getAgent",
        }

    def get_score(self, agent_id: int) -> dict:
        """Delegates to ReputationContract.getCreditProfile. Returns the
        ERC-8004-shaped (score, tier, tasks) slice."""
        p = self.oc._contracts["ReputationContract"].functions.getCreditProfile(int(agent_id)).call()
        return {
            "agentId": int(agent_id),
            "score": int(p[0]),
            "tier":  int(p[1]),
            "tasks": int(p[2]),
            "source": "erc8004-adapter:ReputationContract.getCreditProfile",
        }

    def get_category_score(self, agent_id: int, category_id: int) -> dict:
        """Per-category reputation snapshot. Currently derived from the
        base score + the agent's primary category — an extension of the
        canonical IERC8004. Returns {score, tier, specialization}.

        Real production would track per-category tasks separately in the
        contract; until the deployed Rep contract supports that, we apply
        a specialization multiplier if category matches agent's category.
        """
        from models import Agent as AgentModel
        from extensions import db

        base = self.get_score(agent_id)
        score = base["score"]
        tier = base["tier"]
        # Category affinity: agent gets a score boost in its own category,
        # a smaller score in unrelated categories
        agent = None
        try:
            from app import app
            with app.app_context():
                agent = db.session.get(AgentModel, agent_id)
        except Exception:
            pass
        CATEGORY_IDS = {
            "Development": 0, "Data & Analytics": 1, "Content": 2,
            "Finance": 3, "Research": 4, "Security": 5, "Automation": 6,
        }
        same_category = agent and CATEGORY_IDS.get(agent.category) == category_id
        spec_score = min(1000, int(score * (1.0 if same_category else 0.6)))
        spec_tier = tier if same_category else max(1, tier - 1)

        return {
            "agentId": agent_id,
            "categoryId": category_id,
            "score": spec_score,
            "tier": spec_tier,
            "isSpecialistCategory": bool(same_category),
            "baseScore": score,
            "source": "erc8004-adapter:getCategoryScore (category-weighted over base)",
        }

    def get_reputation(self, agent_id: int) -> dict:
        """Full reputation snapshot including incidents and last decay. The
        ERC-8004 draft returns (merkle, lastUpdate); we return a richer
        JSON payload, with the merkle computed client-side from the
        available fields so the interface still matches semantically."""
        p = self.oc._contracts["ReputationContract"].functions.getCreditProfile(int(agent_id)).call()
        score, tier, tasks, incidents, last_decay, projected = p
        # Synthetic "merkle" is the hash of the score tuple; deterministic
        # and a drop-in proxy until the on-chain contract exposes a real one.
        import hashlib
        h = hashlib.sha3_256(f"{agent_id}:{score}:{tier}:{tasks}:{incidents}:{last_decay}".encode()).hexdigest()
        return {
            "agentId": int(agent_id),
            "merkle":    "0x" + h,
            "score":     int(score),
            "tier":      int(tier),
            "tasksCompleted": int(tasks),
            "incidentCount":  int(incidents),
            "lastUpdate": int(last_decay),
            "projectedScore": int(projected),
            "source": "erc8004-adapter:ReputationContract.getCreditProfile (merkle synth)",
        }
