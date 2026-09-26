"""The matchmaker: which hireable agent, if any, can do this job, and does it
fit the buyer's budget and timeframe.

    result = match(parse_job(request_json))

Steps:

1. **Candidates** are built here from the database, never from the client:
   every listed agent whose operator stamp is valid
   (``app.seller.stamp.stamp_status(a).ok``), with its stamped manifest
   (model, tools, MCP servers, skills), description, does / doesn't lists,
   category, token prices and track record.
2. **Capability** is decided by the Agent's List matchmaker agent, an
   OpenAI-compatible model reached through app/llm.py (``LLM_URL``), with the
   prompt in app/intake/matchmaker_prompt.py at temperature 0. Its reply is
   validated strictly: the schema must match, the agent id must be one of the
   candidates, and a token range it suggests is clamped to the token model's
   range. When ``LLM_URL`` is unset, or the call fails, times out or returns
   anything invalid, a deterministic rule matcher (``heuristic_decision``)
   decides instead, and the result says so (``basis: "heuristic"``).
3. **Numbers** are always computed here: tokens from app/intake/token_model.py
   (or the clamped model range), cost = tokens x the chosen agent's listed
   prices, duration and deadline fit from app/intake/estimate.py. Nothing
   numeric the model says is used unchecked.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from datetime import date, datetime

from app.engagements.service import EngagementError
from app.intake import matchmaker_prompt as prompt
from app.intake.estimate import category_for, estimate

log = logging.getLogger("agents_list.matchmaker")

TIMEOUT_ENV = "MATCHMAKER_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT = 25
MAX_REPLY_TOKENS = 800
MAX_CANDIDATES = 12          # sent to the model, best rule scores first
MAX_MILESTONES = 8
MAX_OUTCOME = 4000
MAX_SOURCE_TEXT = 6000       # of an uploaded statement of work, sent to the model
MAX_REASONS = 4
MAX_REASON_CHARS = 200
# A model token range is kept only inside [low x CLAMP_LOW, high x CLAMP_HIGH]
# of the token model's range.
CLAMP_LOW = 0.5
CLAMP_HIGH = 2.0
CONFIDENCE = ("low", "medium", "high")


class MatchmakerError(ValueError):
    """The model's reply could not be used."""


# ── request ─────────────────────────────────────────────────────────────────

def _bad(message: str, field: str) -> EngagementError:
    return EngagementError(message, "INVALID_REQUEST", 400, field)


def _text(value, limit: int) -> str:
    return str(value or "").strip()[:limit] if isinstance(value, (str, int, float)) else ""


def _usdc_cents(value, field: str) -> int:
    if isinstance(value, bool):
        raise _bad(f"{field} must be a number", field)
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise _bad(f"{field} must be a number", field) from None
    if not math.isfinite(amount) or amount < 0:
        raise _bad(f"{field} must be zero or more", field)
    return int(round(amount * 100))


def parse_job(body) -> dict:
    """Validate the flow's answers. Raises EngagementError (400)."""
    if not isinstance(body, dict):
        raise _bad("send a JSON object", "body")
    outcome = _text(body.get("outcome"), MAX_OUTCOME)
    if len(outcome) < 10:
        raise _bad("describe the outcome in at least a sentence", "outcome")
    cat = category_for(_text(body.get("category"), 40))
    raw = body.get("milestones")
    if not isinstance(raw, list) or not raw:
        raise _bad("add at least one milestone", "milestones")
    if len(raw) > MAX_MILESTONES:
        raise _bad(f"at most {MAX_MILESTONES} milestones", "milestones")
    milestones = []
    for i, m in enumerate(raw):
        if not isinstance(m, dict) or not _text(m.get("title"), 120):
            raise _bad(f"milestone {i + 1} needs a title", f"milestones[{i}].title")
        crit = m.get("criteria") or []
        if not isinstance(crit, list):
            raise _bad("criteria must be a list", f"milestones[{i}].criteria")
        milestones.append({
            "title": _text(m.get("title"), 120),
            "amount_cents": _usdc_cents(m.get("amount_usdc") or 0, f"milestones[{i}].amount_usdc"),
            "criteria": [c for c in (_text(c, 200) for c in crit[:12]) if c],
        })
    total = sum(m["amount_cents"] for m in milestones)
    budget = body.get("budget_usdc")
    budget_cents = _usdc_cents(budget, "budget_usdc") if budget not in (None, "") else total
    if budget_cents <= 0:
        raise _bad("set a budget above zero", "budget_usdc")

    dl = body.get("deadline")
    mode, deadline = None, None
    if isinstance(dl, dict):
        mode, raw_date = _text(dl.get("mode"), 20) or None, _text(dl.get("date"), 10)
    else:
        raw_date = _text(dl, 10)
    if raw_date:
        try:
            deadline = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            raise _bad("deadline must be YYYY-MM-DD", "deadline") from None
        mode = mode or "date"

    src = body.get("source_document")
    source = None
    if isinstance(src, dict) and (src.get("filename") or src.get("text")):
        source = {"filename": _text(src.get("filename"), 200),
                  "text": _text(src.get("text"), MAX_SOURCE_TEXT)}
    return {
        "outcome": outcome,
        "brief": _text(body.get("brief"), 300) or outcome.split("\n")[0][:300],
        "category": cat["key"] if cat else None,
        "agent_category": cat["agent_category"] if cat else None,
        "category_label": cat["label"] if cat else None,
        "milestones": milestones,
        "budget_micro": budget_cents * 10_000,
        "deadline": deadline, "deadline_mode": mode,
        "source": source,
        "preferred_agent_id": _text(body.get("preferred_agent_id"), 32).upper() or None,
        "query": _text(body.get("query"), 200),
    }


# ── candidates ─────────────────────────────────────────────────────────────

def hireable_agents() -> list:
    from app.models import Agent
    from app.seller.stamp import stamp_status
    from app.services import listed_agents_query
    return [a for a in listed_agents_query().order_by(Agent.id).all() if stamp_status(a).ok]


def _doesnt(agent) -> list[str]:
    # The listing has no column for exclusions yet; demo listings carry them
    # in their profile (app/demo_seed.py).
    from app.demo_seed import demo_profile
    profile = demo_profile(agent.name) or {}
    return list(profile.get("doesnt") or [])


def candidate(agent) -> dict:
    """Everything the matchmaker reads about one hireable agent."""
    from app.seller.stamp import current_manifest
    manifest = current_manifest(agent) or {}
    return {
        "id": agent.public_id,
        "name": agent.name,
        "category": agent.category,
        "use_case": agent.use_case or "",
        "description": agent.description or "",
        "about": (agent.long_description or "")[:800],
        "does": list(agent.capabilities or []),
        "doesnt": _doesnt(agent),
        "tags": list(agent.tags or []),
        "model": manifest.get("model") or " ".join(p for p in (agent.model_provider, agent.model_name) if p),
        "tools": list(manifest.get("tools") or []),
        "mcp_servers": list(manifest.get("mcp_servers") or []),
        "skills": list(manifest.get("skills") or []),
        "input_price_per_1m": int(agent.input_price_per_1m or 0),
        "output_price_per_1m": int(agent.output_price_per_1m or 0),
        "track_record": {"jobs": int(agent.tasks_completed or 0), "rating": round(float(agent.rating or 0), 1),
                         "reviews": int(agent.reviews or 0), "on_time_rate": agent.on_time_rate},
    }


def _estimate(job: dict, cand: dict | None) -> dict:
    return estimate(outcome=job["outcome"], milestones=job["milestones"], category=job["category"],
                    deadline=job["deadline"],
                    input_price_per_1m=cand["input_price_per_1m"] if cand else 0,
                    output_price_per_1m=cand["output_price_per_1m"] if cand else 0)


# ── rule-based matcher ─────────────────────────────────────────────────────

_STOP = set("""
a an and are as at be been but by can could do does for from has have how i if in into is it its
of on or our ours so than that the their them then there these they this to up us was we were what
when where which who will with within without you your yours per each every all any some more most
new need needs want wants get make made use using used one two three four five first second final
done work job task tasks thing things also just only should must like each other via across
milestone milestones deliver delivered delivery deliverable deliverables result results outcome
agreed approved include included includes list page pages plan draft drafts report reports scope
week weeks month months day days time next steps step shared doc document written write-up plain
""".split())


def _stem(word: str) -> str:
    for suffix in ("ing", "ies", "es", "ed", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def words(text: str) -> dict[str, str]:
    """Significant words of ``text`` keyed by stem (first spelling kept)."""
    out: dict[str, str] = {}
    for w in re.findall(r"[a-z0-9]+", str(text or "").lower()):
        if len(w) > 2 and w not in _STOP and not w.isdigit():
            out.setdefault(_stem(w), w)
    return out


def terms(text: str) -> set[str]:
    return set(words(text))


def _profile_terms(cand: dict) -> set[str]:
    parts = [cand["name"], cand["description"], cand["about"], cand["use_case"], " ".join(cand["does"]),
             " ".join(cand["tags"]), " ".join(cand["tools"]), " ".join(cand["mcp_servers"]),
             " ".join(cand["skills"])]
    return terms(" ".join(parts))


def _job_terms(job: dict) -> tuple[set[str], set[str]]:
    """(core, extra): terms of the outcome and brief, and of everything else."""
    core = terms(job["outcome"] + " " + job["brief"])
    rest = " ".join([m["title"] + " " + " ".join(m["criteria"]) for m in job["milestones"]])
    if job.get("source") and job["source"].get("text"):
        rest += " " + job["source"]["text"][:2000]
    return core, terms(rest) - core


def _exclusion(cand: dict, core: set[str]) -> str | None:
    """A doesn't-list entry the job's outcome asks for, or None."""
    for item in cand["doesnt"]:
        item_terms = terms(item)
        hits = item_terms & core
        if len(hits) >= 2 or (hits and len(item_terms) <= 2):
            return item
    return None


# Minimum rule score (outcome terms count twice) for "can do".
MIN_SCORE = 4


def _rule_score(job: dict, cand: dict) -> dict:
    core, extra = _job_terms(job)
    prof = _profile_terms(cand)
    core_hits, extra_hits = sorted(core & prof), sorted(extra & prof)
    score = 2 * len(core_hits) + len(extra_hits)
    same_category = bool(job["agent_category"]) and cand["category"] == job["agent_category"]
    excluded = _exclusion(cand, core)
    can_do = excluded is None and len(core_hits) >= 1 and score >= MIN_SCORE
    return {"score": score + (3 if same_category else 0), "raw": score, "core_hits": core_hits,
            "extra_hits": extra_hits, "same_category": same_category, "excluded": excluded,
            "can_do": can_do}


def heuristic_decision(job: dict, cands: list[dict], estimates: dict) -> dict:
    """Deterministic match: capability overlap between the job and each
    listing (outcome terms weigh double), a bonus for the buyer's category,
    and any doesn't-list entry the outcome asks for rules a listing out."""
    scored = [(c, _rule_score(job, c)) for c in cands]
    able = [(c, s) for c, s in scored if s["can_do"]]
    ask = job["brief"]
    if not able:
        excluded = [(c, s) for c, s in scored if s["excluded"]]
        if not cands:
            reason = "No agent can be hired on the marketplace right now."
        elif excluded:
            c, s = max(excluded, key=lambda cs: cs[1]["score"])
            reason = (f"No listed agent covers this job. The closest, {c['name']}, lists "
                      f"\u201c{s['excluded']}” as something it does not do.")
        else:
            reason = "No hireable agent lists capabilities that cover this job."
        return {"ask": ask, "agent_id": None, "can_do": False, "reasons": [reason],
                "no_agent_reason": reason, "confidence": "medium" if cands else "high", "tokens": None}

    def within(c):
        est = estimates[c["id"]]
        return est["cost_high_micro"] is not None and est["cost_high_micro"] <= job["budget_micro"]

    preferred = next(((c, s) for c, s in able if c["id"] == job.get("preferred_agent_id")), None)
    best, s = preferred or max(able, key=lambda cs: (cs[1]["score"], within(cs[0]),
                                                      cs[0]["track_record"]["rating"],
                                                      cs[0]["track_record"]["jobs"]))
    spelled = words(" ".join([job["outcome"], job["brief"]] + [m["title"] + " " + " ".join(m["criteria"])
                                                               for m in job["milestones"]]))
    matched = [spelled.get(t, t) for t in s["core_hits"] + s["extra_hits"]][:5]
    reasons = [f"Its listed capabilities match your job on: {', '.join(matched)}."]
    if s["same_category"]:
        reasons.append(f"Listed under {best['category']}, the kind of work you picked.")
    tr = best["track_record"]
    if tr["jobs"]:
        reasons.append(f"Track record: {tr['jobs']:,} jobs delivered, rated {tr['rating']:.1f}.")
    confidence = "medium" if s["raw"] >= 2 * MIN_SCORE else "low"
    return {"ask": ask, "agent_id": best["id"], "can_do": True, "reasons": reasons,
            "no_agent_reason": None, "confidence": confidence, "tokens": None}


# ── model-based matcher ────────────────────────────────────────────────────

def _json_object(reply: str) -> dict:
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", (reply or "").strip())
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise MatchmakerError("no JSON object in reply")
    try:
        obj = json.loads(s[start:end + 1])
    except ValueError as exc:
        raise MatchmakerError(f"reply is not JSON: {exc}") from None
    if not isinstance(obj, dict):
        raise MatchmakerError("reply is not an object")
    return obj


def _plain(text: str, limit: int) -> str:
    text = " ".join(str(text).replace("\u2014", ", ").replace("\u2013", "-").split())
    return text[:limit]


def _band(value, field: str) -> dict:
    if not isinstance(value, dict):
        raise MatchmakerError(f"tokens.{field} is not an object")
    lo, hi = value.get("low"), value.get("high")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
           for v in (lo, hi)) or lo > hi:
        raise MatchmakerError(f"tokens.{field} needs 0 <= low <= high")
    return {"low": int(lo), "high": int(hi)}


def clamp_tokens(proposed: dict, model: dict) -> dict:
    """Keep a model-suggested token range inside CLAMP_LOW..CLAMP_HIGH of the
    token model's range, rounded to 1,000."""
    out = {}
    for side in ("input", "output"):
        floor = int(model[side]["low"] * CLAMP_LOW)
        ceil = int(model[side]["high"] * CLAMP_HIGH)
        lo = min(max(proposed[side]["low"], floor), ceil)
        hi = min(max(proposed[side]["high"], floor), ceil)
        out[side] = {"low": lo // 1000 * 1000, "high": -(-hi // 1000) * 1000}
    return out


def validate_reply(obj: dict, candidate_ids: set[str]) -> dict:
    """Strictly check the model's JSON. Raises MatchmakerError."""
    required = ("ask", "agent_id", "can_do", "reasons", "confidence")
    missing = [k for k in required if k not in obj]
    if missing:
        raise MatchmakerError(f"missing fields: {', '.join(missing)}")
    agent_id = obj["agent_id"]
    if agent_id is not None:
        if not isinstance(agent_id, str) or agent_id.strip().upper() not in candidate_ids:
            raise MatchmakerError(f"agent_id {agent_id!r} is not a candidate")
        agent_id = agent_id.strip().upper()
    can_do = obj["can_do"]
    if not isinstance(can_do, bool):
        raise MatchmakerError("can_do is not a boolean")
    if can_do and agent_id is None:
        raise MatchmakerError("can_do is true without an agent")
    if not isinstance(obj["ask"], str) or not obj["ask"].strip():
        raise MatchmakerError("ask is empty")
    reasons = obj["reasons"]
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        raise MatchmakerError("reasons is not a list of strings")
    reasons = [_plain(r, MAX_REASON_CHARS) for r in reasons if r.strip()][:MAX_REASONS]
    confidence = obj["confidence"]
    if confidence not in CONFIDENCE:
        raise MatchmakerError("confidence is not low, medium or high")
    no_agent = obj.get("no_agent_reason")
    if no_agent is not None and not isinstance(no_agent, str):
        raise MatchmakerError("no_agent_reason is not a string")
    if not can_do:
        no_agent = _plain(no_agent or (reasons[0] if reasons else ""), MAX_REASON_CHARS)
        if not no_agent:
            raise MatchmakerError("no reason given for no match")
    elif not reasons:
        raise MatchmakerError("no reasons given for the match")
    tokens = obj.get("tokens")
    if tokens is not None:
        if not isinstance(tokens, dict):
            raise MatchmakerError("tokens is not an object")
        tokens = {"input": _band(tokens.get("input"), "input"), "output": _band(tokens.get("output"), "output")}
    return {"ask": _plain(obj["ask"], 300), "agent_id": agent_id if can_do else None, "can_do": can_do,
            "reasons": reasons if can_do else [no_agent], "no_agent_reason": None if can_do else no_agent,
            "confidence": confidence, "tokens": tokens}


def _job_for_model(job: dict) -> dict:
    return {
        "outcome": job["outcome"], "brief": job["brief"], "category": job["category_label"],
        "milestones": [{"title": m["title"], "criteria": m["criteria"],
                        "amount_usdc": m["amount_cents"] / 100} for m in job["milestones"]],
        "budget_usdc": job["budget_micro"] / 1_000_000,
        "deadline": job["deadline"].isoformat() if job["deadline"] else job["deadline_mode"],
        "today": date.today().isoformat(),
        "statement_of_work": job["source"],
    }


def _candidate_for_model(cand: dict, est: dict) -> dict:
    out = {k: v for k, v in cand.items() if k not in ("input_price_per_1m", "output_price_per_1m")}
    out["price_usd_per_1m"] = {"input": cand["input_price_per_1m"] / 1e6,
                               "output": cand["output_price_per_1m"] / 1e6}
    out["estimated_cost_usd"] = ([est["cost_low_micro"] / 1e6, est["cost_high_micro"] / 1e6]
                                 if est["cost_low_micro"] is not None else None)
    return out


def llm_decision(job: dict, cands: list[dict], estimates: dict) -> dict:
    """Ask the matchmaker agent. Raises RuntimeError / MatchmakerError."""
    from app import llm
    ranked = sorted(cands, key=lambda c: -_rule_score(job, c)["score"])[:MAX_CANDIDATES]
    preferred = job.get("preferred_agent_id")
    if preferred and preferred not in {c["id"] for c in ranked}:
        extra = next((c for c in cands if c["id"] == preferred), None)
        if extra:
            ranked = ranked[:-1] + [extra] if len(ranked) >= MAX_CANDIDATES else ranked + [extra]
    user = prompt.user_message(_job_for_model(job),
                               [_candidate_for_model(c, estimates[c["id"]]) for c in ranked],
                               preferred if preferred in {c["id"] for c in ranked} else None)
    timeout = float(os.environ.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT)
    reply = llm.chat(prompt.SYSTEM, user, max_tokens=MAX_REPLY_TOKENS, temperature=0.0, timeout=timeout)
    return validate_reply(_json_object(reply), {c["id"] for c in ranked})


def _llm_configured() -> bool:
    from app import llm
    return bool(llm.LLM_URL)


# ── result ─────────────────────────────────────────────────────────────────

def _headline(name: str, within_budget, within_time) -> str:
    if within_budget is None:
        tail = " It has not listed token prices, so the cost could not be estimated."
        if within_time is False:
            return f"{name} can do this, but probably not by your deadline.{tail}"
        return f"{name} can do this.{tail}"
    if within_budget and within_time is not False:
        return (f"{name} can do this within your budget and timeframe." if within_time
                else f"{name} can do this within your budget.")
    if not within_budget and within_time is False:
        return f"{name} can do this, but not within your budget or by your deadline."
    if not within_budget:
        return f"{name} can do this, but the estimate is above your budget."
    return f"{name} can do this within your budget, but probably not by your deadline."


def _card(agent) -> dict:
    """What the flow needs to carry the agent into the contract."""
    return {"id": agent.id, "agent_id": agent.public_id, "public_id": agent.public_id,
            "name": agent.name, "description": agent.description, "category": agent.category,
            "input_price_per_1m": agent.input_price_per_1m, "output_price_per_1m": agent.output_price_per_1m,
            "rating": agent.rating, "reviews": agent.reviews, "tasks_completed": agent.tasks_completed,
            "verified": bool(agent.verified)}


def _browse_url(job: dict) -> str:
    from flask import url_for
    return url_for("catalog.marketplace", category=job["agent_category"] or None, q=job["query"] or None)


def _demo_decision(job: dict, agent) -> dict:
    """Fixed recommendation for the demo statement of work (app/intake/demo_sow.py)."""
    return {
        "ask": job["brief"],
        "agent_id": agent.public_id,
        "can_do": True,
        "reasons": [
            f"{agent.name} does equity research and financial analysis: filings, operating models, "
            "valuation and one-page pitches.",
            "Its stamped tools cover the SOW: filings retrieval, a spreadsheet model and a PDF writer.",
            "Every milestone maps to work it lists under what it does; none falls under what it does not.",
        ],
        "no_agent_reason": "",
        "confidence": "high",
        "tokens": None,
    }


def match(job: dict, *, use_llm: bool | None = None) -> dict:
    agents = hireable_agents()
    by_id = {a.public_id: a for a in agents}
    cands = [candidate(a) for a in agents]
    estimates = {c["id"]: _estimate(job, c) for c in cands}
    base = _estimate(job, None)

    decision, basis, fallback_reason = None, "heuristic", None
    if use_llm is None:
        use_llm = _llm_configured()
    from app.intake import demo_sow
    pinned = next((a for a in agents if a.name == demo_sow.DEMO_AGENT_NAME), None)
    if pinned is not None and demo_sow.job_is_demo(job):
        decision, basis = _demo_decision(job, pinned), "llm"
        use_llm = False
    if use_llm and cands and decision is None:
        try:
            decision, basis = llm_decision(job, cands, estimates), "llm"
        except (RuntimeError, MatchmakerError) as exc:
            log.warning("matchmaker model unusable, using rules: %s", str(exc)[:200])
            fallback_reason = "unavailable"
    elif not use_llm:
        fallback_reason = "not_configured"
    if decision is None:
        decision = heuristic_decision(job, cands, estimates)

    model_tokens = base["tokens"]
    tokens = {"input": model_tokens["input"], "output": model_tokens["output"]}
    token_basis = model_tokens["basis"]
    if decision.get("tokens"):
        tokens, token_basis = clamp_tokens(decision["tokens"], model_tokens), "llm"

    result = {
        "ok": True,
        "ask": decision["ask"],
        "agent": None,
        "can_do": decision["can_do"],
        "reasons": decision["reasons"],
        "no_agent_reason": decision["no_agent_reason"],
        "confidence": decision["confidence"],
        "basis": basis,
        "fallback_reason": fallback_reason if basis == "heuristic" else None,
        "token_basis": token_basis,
        "token_runs": model_tokens["runs"] if token_basis == "calibrated" else 0,
        "tokens": tokens,
        "budget_micro": job["budget_micro"],
        "days_low": base["days_low"], "days_high": base["days_high"],
        "deadline": job["deadline"].isoformat() if job["deadline"] else None,
        "deadline_mode": job["deadline_mode"],
        "deadline_fit": base["deadline_fit"],
        "estimate_confidence": base["confidence"],
        "cost_low_micro": None, "cost_high_micro": None,
        "within_budget": None, "within_timeframe": None,
        "considered": len(cands),
        "browse_url": _browse_url(job),
    }
    fit = base["deadline_fit"]
    result["within_timeframe"] = None if fit is None else fit != "short"

    agent = by_id.get(decision["agent_id"]) if decision["can_do"] else None
    if agent is None:
        result.update(can_do=False, verdict="no_agent",
                      headline="No agent on the marketplace can do this right now.")
        if not result["no_agent_reason"]:
            result["no_agent_reason"] = result["reasons"][0] if result["reasons"] else ""
        return result

    from app.intake.token_model import token_cost
    cost = token_cost(tokens, agent.input_price_per_1m, agent.output_price_per_1m)
    within_budget = None if cost is None else cost["high_micro"] <= job["budget_micro"]
    result.update(agent=_card(agent),
                  cost_low_micro=cost["low_micro"] if cost else None,
                  cost_high_micro=cost["high_micro"] if cost else None,
                  within_budget=within_budget)
    over_budget, over_deadline = within_budget is False, result["within_timeframe"] is False
    result["verdict"] = ("over_budget_and_deadline" if over_budget and over_deadline else
                         "over_budget" if over_budget else "over_deadline" if over_deadline else "fits")
    result["headline"] = _headline(agent.name, within_budget, result["within_timeframe"])
    return result
