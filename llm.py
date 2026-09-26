"""
llm.py — thin client for the Akash-hosted vLLM / Ollama instance.

Speaks the OpenAI-compatible /v1/chat/completions surface vLLM exposes.
Every AgentHire agent gets a per-agent system prompt so the same model
answers as 125 different agents without any fine-tuning.
"""
from __future__ import annotations
import os
import json
import urllib.request
import urllib.error

LLM_URL   = os.environ.get("LLM_URL", "").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "Qwen/Qwen2.5-Coder-7B-Instruct")
LLM_KEY   = os.environ.get("LLM_API_KEY", "")   # optional, vLLM --api-key

# Global marketplace context — injected into every system prompt so every
# response is AgentHire-aware without extra work at call sites.
MARKETPLACE_CONTEXT = """You are operating inside AgentHire, an on-chain
marketplace for autonomous AI agents on Avalanche Fuji.

Protocol facts you can rely on:
  - Identity via ERC-8004 AgentRegistry at 0x6B71b84F...
  - Reputation via ReputationContract at 0x40ef89Ce... (score 0-1000, tiers T1-T3)
  - Stake via StakingSlashing at 0xfc942b4d...
  - Payments via x402 (HTTP 402) settling in MockUSDC (6 decimals)
  - Escrow via EscrowPayment at 0xD19990C7...
  - Gasless buyer flow via EIP-3009 transferWithAuthorization

API endpoints on the host app (localhost:8080):
  POST /api/x402/pay          {fromId, toId, amountUSDC}
  GET  /api/agents/<id>/erc8004
  POST /api/sim/post-bid      {tokenBudget, maxPricePerToken, minTier, categoryId}
  POST /api/sim/slash-agent   {agentId, reason}
"""


def _agent_system(agent_name: str, agent_category: str, agent_bio: str = "") -> str:
    return (
        f"{MARKETPLACE_CONTEXT}\n\n"
        f"You are {agent_name}, one of 125+ autonomous agents listed on AgentHire. "
        f"Your specialty within the marketplace is {agent_category}. {agent_bio}\n\n"
        f"IMPORTANT rules for every response:\n"
        f"  1. When asked who you are, ALWAYS introduce yourself as an agent on "
        f"     the AgentHire marketplace first, then mention your specialty.\n"
        f"  2. Never describe yourself as a standalone product or company. You "
        f"     are one agent among many on AgentHire, discoverable by buyers via "
        f"     x402 payments and scored on-chain via ERC-8004.\n"
        f"  3. Stay focused on answering the buyer's actual task — hand back "
        f"     concise, implementation-ready output (code when code is asked for).\n"
        f"  4. Reference marketplace primitives (escrow, stake, reputation) when "
        f"     a buyer asks how payment or dispute flow works."
    )


def generate(prompt: str, *, agent_name: str = "Agent",
             agent_category: str = "Development",
             agent_bio: str = "",
             max_tokens: int = 400,
             temperature: float = 0.3) -> dict:
    """Send a chat-completion request to the Akash-hosted LLM. Returns a dict
    with {response, model, tokens, latencyMs}. Raises RuntimeError on any
    transport or server error (caller decides whether to 500 or degrade)."""
    if not LLM_URL:
        raise RuntimeError("LLM_URL not configured in environment")

    body = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": _agent_system(agent_name, agent_category, agent_bio)},
            {"role": "user",   "content": prompt},
        ],
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
    }
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if LLM_KEY:
        headers["Authorization"] = f"Bearer {LLM_KEY}"

    req = urllib.request.Request(
        f"{LLM_URL}/v1/chat/completions",
        data=data, headers=headers, method="POST",
    )
    import time
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LLM {e.code}: {e.read()[:200].decode('utf-8', 'replace')}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM unreachable: {e.reason}")

    choice = (payload.get("choices") or [{}])[0]
    msg = (choice.get("message") or {}).get("content", "")
    usage = payload.get("usage") or {}
    return {
        "response": msg,
        "model": payload.get("model") or LLM_MODEL,
        "promptTokens":     usage.get("prompt_tokens"),
        "completionTokens": usage.get("completion_tokens"),
        "totalTokens":      usage.get("total_tokens"),
        "latencyMs": int((time.time() - t0) * 1000),
    }


def health() -> dict:
    """Quick status probe. Used by /api/llm/status."""
    if not LLM_URL:
        return {"ok": False, "configured": False, "error": "LLM_URL not set"}
    try:
        req = urllib.request.Request(
            f"{LLM_URL}/v1/models",
            headers={"Authorization": f"Bearer {LLM_KEY}"} if LLM_KEY else {},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        models = [m.get("id") for m in d.get("data", [])]
        return {"ok": True, "configured": True, "url": LLM_URL,
                "model": LLM_MODEL, "availableModels": models}
    except Exception as e:
        return {"ok": False, "configured": True, "url": LLM_URL, "error": str(e)[:120]}
