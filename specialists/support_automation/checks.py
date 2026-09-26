"""
specialists/support_automation/checks.py - acceptance checks for the
support-automation specialist.

Each check is fn(workspace, params, *, run=None) -> {"passed", "details",
"score"}, listed in CHECK_DEFS (the kit's CheckRegistry.add_defs wraps them).
Checks recompute from the client's files (ticket export, help center,
policies) instead of trusting anything the agent wrote: the redacted copy,
taxonomy, gap map, held-out split and replay report must each equal what the
tools derive from inputs/, so a forged or hand-edited number fails. Every
path goes through the kit's workspace jail (tools._resolve).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from agentkit.checks.builtin import DEFAULT_PLACEHOLDERS
from agentkit.errors import AgentKitError
from agentkit.evidence import sha256_file
from agentkit.journal import safe_name
from agentkit.policy import INTERNAL_DIR
from agentkit.tools.documents import document_text
from specialists.support_automation import tools as T

REQUIRED_ESCALATIONS = ("billing_dispute", "legal_threat", "safety", "account_security", "vulnerable_user")
# Where article sources may live: the client's own material, never a file
# written during the engagement.
CLIENT_DIR = "inputs"
SOURCE = "inputs/tickets.csv"
ID = T.ID_COLUMN
# Placeholder text a macro must not ship with; {{first_name}}-style
# customer fields are allowed in macros.
MACRO_PLACEHOLDERS = [re.compile(p, re.IGNORECASE) for p in DEFAULT_PLACEHOLDERS if r"\{" not in p]


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


def _read(workspace: Path, rel: str) -> tuple[list[str], list[dict[str, str]]]:
    return T._read_csv(T._resolve(workspace, rel))


def _altered(orig: dict, row: dict, columns: list[str]) -> bool:
    """True when a cell is neither the export's value nor its redaction."""
    for col in columns:
        if col == ID:
            continue
        value, original = row.get(col) or "", orig.get(col) or ""
        if value != original and value != T.redact_text(original)[0]:
            return True
    return False


@_guard
def ticket_export_valid(workspace: Path, params: dict, *, run=None) -> dict:
    """params: source (default inputs/tickets.csv), columns (default
    ticket_id, subject, body, must_escalate). The client's export has those
    columns, unique non-empty ticket ids, and a 1/0 must_escalate label on
    every row with at least one positive: milestone 3 measures escalation
    recall against these labels. Only the client can fix a failure here."""
    source = params.get("source", SOURCE)
    header, rows = _read(workspace, source)
    need = list(params.get("columns") or T.REQUIRED_COLUMNS)
    missing = [c for c in need if c not in header]
    if missing:
        return _result(False, f"{source} lacks column(s) {', '.join(missing)} "
                              f"(has: {', '.join(header) or 'none'}); ask the client for them")
    problems = []
    if not rows:
        problems.append(f"{source} has no tickets")
    ids = [(r.get(ID) or "").strip() for r in rows]
    if any(not i for i in ids):
        problems.append(f"{sum(1 for i in ids if not i)} tickets without a {ID}")
    dupes = sorted(i for i, n in Counter(ids).items() if i and n > 1)
    if dupes:
        problems.append(f"duplicate {ID}s: {', '.join(dupes[:10])}")
    if T.LABEL_COLUMN in need:
        bad = [r.get(ID) or "?" for r in rows
               if str(r.get(T.LABEL_COLUMN) or "").strip().lower() not in T.LABEL_VALUES]
        if bad:
            problems.append(f"{T.LABEL_COLUMN} is not 1/0 on {len(bad)} tickets: {', '.join(bad[:10])}")
        elif rows and not any(T._truthy(r.get(T.LABEL_COLUMN)) for r in rows):
            problems.append(f"no ticket has {T.LABEL_COLUMN}=1; escalation recall cannot be measured")
    positives = sum(1 for r in rows if T._truthy(r.get(T.LABEL_COLUMN)))
    return _result(not problems, "; ".join(problems) or
                   f"{len(rows)} tickets, {positives} labeled must-escalate")


@_guard
def no_pii_remaining(workspace: Path, params: dict, *, run=None) -> dict:
    """params: paths [..] (files or directories). No email address, phone
    number or Luhn-valid card number is left in any of them. Names and street
    addresses are outside what this check can see."""
    total, notes = 0, []
    for rel in params["paths"]:
        target = T._resolve(workspace, rel)
        files = sorted(p for p in target.rglob("*") if p.is_file()) if target.is_dir() else [target]
        for f in files:
            n = T.scan_pii(workspace, path=f.relative_to(workspace.resolve()).as_posix())["total"]
            if n:
                total += n
                notes.append(f"{f.name}: {n}")
    return _result(total == 0, "no emails, phones or card numbers found" if not total else
                   "PII remains: " + ", ".join(notes))


@_guard
def redaction_complete(workspace: Path, params: dict, *, run=None) -> dict:
    """params: source (default inputs/tickets.csv), redacted. The redacted
    copy has the export's columns and tickets in the same order, every cell is
    the export's value or exactly its redaction (so ticket text cannot be
    rewritten or added), and no PII is left."""
    source = params.get("source", SOURCE)
    src_header, src = _read(workspace, source)
    T.require_ids(src_header, source)
    red_header, red = _read(workspace, params["redacted"])
    if red_header != src_header:
        return _result(False, f"columns differ from {source}: {red_header} vs {src_header}")
    if [r.get(ID) for r in src] != [r.get(ID) for r in red]:
        return _result(False, f"ticket ids differ: source {len(src)} rows, redacted {len(red)} rows")
    altered = [r.get(ID) or "?" for s, r in zip(src, red) if _altered(s, r, src_header)]
    left = T.scan_pii(workspace, path=params["redacted"])["total"]
    problems = [f"rows differ from {source}: {', '.join(altered[:20])}"] if altered else []
    if left:
        problems.append(f"{left} PII hits remain")
    return _result(not problems, "; ".join(problems) or f"{len(red)} rows; 0 PII hits remain")


def _same(reported: Any, expected: Any) -> bool:
    if isinstance(expected, str):
        return (reported or "") == expected
    try:
        return abs(float(reported) - float(expected)) < 1e-9
    except (TypeError, ValueError):
        return False


def _by_intent(rows: list[dict], problems: list[str]) -> dict[str, dict]:
    """Rows keyed by intent; a repeated intent is a problem (a second row
    would otherwise hide behind the first)."""
    dupes = sorted(k or "(blank)" for k, n in Counter(r.get("intent") for r in rows).items() if n > 1)
    if dupes:
        problems.append(f"repeated rows for {', '.join(dupes)}")
    return {r.get("intent"): r for r in rows}


def _labeled_export(workspace: Path, params: dict, intents: list[dict]) -> list[dict]:
    """The redacted client export, labeled with the rules (in memory)."""
    return T.label_rows(T.redacted_export(workspace, params.get("source", SOURCE))[1], intents)


@_guard
def taxonomy_reconciles(workspace: Path, params: dict, *, run=None) -> dict:
    """params: source (default inputs/tickets.csv), rules, taxonomy, labeled
    (optional), min_coverage (default 0.9). Re-labels the redacted export with
    the rules: every taxonomy row (label, automation, volume, share, handle
    time, escalation rate) must equal the recomputed one, and the labeled file
    must carry the recomputed intent for every ticket."""
    intents = T.load_intent_rules(workspace, params["rules"])
    rows = _labeled_export(workspace, params, intents)
    expected = {r["intent"]: r for r in T.compute_taxonomy(rows, intents)}
    problems: list[str] = []
    reported = _by_intent(T._read_taxonomy(workspace, params["taxonomy"]), problems)
    for intent, exp in expected.items():
        got = reported.get(intent)
        if got is None:
            problems.append(f"{intent}: missing from the taxonomy")
            continue
        diffs = [f"{k} {got.get(k) or '(blank)'} vs {exp[k]}" for k in T.TAXONOMY_HEADER[1:]
                 if not _same(got.get(k), exp[k])]
        if diffs:
            problems.append(f"{intent}: reported " + ", ".join(diffs))
    problems += [f"{k}: not in rules" for k in reported if k not in expected]
    if params.get("labeled"):
        _, labeled = _read(workspace, params["labeled"])
        if [r.get(ID) for r in labeled] != [r.get(ID) for r in rows]:
            problems.append(f"{params['labeled']} does not hold the export's tickets in order")
        else:
            wrong = [r.get(ID) for r, e in zip(labeled, rows) if r.get("intent") != e["intent"]]
            if wrong:
                problems.append(f"{params['labeled']}: intent differs from the rules for {', '.join(wrong[:20])}")
    coverage = 1 - expected[T.OTHER_INTENT]["volume"] / (len(rows) or 1)
    need = float(params.get("min_coverage", 0.9))
    if coverage < need:
        problems.append(f"coverage {coverage:.1%} below {need:.0%}")
    return _result(not problems, "; ".join(problems) or f"{len(rows)} tickets reconcile; coverage {coverage:.1%}",
                   round(coverage, 4))


def _coverage(workspace: Path, params: dict) -> tuple[list[dict], list[dict]]:
    """(intents, coverage rows) with volumes from the redacted client export."""
    intents = T.load_intent_rules(workspace, params["rules"])
    volumes = T.intent_volumes(_labeled_export(workspace, params, intents))
    return intents, T.compute_coverage(workspace, intents, volumes, params.get("kb_dir", "inputs/help_center"))


@_guard
def gap_map_consistent(workspace: Path, params: dict, *, run=None) -> dict:
    """params: source (default inputs/tickets.csv), rules, kb_dir, gap_map.
    Recomputes every intent's volume, covered/gap status and covering
    articles from the export and the help center; the gap map must match."""
    _, rows = _coverage(workspace, params)
    expected = {r["intent"]: r for r in rows}
    bad: list[str] = []
    reported = _by_intent(_read(workspace, params["gap_map"])[1], bad)
    for intent, exp in expected.items():
        got = reported.get(intent)
        if got is None:
            bad.append(f"{intent}: missing")
            continue
        if got.get("status") != exp["status"]:
            bad.append(f"{intent}: reported {got.get('status')} vs {exp['status']}")
        if not _same(got.get("volume"), exp["volume"]):
            bad.append(f"{intent}: volume {got.get('volume')} vs {exp['volume']}")
        if set(filter(None, (got.get("articles") or "").split(";"))) != set(filter(None, exp["articles"].split(";"))):
            bad.append(f"{intent}: articles {got.get('articles') or '(none)'} vs {exp['articles'] or '(none)'}")
    bad += [f"{k}: not in rules" for k in reported if k not in expected]
    return _result(not bad, "; ".join(bad) or f"{len(expected)} intents match")


def _articles(workspace: Path, rel_dir: str | None, suffixes=(".md",)) -> list[Path]:
    """Articles directly inside a workspace directory (jailed)."""
    return T._dir_files(T._resolver(workspace, None), rel_dir, suffixes) if rel_dir else []


def _gaps(workspace: Path, params: dict) -> tuple[list[dict], list[dict]]:
    """(automatable gaps, every gap) with ticket volume, largest first."""
    intents, rows = _coverage(workspace, params)
    human = {i["id"] for i in intents if i.get("automation") == "human_only"}
    every = [r for r in rows if r["status"] == "gap" and r["volume"] > 0]
    return [r for r in every if r["intent"] not in human], every


def _load_macros(workspace: Path, path: str) -> list[dict]:
    data = json.loads(T._resolve(workspace, path).read_text(encoding="utf-8"))
    macros = data.get("macros") if isinstance(data, dict) else data
    if not isinstance(macros, list):
        raise ValueError("macros file must be a list or {'macros': [...]}")
    return macros


@_guard
def top_gaps_addressed(workspace: Path, params: dict, *, run=None) -> dict:
    """params: source (default inputs/tickets.csv), rules, kb_dir,
    articles_dir, macros (optional), top_n. The top-N automatable gaps
    (volumes from the client export, coverage against the original help
    center) must each have a new article or macro declaring the intent. Fails
    when every gap is marked human_only: that leaves nothing to fill."""
    auto, every = _gaps(workspace, params)
    if every and not auto:
        return _result(False, f"all {len(every)} knowledge gaps are marked human_only in the intent rules "
                              f"({', '.join(g['intent'] for g in every[:10])}); nothing left to fill", 0.0)
    gaps = auto[: int(params.get("top_n", 5))]
    declared = set()
    for p in _articles(workspace, params["articles_dir"]):
        declared |= set(T.parse_article(p)["intents"])
    if params.get("macros"):
        for m in _load_macros(workspace, params["macros"]):
            declared |= set(m.get("intents") or []) if isinstance(m, dict) else set()
    missing = [g["intent"] for g in gaps if g["intent"] not in declared]
    score = 1 - len(missing) / len(gaps) if gaps else 1.0
    return _result(not missing, f"missing: {', '.join(missing)}" if missing else
                   f"all {len(gaps)} top gaps addressed", round(score, 4))


def _top_dir(workspace: Path, path: Path) -> str:
    parts = path.relative_to(Path(workspace).resolve()).parts
    return parts[0].lower() if parts else ""


def _source_problem(workspace: Path, ref: Any, *, client_only: bool = False) -> str | None:
    """Why `ref` is not a usable source, or None. A source is a workspace
    file (a client file under inputs/ with client_only); a ticket CSV (one
    with a ticket_id column) must be cited by row, '<file>#<ticket_id>'."""
    if not isinstance(ref, str) or not ref.strip():
        return f"not a source: {ref!r}"
    rel, _, anchor = ref.partition("#")
    try:
        path = T._resolve(workspace, rel)
    except ValueError as exc:
        return f"{ref}: {exc}"
    if not path.is_file() or (client_only and _top_dir(workspace, path) != CLIENT_DIR):
        return f"source not found under {CLIENT_DIR}/: {ref}" if client_only else f"source not found: {ref}"
    is_csv = path.suffix.lower() == ".csv"
    if anchor:
        if not is_csv:
            return f"row anchor on a non-CSV source: {ref}"
        if not any(r.get(ID) == anchor for r in T._read_csv(path)[1]):
            return f"ticket not found: {ref}"
    elif is_csv and ID in T._read_csv(path)[0]:
        return f"cite a ticket row ({rel}#<{ID}>), not the whole export: {ref}"
    return None


def _document_values(workspace: Path, refs: list[Any]) -> set[str]:
    """Policy numbers stated in the cited client documents (not ticket rows:
    customers and one-off agent replies never ground a policy number)."""
    values: set[str] = set()
    for ref in refs:
        if _source_problem(workspace, ref, client_only=True) or "#" in ref:
            continue
        try:
            values |= T.policy_values(document_text(T._resolve(workspace, ref)))
        except (AgentKitError, OSError, ValueError):
            continue
    return values


def _number_problems(workspace: Path, name: str, text: str, refs: list[Any]) -> list[str]:
    unbacked = sorted(T.policy_values(text) - _document_values(workspace, refs))
    if not unbacked:
        return []
    return [f"{name}: {', '.join(unbacked)} not stated in a cited client document "
            "(ticket rows cannot ground a policy number)"]


@_guard
def articles_grounded(workspace: Path, params: dict, *, run=None) -> dict:
    """params: articles_dir, min_articles (default 1), rules (optional).
    The folder holds only Markdown articles (no other files or folders).
    Every article declares intents known to the rules and at least one
    source; every source resolves to a client file under inputs/ or a ticket
    row of one ('inputs/tickets.csv#T0042', never the whole export); and every
    policy number it states (days, hours, amounts, percentages) appears in a
    cited client document. Files written during the engagement do not count."""
    root = T._resolve(workspace, params["articles_dir"])
    problems = [f"{e.name}: only Markdown articles belong in {params['articles_dir']}"
                for e in (sorted(root.iterdir()) if root.is_dir() else [])
                if e.is_dir() or e.suffix.lower() != ".md"]
    paths = _articles(workspace, params["articles_dir"])
    known = {i["id"] for i in T.load_intent_rules(workspace, params["rules"])} if params.get("rules") else None
    for p in paths:
        art = T.parse_article(p)
        if not art["intents"]:
            problems.append(f"{p.name}: no intents")
        elif known is not None and set(art["intents"]) - known:
            problems.append(f"{p.name}: unknown intents {sorted(set(art['intents']) - known)}")
        if not art["sources"]:
            problems.append(f"{p.name}: no sources")
        for src in art["sources"]:
            why = _source_problem(workspace, src, client_only=True)
            if why:
                problems.append(f"{p.name}: {why}")
        problems += _number_problems(workspace, p.name, f"{art['title']}\n\n{art['body']}", art["sources"])
    if len(paths) < int(params.get("min_articles", 1)):
        problems.append(f"{len(paths)} articles, need {params.get('min_articles', 1)}")
    return _result(not problems, "; ".join(problems) or f"{len(paths)} articles grounded")


@_guard
def macros_valid(workspace: Path, params: dict, *, run=None) -> dict:
    """params: macros, rules. Unique ids, a non-empty body without placeholder
    text ({{field}} customer fields are fine), intents known to the rules,
    sources (if any) that resolve like article sources, and every policy
    number in the body stated in a cited client document."""
    known = {i["id"] for i in T.load_intent_rules(workspace, params["rules"])}
    macros = _load_macros(workspace, params["macros"])
    problems, ids = [], set()
    for i, m in enumerate(macros):
        if not isinstance(m, dict):
            problems.append(f"#{i}: not an object")
            continue
        mid = m.get("id") or f"#{i}"
        if mid in ids:
            problems.append(f"{mid}: duplicate id")
        ids.add(mid)
        body = str(m.get("body") or "")
        if not body.strip():
            problems.append(f"{mid}: empty body")
        if any(p.search(body) for p in MACRO_PLACEHOLDERS):
            problems.append(f"{mid}: placeholder text in body")
        intents = m.get("intents") or []
        if not isinstance(intents, list) or not intents or set(intents) - known:
            problems.append(f"{mid}: intents {intents} not in rules")
        sources = m.get("sources") or []
        if not isinstance(sources, list):
            problems.append(f"{mid}: sources must be a list")
            sources = []
        for src in sources:
            why = _source_problem(workspace, src, client_only=True)
            if why:
                problems.append(f"{mid}: {why}")
        problems += _number_problems(workspace, str(mid), f"{m.get('title') or ''}\n\n{body}", sources)
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


def _knowledge_problem(workspace: Path, rel: Any) -> str | None:
    if not isinstance(rel, str) or not rel.strip():
        return f"knowledge path is not a path: {rel!r}"
    try:
        path = T._resolve(workspace, rel)
    except ValueError as exc:
        return f"knowledge path {rel}: {exc}"
    if not path.exists():
        return f"knowledge path not found: {rel}"
    if path.is_file() and path.suffix.lower() == ".csv" and ID in T._read_csv(path)[0]:
        return f"knowledge path is a ticket export, not knowledge: {rel}"
    return None


@_guard
def agent_config_valid(workspace: Path, params: dict, *, run=None) -> dict:
    """params: config, required_escalations (default: billing dispute, legal
    threat, safety, account security, vulnerable user). The config needs
    instructions, AI-disclosure text, a handoff message and well-formed
    escalation rules (a category and a list of phrases of at least three
    characters each) covering every required category; knowledge paths must
    exist and must not be ticket exports."""
    cfg = T.load_agent_config(workspace, params["config"])
    problems = [f"missing {key}" for key in ("instructions", "ai_disclosure", "handoff_message")
                if not str(cfg.get(key) or "").strip()]
    rules = cfg.get("escalation_rules")
    problems += T.escalation_rule_problems(rules)
    have = {r["category"].strip() for r in (rules if isinstance(rules, list) else [])
            if isinstance(r, dict) and isinstance(r.get("category"), str)
            and not T.keyword_problems(r.get("keywords"), "")}
    for cat in params.get("required_escalations") or REQUIRED_ESCALATIONS:
        if cat not in have:
            problems.append(f"no escalation rule for {cat}")
    paths = cfg.get("knowledge_paths") or []
    for rel in paths if isinstance(paths, list) else [paths]:
        why = _knowledge_problem(workspace, rel)
        if why:
            problems.append(why)
    return _result(not problems, "; ".join(problems) or "config complete")


def _cited_ids(workspace: Path, refs: list[Any]) -> set[str]:
    """Ticket ids a list of sources draws on: each row anchor, and every
    ticket of a ticket CSV cited without one."""
    ids: set[str] = set()
    for ref in refs:
        if not isinstance(ref, str):
            continue
        rel, _, anchor = ref.partition("#")
        if anchor:
            ids.add(anchor.strip())
            continue
        try:
            path = T._resolve(workspace, rel)
        except ValueError:
            continue
        if path.is_file() and path.suffix.lower() == ".csv":
            header, rows = T._read_csv(path)
            if ID in header:
                ids |= {r.get(ID) or "" for r in rows}
    return ids


def _split_problems(workspace: Path, source: str, files: dict[str, tuple[list[str], list[dict]]]) -> list[str]:
    """Problems when the split files do not hold exactly the export's tickets
    with every export column unchanged (or redacted)."""
    src_header, src = _read(workspace, source)
    T.require_ids(src_header, source)
    by_id = {r.get(ID): r for r in src}
    problems, rows = [], []
    for name, (header, part) in files.items():
        lost = [c for c in src_header if c not in header]
        if lost:
            problems.append(f"{name} lacks column(s) {', '.join(lost)} of {source}")
        rows += part
    seen = [r.get(ID) for r in rows]
    if sorted(seen, key=str) != sorted(by_id, key=str):
        problems.append(f"split holds {len(seen)} tickets, {source} has {len(by_id)}")
    altered = sorted(str(r.get(ID)) for r in rows
                     if r.get(ID) in by_id and _altered(by_id[r.get(ID)], r, src_header))
    if altered:
        problems.append(f"rows differ from {source}: {', '.join(altered[:20])}")
    return problems


@_guard
def eval_holdout_sealed(workspace: Path, params: dict, *, run=None) -> dict:
    """params: source (the client's export), holdout, build, holdout_fraction,
    seed, min_size, tickets (optional copy to check too), articles_dir and
    macros (optional). The held-out set must be the seeded, stratified split
    of the export (tools.holdout_ids), disjoint from the build set, and
    together they must hold exactly the export's tickets with every column
    unchanged or redacted - so relabeling or rewriting a held-out ticket
    fails. No article or macro may cite a held-out ticket, by row or by
    citing a whole ticket file."""
    hold_header, hold = _read(workspace, params["holdout"])
    build_header, build = _read(workspace, params["build"])
    T.require_ids(hold_header, params["holdout"])
    T.require_ids(build_header, params["build"])
    hold_ids = {r.get(ID) for r in hold}
    problems = []
    if hold_ids & {r.get(ID) for r in build}:
        problems.append("holdout and build overlap")
    frac = float(params.get("holdout_fraction", 0.3))
    seed = str(params.get("seed", "support-automation"))
    for key in ("tickets", "source"):
        if params.get(key):
            _, all_rows = _read(workspace, params[key])
            if T.holdout_ids(all_rows, frac, seed) != hold_ids:
                problems.append(f"holdout does not match the seeded split of {params[key]}")
    if params.get("source"):
        problems += _split_problems(workspace, params["source"], {params["holdout"]: (hold_header, hold),
                                                                  params["build"]: (build_header, build)})
    if len(hold) < int(params.get("min_size", 1)):
        problems.append(f"holdout has {len(hold)} tickets, need {params.get('min_size')}")
    refs: list[Any] = []
    for p in _articles(workspace, params.get("articles_dir"), suffixes=None):
        refs += T.parse_article(p)["sources"]
    if params.get("macros") and T._resolve(workspace, params["macros"]).is_file():
        refs += [s for m in _load_macros(workspace, params["macros"]) if isinstance(m, dict)
                 for s in (m.get("sources") if isinstance(m.get("sources"), list) else [])]
    leaked = sorted(hold_ids & _cited_ids(workspace, refs))
    if leaked:
        problems.append(f"held-out tickets cited as sources: {', '.join(leaked[:20])}")
    return _result(not problems, "; ".join(problems) or f"{len(hold)} held-out tickets sealed")


@_guard
def escalation_recall(workspace: Path, params: dict, *, run=None) -> dict:
    """params: config, eval, min_recall (default 0.95), min_precision
    (default 0.3), min_positives (default 1), report (optional). Replays the
    escalation rules on the held-out set. Recall must reach min_recall on at
    least min_positives must-escalate tickets, and precision must reach
    min_precision so a rule set that hands most conversations to a person
    fails; malformed rules fail; a report file must equal the replay. The
    details give the recall's 95% lower bound."""
    cfg = T.load_agent_config(workspace, params["config"])
    rules = cfg.get("escalation_rules") or []
    _, rows = _read(workspace, params["eval"])
    got = T.compute_replay(rows, rules)
    problems = []
    rule_problems = T.escalation_rule_problems(rules)
    if rule_problems:
        problems.append("malformed rules: " + "; ".join(rule_problems[:5]))
    if params.get("report"):
        reported = json.loads(T._resolve(workspace, params["report"]).read_text(encoding="utf-8"))
        for key in ("must_escalate_recall", "escalation_precision", "false_neg", "true_pos", "false_pos", "tickets"):
            if reported.get(key) != got[key]:
                problems.append(f"report {key}={reported.get(key)} but replay gives {got[key]}")
    positives = got["true_pos"] + got["false_neg"]
    need_pos = int(params.get("min_positives", 1))
    if positives < need_pos:
        problems.append(f"held-out set has {positives} must-escalate tickets, need at least {need_pos} "
                        "for a recall claim")
    need = float(params.get("min_recall", 0.95))
    if positives and got["must_escalate_recall"] < need:
        problems.append(f"recall {got['must_escalate_recall']:.2f} below {need:.2f} "
                        f"(missed {', '.join(map(str, got['missed_ticket_ids']))})")
    need_p = float(params.get("min_precision", 0.3))
    if got["true_pos"] + got["false_pos"] and got["escalation_precision"] < need_p:
        problems.append(f"precision {got['escalation_precision']:.2f} below {need_p:.2f}: {got['false_pos']} "
                        f"of {got['tickets'] - positives} ordinary tickets would go to a person")
    summary = (f"recall {got['must_escalate_recall']:.2f} ({got['true_pos']}/{positives}, 95% lower bound "
               f"{got['recall_lower_bound_95']:.2f}), precision {got['escalation_precision']:.2f}")
    return _result(not problems, "; ".join(problems + [summary]) if problems else summary,
                   got["must_escalate_recall"])


@_guard
def prior_milestone_unchanged(workspace: Path, params: dict, *, run=None) -> dict:
    """params: milestone, dirs (optional). Every file an earlier milestone
    submitted in this workspace still has the sha256 in its saved submission,
    and each listed directory holds no file that submission lacks - so a
    later milestone cannot rewrite the discovery or knowledge base it builds
    on (re-running a tool into the same path with the same inputs is fine;
    it writes the same bytes). A milestone that never ran in this workspace
    has no submission (the agent cannot remove one: .agentkit/ is off-limits
    to its tools), so there is nothing to pin and the check says so."""
    mid = str(params["milestone"])
    saved = Path(workspace).resolve() / INTERNAL_DIR / "submissions" / f"{safe_name(mid)}.json"
    if not saved.is_file():
        return _result(True, f"{mid} has no submission in this workspace; nothing to pin")
    sub = json.loads(saved.read_text(encoding="utf-8"))
    arts = [a for a in sub.get("artifacts") or [] if isinstance(a, dict) and a.get("path")]
    changed = []
    for a in arts:
        path = T._resolve(workspace, a["path"])
        if not path.is_file():
            changed.append(f"{a['path']} (missing)")
        elif sha256_file(path) != a.get("sha256"):
            changed.append(a["path"])
    submitted = {T._resolve(workspace, a["path"]) for a in arts}
    for rel in params.get("dirs") or []:
        root = T._resolve(workspace, rel)
        if root.is_dir():
            changed += [f"{f.relative_to(Path(workspace).resolve()).as_posix()} (added)"
                        for f in sorted(root.rglob("*")) if f.is_file() and f.resolve() not in submitted]
    status = sub.get("status", "?")
    if changed:
        return _result(False, f"changed since {mid} was submitted ({status}): {', '.join(changed[:20])}")
    return _result(True, f"{len(arts)} {mid} files unchanged since submission ({status})")


CHECK_DEFS: dict[str, Any] = {
    "ticket_export_valid": ticket_export_valid,
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
    "prior_milestone_unchanged": prior_milestone_unchanged,
}
