"""app.llm: the default and finance-demo LLM endpoints."""
from app import llm


def _clear(monkeypatch):
    monkeypatch.setattr(llm, "LLM_URL", "")
    monkeypatch.setattr(llm, "LLM_MODEL", "default-model")
    monkeypatch.setattr(llm, "LLM_KEY", "")
    monkeypatch.setattr(llm, "FINANCE_LLM_URL", "")
    monkeypatch.setattr(llm, "FINANCE_LLM_MODEL", "")
    monkeypatch.setattr(llm, "FINANCE_LLM_KEY", "")
    monkeypatch.setattr(llm, "FINANCE_DEMO_AGENT_ID", "")


def test_endpoint_for_agent_uses_default_when_finance_not_configured(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setattr(llm, "LLM_URL", "http://default.test")
    monkeypatch.setattr(llm, "FINANCE_DEMO_AGENT_ID", "AGT-FIN-0000-0")
    ep = llm.endpoint_for_agent("AGT-FIN-0000-0")
    assert ep.url == "http://default.test" and ep.model == "default-model"


def test_endpoint_for_agent_uses_finance_only_for_the_designated_agent(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setattr(llm, "LLM_URL", "http://default.test")
    monkeypatch.setattr(llm, "FINANCE_LLM_URL", "http://finance.test")
    monkeypatch.setattr(llm, "FINANCE_LLM_MODEL", "finance-model")
    monkeypatch.setattr(llm, "FINANCE_DEMO_AGENT_ID", "AGT-FIN-0000-0")

    assert llm.endpoint_for_agent("AGT-FIN-0000-0").url == "http://finance.test"
    assert llm.endpoint_for_agent("AGT-FIN-0000-0").model == "finance-model"
    # Case-insensitive match on the public id.
    assert llm.endpoint_for_agent("agt-fin-0000-0").url == "http://finance.test"
    # Every other agent still gets the default endpoint.
    assert llm.endpoint_for_agent("AGT-OTHER-0000-0").url == "http://default.test"
    assert llm.endpoint_for_agent(None).url == "http://default.test"


def test_generate_routes_to_the_right_endpoint(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setattr(llm, "LLM_URL", "http://default.test")
    monkeypatch.setattr(llm, "FINANCE_LLM_URL", "http://finance.test")
    monkeypatch.setattr(llm, "FINANCE_LLM_MODEL", "finance-model")
    monkeypatch.setattr(llm, "FINANCE_DEMO_AGENT_ID", "AGT-FIN-0000-0")

    seen = {}

    def fake_post(endpoint, body, *, timeout):
        seen["endpoint"] = endpoint
        seen["body"] = body
        return {"choices": [{"message": {"content": "hi"}}], "model": endpoint.model,
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
    monkeypatch.setattr(llm, "_post", fake_post)

    llm.generate("hello", agent_public_id="AGT-FIN-0000-0")
    assert seen["endpoint"].url == "http://finance.test"

    llm.generate("hello", agent_public_id="AGT-OTHER-0000-0")
    assert seen["endpoint"].url == "http://default.test"

    # Thinking mode is disabled for every call (Qwen3 reasoning-trace suppression).
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_chat_uses_default_endpoint_and_is_monkeypatchable(monkeypatch):
    """The exact pattern tests/test_sow_parse.py relies on: monkeypatching
    LLM_URL/chat directly must keep working after the dual-endpoint change."""
    _clear(monkeypatch)
    calls = []

    def fake_chat(system, user, **kw):
        calls.append((system, user, kw))
        return "ok"
    monkeypatch.setattr(llm, "LLM_URL", "http://default.test")
    monkeypatch.setattr(llm, "chat", fake_chat)
    assert llm.LLM_URL == "http://default.test"
    result = llm.chat("sys", "usr")
    assert result == "ok" and calls == [("sys", "usr", {})]


def test_health_reports_finance_only_when_configured(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setattr(llm, "_probe", lambda ep: {"ok": bool(ep.url), "configured": bool(ep.url)})

    assert "finance" not in llm.health()

    monkeypatch.setattr(llm, "FINANCE_LLM_URL", "http://finance.test")
    monkeypatch.setattr(llm, "FINANCE_DEMO_AGENT_ID", "AGT-FIN-0000-0")
    result = llm.health()
    assert result["finance"]["ok"] is True
    assert result["financeDemoAgentId"] == "AGT-FIN-0000-0"
