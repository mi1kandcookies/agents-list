"""
llm.py - thin client for an OpenAI-compatible chat-completions endpoint.

Speaks the /v1/chat/completions surface exposed by vLLM, Ollama and most
hosted providers. Used for the optional "ask this agent" preview on agent
pages, to read uploaded statements of work into a draft scope
(app/intake/sow_parse.py), and by the matchmaker that picks the agent for a
job (app/intake/matchmaker.py).

Two independent endpoints, each optional:
    default   general-purpose: SOW parsing, the matchmaker, and the "ask
              this agent" preview for every agent except the one below.
    finance   one specific demo listing (FINANCE_DEMO_AGENT_ID) uses this
              dedicated endpoint/model for its "ask this agent" preview
              instead of the default.

Both are plain HTTP, unauthenticated in the current deployment: called from
Flask view code only, never forwarded to or reachable from browser JS.

Env (read fresh on every call, so tests can monkeypatch the module-level
names below directly, the same as the single-endpoint version of this
module always worked):
    LLM_URL / LLM_MODEL / LLM_API_KEY
        default endpoint (feature disabled when LLM_URL is unset)
    FINANCE_LLM_URL / FINANCE_LLM_MODEL / FINANCE_LLM_API_KEY
        finance endpoint (feature disabled when FINANCE_LLM_URL is unset)
    FINANCE_DEMO_AGENT_ID
        the one Agent.public_id that uses the finance endpoint
"""
from __future__ import annotations
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

LLM_URL   = os.environ.get("LLM_URL", "").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "Qwen/Qwen2.5-Coder-7B-Instruct")
LLM_KEY   = os.environ.get("LLM_API_KEY", "")

FINANCE_LLM_URL   = os.environ.get("FINANCE_LLM_URL", "").rstrip("/")
FINANCE_LLM_MODEL = os.environ.get("FINANCE_LLM_MODEL", "")
FINANCE_LLM_KEY   = os.environ.get("FINANCE_LLM_API_KEY", "")
FINANCE_DEMO_AGENT_ID = os.environ.get("FINANCE_DEMO_AGENT_ID", "").strip().upper()


@dataclass(frozen=True)
class Endpoint:
    url: str
    model: str
    key: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.url)


def _default_endpoint() -> Endpoint:
    """Built fresh from the current module globals on every call, so
    monkeypatching LLM_URL/LLM_MODEL/LLM_API_KEY (directly, the same as
    before this module supported a second endpoint) still works."""
    return Endpoint(url=LLM_URL.rstrip("/"), model=LLM_MODEL, key=LLM_KEY)


def _finance_endpoint() -> Endpoint:
    return Endpoint(url=FINANCE_LLM_URL.rstrip("/"), model=FINANCE_LLM_MODEL or LLM_MODEL,
                    key=FINANCE_LLM_KEY)


def is_finance_demo_agent(agent_public_id: str | None) -> bool:
    """True when this agent is the one designated to use the finance
    endpoint, and that endpoint is actually configured."""
    return ((agent_public_id or "").strip().upper() == FINANCE_DEMO_AGENT_ID
            and _finance_endpoint().configured)


def endpoint_for_agent(agent_public_id: str | None) -> Endpoint:
    """The finance endpoint for the one designated demo agent (only when it
    is itself configured), else the default endpoint."""
    finance = _finance_endpoint()
    if (agent_public_id or "").strip().upper() == FINANCE_DEMO_AGENT_ID and finance.configured:
        return finance
    return _default_endpoint()


def _agent_system(agent_name: str, agent_category: str, agent_bio: str = "") -> str:
    return (
        f"You are {agent_name}, a specialist agent listed on Agent's List, a "
        f"marketplace where buyers hire agents for scoped, long-running work. "
        f"Your specialty is {agent_category}. {agent_bio}\n\n"
        "Answer the buyer's question directly and concisely. When asked about "
        "cost or timeline, give ranges and state your assumptions. Do not "
        "promise outcomes you cannot verify."
    )


def _post(endpoint: Endpoint, body: dict, *, timeout: float) -> dict:
    if not endpoint.configured:
        raise RuntimeError("LLM endpoint not configured in environment")
    headers = {"Content-Type": "application/json"}
    if endpoint.key:
        headers["Authorization"] = f"Bearer {endpoint.key}"
    req = urllib.request.Request(
        f"{endpoint.url}/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"), headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LLM {e.code}: {e.read()[:200].decode('utf-8', 'replace')}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise RuntimeError(f"LLM unreachable: {getattr(e, 'reason', e)}")


def generate(prompt: str, *, agent_name: str = "Agent",
             agent_category: str = "Development",
             agent_bio: str = "",
             agent_public_id: str | None = None,
             max_tokens: int = 400,
             temperature: float = 0.3) -> dict:
    """Send a chat-completion request to the configured LLM (the finance
    endpoint when ``agent_public_id`` is the designated finance demo agent,
    else the default one). Returns {response, model, tokens, latencyMs}.
    Raises RuntimeError on any transport or server error (caller decides
    whether to 500 or degrade)."""
    endpoint = endpoint_for_agent(agent_public_id)
    body = {
        "model": endpoint.model,
        "messages": [
            {"role": "system", "content": _agent_system(agent_name, agent_category, agent_bio)},
            {"role": "user",   "content": prompt},
        ],
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
        # Qwen3 emits a <think>...</think> reasoning block by default; this
        # endpoint's chat template honors this vLLM extension to skip it, so
        # the deliverable is the direct answer. Harmless if a template ignores it.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    t0 = time.time()
    payload = _post(endpoint, body, timeout=60)
    choice = (payload.get("choices") or [{}])[0]
    msg = (choice.get("message") or {}).get("content", "")
    usage = payload.get("usage") or {}
    return {
        "response": msg,
        "model": payload.get("model") or endpoint.model,
        "promptTokens":     usage.get("prompt_tokens"),
        "completionTokens": usage.get("completion_tokens"),
        "totalTokens":      usage.get("total_tokens"),
        "latencyMs": int((time.time() - t0) * 1000),
    }


def chat(system: str, user: str, *, max_tokens: int = 1200, temperature: float = 0.0,
         timeout: float = 20, endpoint: Endpoint | None = None) -> str:
    """One system + user turn on the default endpoint (or ``endpoint`` when
    given); returns the reply text. Raises RuntimeError like ``generate``."""
    endpoint = endpoint or _default_endpoint()
    body = {
        "model": endpoint.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
        "chat_template_kwargs": {"enable_thinking": False},
    }
    payload = _post(endpoint, body, timeout=timeout)
    choice = (payload.get("choices") or [{}])[0]
    return (choice.get("message") or {}).get("content") or ""


def _probe(endpoint: Endpoint) -> dict:
    if not endpoint.configured:
        return {"ok": False, "configured": False, "error": "not set"}
    try:
        req = urllib.request.Request(
            f"{endpoint.url}/v1/models",
            headers={"Authorization": f"Bearer {endpoint.key}"} if endpoint.key else {},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        models = [m.get("id") for m in d.get("data", [])]
        return {"ok": True, "configured": True, "url": endpoint.url,
                "model": endpoint.model, "availableModels": models}
    except Exception as e:
        return {"ok": False, "configured": True, "url": endpoint.url, "error": str(e)[:120]}


def health() -> dict:
    """Quick status probe for both endpoints. Used by /api/llm/status."""
    result = _probe(_default_endpoint())
    finance = _finance_endpoint()
    if finance.configured:
        result["finance"] = _probe(finance)
        result["financeDemoAgentId"] = FINANCE_DEMO_AGENT_ID or None
    return result
