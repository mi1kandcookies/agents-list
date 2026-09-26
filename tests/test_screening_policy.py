import pytest

from app.screening.policy import ScreeningBlocked, enforce_verdict, normalize_verdict


def test_screening_is_fail_closed_for_unknown_and_hold():
    for payload in ({}, {"decision": "HOLD"}):
        verdict = normalize_verdict(payload, address="0xabc", provider="fixture", now=100)
        assert verdict.decision == "ASK_HUMAN"
        with pytest.raises(ScreeningBlocked):
            enforce_verdict(verdict, 1, now=100)


def test_screening_cap_is_enforced():
    verdict = normalize_verdict({"decision": "CAP", "capAtomic": 50, "verdictId": "v1"},
                                 address="0xabc", provider="fixture", now=100)
    enforce_verdict(verdict, 50, now=100)
    with pytest.raises(ScreeningBlocked, match="cap"):
        enforce_verdict(verdict, 51, now=100)


def test_refuse_is_terminal_even_if_amount_is_small():
    verdict = normalize_verdict({"decision": "DENY", "reason": "blocked"},
                                 address="0xabc", provider="fixture", now=100)
    with pytest.raises(ScreeningBlocked, match="blocked"):
        enforce_verdict(verdict, 1, now=100)
