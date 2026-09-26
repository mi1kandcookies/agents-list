"""
llm.py - thin client for an OpenAI-compatible chat-completions endpoint.

Speaks the /v1/chat/completions surface exposed by vLLM, Ollama and most
hosted providers. Used for the optional "ask this agent" preview on agent
pages, to read uploaded statements of work into a draft scope
(app/intake/sow_parse.py) and by the matchmaker that picks the agent for a
job (app/intake/matchmaker.py). The scoping agent (roadmap Phase 2) will use a
frontier model via the Anthropic API / Agent SDK instead.

Env:
    LLM_URL      base URL of the endpoint (feature disabled when unset)
    LLM_MODEL    model id to request
    LLM_API_KEY  optional bearer token
"""
from __future__ import annotations
import os
import json
import urllib.request
import urllib.error

LLM_URL   = os.environ.get("LLM_URL", "").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "Qwen/Qwen2.5-Coder-7B-Instruct")
LLM_KEY   = os.environ.get("LLM_API_KEY", "")


def _agent_system(agent_name: str, agent_category: str, agent_bio: str = "") -> str:
    return (
        f"You are {agent_name}, a specialist agent listed on Agent's List, a "
        f"marketplace where buyers hire agents for scoped, long-running work. "
        f"Your specialty is {agent_category}. {agent_bio}\n\n"
        "Answer the buyer's question directly and concisely. When asked about "
        "cost or timeline, give ranges and state your assumptions. Do not "
        "promise outcomes you cannot verify."
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


def chat(system: str, user: str, *, max_tokens: int = 1200, temperature: float = 0.0,
         timeout: float = 20) -> str:
    """One system + user turn; returns the reply text. Raises RuntimeError on
    any transport or server error, like ``generate``."""
    if not LLM_URL:
        raise RuntimeError("LLM_URL not configured in environment")
    body = {
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
    }
    headers = {"Content-Type": "application/json"}
    if LLM_KEY:
        headers["Authorization"] = f"Bearer {LLM_KEY}"
    req = urllib.request.Request(f"{LLM_URL}/v1/chat/completions",
                                 data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LLM {e.code}: {e.read()[:200].decode('utf-8', 'replace')}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise RuntimeError(f"LLM unreachable: {getattr(e, 'reason', e)}")
    choice = (payload.get("choices") or [{}])[0]
    return (choice.get("message") or {}).get("content") or ""


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
