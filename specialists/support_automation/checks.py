"""
specialists/support_automation/checks.py - acceptance checks for the
support-automation specialist.

Each check is fn(workspace, params, *, run=None) -> {"passed", "details",
"score"}, listed in CHECK_DEFS (the kit's CheckRegistry.add_defs wraps them).
Checks recompute from the source files (ticket export, intent rules, help
center, held-out set) instead of trusting the numbers the agent wrote, so a
forged taxonomy, gap map, held-out set or replay report fails. Every path
goes through the kit's workspace jail (tools._resolve).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from specialists.support_automation import tools as T

REQUIRED_ESCALATIONS = ("billing_dispute", "legal_threat", "safety", "account_security", "vulnerable_user")
# Where article sources may live: the client's own material, never a file
# written during the engagement.
CLIENT_DIR = "inputs"


def _result(passed: bool | None, details: str, score: float | None = None) -> dict:
    return {"passed": passed, "details": details, "score": score}


def _guard(fn):
    """Missing or malformed files fail the check with a readable reason."""
    def wrapper(workspace: Path, params: dict, *, run=None) -> dict:
        try:
            return fn(Path(workspace), params or {}, run=run)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return _result(False, f"{fn.__name__}: {type(exc).__name__}: {exc}")
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


@_guard
def no_pii_remaining(workspace: Path, params: dict, *, run=None) -> dict:
    """params: paths [..] (files or directories of .md/.csv/.json/.txt)."""
    total, notes = 0, []
    for rel in params["paths"]:
        target = T._resolve(workspace, rel)
        files = sorted(p for p in target.rglob("*") if p.is_file()) if target.is_dir() else [target]
        for f in files:
            n = T.scan_pii(workspace, path=f.relative_to(workspace.resolve()).as_posix())["total"]
            if n:
                total += n
                notes.append(f"{f.name}: {n}")
    return _result(total == 0, "no PII found" if not total else "PII remains: " + ", ".join(notes))


@_guard
def redaction_complete(workspace: Path, params: dict, *, run=None) -> dict:
    """params: source, redacted. Same ticket ids in the same order, and no PII
    left in the redacted copy."""
    _, src = T._read_csv(T._resolve(workspace, params.get("source", "inputs/tickets.csv")))
    _, red = T._read_csv(T._resolve(workspace, params["redacted"]))
    if [r.get("ticket_id") for r in src] != [r.get("ticket_id") for r in red]:
        return _result(False, f"ticket ids differ: source {len(src)} rows, redacted {len(red)} rows")
    left = T.scan_pii(workspace, path=params["redacted"])["total"]
    return _result(left == 0, f"{len(red)} rows; {left} PII hits remain")


@_guard
def taxonomy_reconciles(workspace: Path, params: dict, *, run=None) -> dict:
    """params: tickets, rules, taxonomy, min_coverage (default 0.9). Re-labels
    the tickets with the rules and compares every volume in the taxonomy."""
    intents = T.load_intent_rules(workspace, params["rules"])
    _, rows = T._read_csv(T._resolve(workspace, params["tickets"]))
    for row in rows:
        row["intent"] = T.classify_text(f"{row.get('subject', '')} {row.get('body', '')}", intents)
    expected = {r["intent"]: r["volume"] for r in T.compute_taxonomy(rows, intents)}
    reported = {r["intent"]: int(r["volume"]) for r in T._read_taxonomy(workspace, params["taxonomy"])}
    problems = [f"{k}: reported {reported.get(k)} vs {v}" for k, v in expected.items()
                if v and reported.get(k) != v]
    problems += [f"{k}: not in rules" for k in reported if k not in expected]
    if sum(reported.values()) != len(rows):
        problems.append(f"volumes sum to {sum(reported.values())}, export has {len(rows)} tickets")
    coverage = 1 - expected.get(T.OTHER_INTENT, 0) / (len(rows) or 1)
    need = float(params.get("min_coverage", 0.9))
    if coverage < need:
        problems.append(f"coverage {coverage:.1%} below {need:.0%}")
    return _result(not problems, "; ".join(problems) or f"{len(rows)} tickets reconcile; coverage {coverage:.1%}",
                   round(coverage, 4))


@_guard
def gap_map_consistent(workspace: Path, params: dict, *, run=None) -> dict:
    """params: rules, taxonomy, kb_dir, gap_map. Recomputes covered/gap per intent."""
    intents = T.load_intent_rules(workspace, params["rules"])
    expected = {r["intent"]: r["status"] for r in
                T.compute_coverage(workspace, intents, T._read_taxonomy(workspace, params["taxonomy"]),
                                   params.get("kb_dir", "inputs/help_center"))}
    _, reported_rows = T._read_csv(T._resolve(workspace, params["gap_map"]))
    reported = {r["intent"]: r["status"] for r in reported_rows}
    bad = [f"{k}: reported {reported.get(k)} vs {v}" for k, v in expected.items() if reported.get(k) != v]
    return _result(not bad, "; ".join(bad) or f"{len(expected)} intents match")


def _articles(workspace: Path, rel_dir: str | None) -> list[Path]:
    """Markdown articles directly inside a workspace directory (jailed)."""
    return T._dir_files(T._resolver(workspace, None), rel_dir) if rel_dir else []


def _gaps(workspace: Path, params: dict) -> list[dict]:
    intents = T.load_intent_rules(workspace, params["rules"])
    rows = T.compute_coverage(workspace, intents, T._read_taxonomy(workspace, params["taxonomy"]),
                              params.get("kb_dir", "inputs/help_center"))
    skip = {i["id"] for i in intents if i.get("automation") == "human_only"}
    return [r for r in rows if r["status"] == "gap" and r["intent"] not in skip and r["volume"] > 0]


def _load_macros(workspace: Path, path: str) -> list[dict]:
    data = json.loads(T._resolve(workspace, path).read_text(encoding="utf-8"))
    macros = data.get("macros") if isinstance(data, dict) else data
    if not isinstance(macros, list):
        raise ValueError("macros file must be a list or {'macros': [...]}")
    return macros


@_guard
def top_gaps_addressed(workspace: Path, params: dict, *, run=None) -> dict:
    """params: rules, taxonomy, kb_dir, articles_dir, macros (optional), top_n.
    The top-N automatable gaps (recomputed against the original help center)
    must each have a new article or macro declaring the intent."""
    gaps = _gaps(workspace, params)[: int(params.get("top_n", 5))]
    declared = set()
    for p in _articles(workspace, params["articles_dir"]):
        declared |= set(T.parse_article(p)["intents"])
    if params.get("macros"):
        for m in _load_macros(workspace, params["macros"]):
            declared |= set(m.get("intents") or [])
    missing = [g["intent"] for g in gaps if g["intent"] not in declared]
    score = 1 - len(missing) / len(gaps) if gaps else 1.0
    return _result(not missing, f"missing: {', '.join(missing)}" if missing else
                   f"all {len(gaps)} top gaps addressed", round(score, 4))


def _source_exists(workspace: Path, ref: str, *, client_only: bool = False) -> bool:
    """A source is a workspace file, optionally with '#<ticket_id>' for a row;
    with client_only, a file under inputs/ (after resolving the path)."""
    rel, _, anchor = ref.partition("#")
    try:
        path = T._resolve(workspace, rel)
    except ValueError:
        return False
    if not path.is_file():
        return False
    if client_only:
        parts = path.relative_to(Path(workspace).resolve()).parts
        if not parts or parts[0].lower() != CLIENT_DIR:
            return False
    if not anchor:
        return True
    if path.suffix.lower() != ".csv":
        return False
    return any(r.get("ticket_id") == anchor for r in T._read_csv(path)[1])


@_guard
def articles_grounded(workspace: Path, params: dict, *, run=None) -> dict:
    """params: articles_dir, min_articles (default 1), rules (optional).
    Every article declares intents known to the rules and at least one
    source, and every source resolves to a client file under inputs/ or a
    row of one ('inputs/tickets.csv#T0042'); files written during the
    engagement do not count."""
    paths = _articles(workspace, params["articles_dir"])
    known = {i["id"] for i in T.load_intent_rules(workspace, params["rules"])} if params.get("rules") else None
    problems = []
    for p in paths:
        art = T.parse_article(p)
        if not art["intents"]:
            problems.append(f"{p.name}: no intents")
        elif known is not None and set(art["intents"]) - known:
            problems.append(f"{p.name}: unknown intents {sorted(set(art['intents']) - known)}")
        if not art["sources"]:
            problems.append(f"{p.name}: no sources")
        for src in art["sources"]:
            if not _source_exists(workspace, src, client_only=True):
                problems.append(f"{p.name}: source not found under {CLIENT_DIR}/: {src}")
    if len(paths) < int(params.get("min_articles", 1)):
        problems.append(f"{len(paths)} articles, need {params.get('min_articles', 1)}")
    return _result(not problems, "; ".join(problems) or f"{len(paths)} articles grounded")


@_guard
def macros_valid(workspace: Path, params: dict, *, run=None) -> dict:
    """params: macros, rules. Unique ids, non-empty body, known intents."""
    known = {i["id"] for i in T.load_intent_rules(workspace, params["rules"])}
    macros = _load_macros(workspace, params["macros"])
    problems, ids = [], set()
    for i, m in enumerate(macros):
        mid = m.get("id") or f"#{i}"
        if mid in ids:
            problems.append(f"{mid}: duplicate id")
        ids.add(mid)
        if not str(m.get("body") or "").strip():
            problems.append(f"{mid}: empty body")
        intents = m.get("intents") or []
        if not intents or set(intents) - known:
            problems.append(f"{mid}: intents {intents} not in rules")
    if not macros:
        problems.append("no macros")
    return _result(not problems, "; ".join(problems) or f"{len(macros)} macros valid")


@_guard
def kb_consistent(workspace: Path, params: dict, *, run=None) -> dict:
    """params: terms, articles_dir, paths (extra files, e.g. macros). Fails when
    the delivered KB states different numbers for the same policy term."""
    out = T.find_contradictions(workspace, terms=params["terms"], kb_dir=params["articles_dir"],
                                paths=params.get("paths") or [])
    conflicts = out["conflicts"]
    return _result(not conflicts, "no conflicting policy numbers" if not conflicts else
                   "; ".join(f"{c['term']}: {sorted(c['values'])}" for c in conflicts))


@_guard
def agent_config_valid(workspace: Path, params: dict, *, run=None) -> dict:
    """params: config, required_escalations (default: billing dispute, legal
    threat, safety, account security, vulnerable user). The config needs
    instructions, AI-disclosure text, a handoff message and a keyword rule for
    every required escalation category; knowledge paths must exist."""
    cfg = T.load_agent_config(workspace, params["config"])
    problems = []
    for key in ("instructions", "ai_disclosure", "handoff_message"):
        if not str(cfg.get(key) or "").strip():
            problems.append(f"missing {key}")
    rules = cfg.get("escalation_rules") or []
    have = {r.get("category") for r in rules if r.get("keywords")}
    for cat in params.get("required_escalations", REQUIRED_ESCALATIONS):
        if cat not in have:
            problems.append(f"no escalation rule for {cat}")
    for rel in cfg.get("knowledge_paths") or []:
        if not _source_exists(workspace, rel) and not T._resolve(workspace, rel).is_dir():
            problems.append(f"knowledge path not found: {rel}")
    return _result(not problems, "; ".join(problems) or "config complete")


@_guard
def eval_holdout_sealed(workspace: Path, params: dict, *, run=None) -> dict:
    """params: tickets, source (optional), holdout, build, holdout_fraction,
    seed, min_size, articles_dir, macros. Recomputes the split, requires
    build/holdout to be disjoint, and fails if any held-out ticket is cited by
    an article or macro (leakage into what the agent was built from). With
    `source` (the client's export), the split must cover exactly its tickets
    and every row must keep the export's must_escalate label and redacted
    subject and body, so relabeling or rewriting a held-out ticket fails."""
    _, hold = T._read_csv(T._resolve(workspace, params["holdout"]))
    _, build = T._read_csv(T._resolve(workspace, params["build"]))
    hold_ids = {r["ticket_id"] for r in hold}
    problems = []
    if hold_ids & {r["ticket_id"] for r in build}:
        problems.append("holdout and build overlap")
    frac = float(params.get("holdout_fraction", 0.3))
    seed = str(params.get("seed", "support-automation"))
    for key in ("tickets", "source"):
        if params.get(key):
            _, all_rows = T._read_csv(T._resolve(workspace, params[key]))
            want = {r["ticket_id"] for r in all_rows if T._split_bucket(r["ticket_id"], seed) < frac}
            if want != hold_ids:
                problems.append(f"holdout does not match the seeded split of {params[key]}")
    if params.get("source"):
        problems += _altered_rows(workspace, params["source"], hold + build)
    if len(hold) < int(params.get("min_size", 1)):
        problems.append(f"holdout has {len(hold)} tickets, need {params.get('min_size')}")
    cited = set()
    for p in _articles(workspace, params.get("articles_dir")):
        cited |= {s.partition("#")[2] for s in T.parse_article(p)["sources"]}
    if params.get("macros"):
        for m in _load_macros(workspace, params["macros"]):
            cited |= {s.partition("#")[2] for s in m.get("sources") or []}
    leaked = sorted(hold_ids & cited)
    if leaked:
        problems.append(f"held-out tickets cited as sources: {', '.join(leaked)}")
    return _result(not problems, "; ".join(problems) or f"{len(hold)} held-out tickets sealed")


def _altered_rows(workspace: Path, source: str, rows: list[dict]) -> list[str]:
    """Problems when `rows` are not exactly the source export's tickets with
    their gold label and redacted text unchanged."""
    _, src = T._read_csv(T._resolve(workspace, source))
    by_id = {r.get("ticket_id"): r for r in src}
    problems = []
    seen = [r.get("ticket_id") for r in rows]
    if sorted(seen, key=str) != sorted(by_id, key=str):
        problems.append(f"split holds {len(seen)} tickets, {source} has {len(by_id)}")
    altered = []
    for row in rows:
        orig = by_id.get(row.get("ticket_id"))
        if orig is None:
            continue
        if T._truthy(row.get("must_escalate")) != T._truthy(orig.get("must_escalate")) or any(
                (row.get(col) or "") != T.redact_text(orig.get(col) or "")[0] for col in ("subject", "body")):
            altered.append(str(row.get("ticket_id")))
    if altered:
        problems.append(f"rows differ from {source}: {', '.join(sorted(altered)[:20])}")
    return problems


@_guard
def escalation_recall(workspace: Path, params: dict, *, run=None) -> dict:
    """params: config, eval, min_recall (default 0.95), report (optional).
    Replays the escalation rules on the held-out set; if a report file is
    given, its numbers must equal the recomputed ones."""
    cfg = T.load_agent_config(workspace, params["config"])
    _, rows = T._read_csv(T._resolve(workspace, params["eval"]))
    got = T.compute_replay(rows, cfg.get("escalation_rules") or [])
    problems = []
    if params.get("report"):
        reported = json.loads(T._resolve(workspace, params["report"]).read_text(encoding="utf-8"))
        for key in ("must_escalate_recall", "escalation_precision", "false_neg", "true_pos", "tickets"):
            if reported.get(key) != got[key]:
                problems.append(f"report {key}={reported.get(key)} but replay gives {got[key]}")
    need = float(params.get("min_recall", 0.95))
    if got["must_escalate_recall"] < need:
        problems.append(f"recall {got['must_escalate_recall']:.2f} below {need:.2f} "
                        f"(missed {', '.join(got['missed_ticket_ids'])})")
    if got["true_pos"] + got["false_neg"] == 0:
        problems.append("held-out set has no must-escalate tickets")
    return _result(not problems, "; ".join(problems) or
                   f"recall {got['must_escalate_recall']:.2f}, precision {got['escalation_precision']:.2f}",
                   got["must_escalate_recall"])


CHECK_DEFS: dict[str, Any] = {
    "no_pii_remaining": no_pii_remaining,
    "redaction_complete": redaction_complete,
    "taxonomy_reconciles": taxonomy_reconciles,
    "gap_map_consistent": gap_map_consistent,
    "top_gaps_addressed": top_gaps_addressed,
    "articles_grounded": articles_grounded,
    "macros_valid": macros_valid,
    "kb_consistent": kb_consistent,
    "agent_config_valid": agent_config_valid,
    "eval_holdout_sealed": eval_holdout_sealed,
    "escalation_recall": escalation_recall,
}
