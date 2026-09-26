"""
specialists/proposal_writer/checks.py - acceptance checks for the
proposal-writer specialist.

Each check is fn(workspace, params, *, run=None) -> {"passed", "details",
"score"} and recomputes from the source documents in inputs/ (solicitation,
questionnaire, knowledge base) with the same parsers the tools use. The
agent's own files are compared against that recomputation, never trusted:
a source whose sha256 changed, a requirement that is not verbatim in the
solicitation, a matrix row marked addressed with no marker in the draft, or
a cited number that is not in the cited passage all fail.
"""
from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any, Callable

from agentkit.errors import ToolError
from specialists.proposal_writer import tools as T

BRIEF_PATH = "deliverables/m1-shred/bid-brief.md"
GAPS_PATH = "deliverables/m2-outline/gaps.md"
EVIDENCE_STATUSES = {"mapped", "gap", "not_applicable"}


def _result(passed: bool | None, details: str, score: float | None = None) -> dict[str, Any]:
    return {"passed": passed, "details": details, "score": score}


def _guarded(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """A missing or malformed deliverable is a failed check, not a crash."""
    @functools.wraps(fn)
    def wrapper(workspace: Path, params: dict | None = None, *, run=None) -> dict[str, Any]:
        try:
            return fn(Path(workspace), dict(params or {}), run=run)
        except (ToolError, OSError, ValueError, KeyError, TypeError) as exc:
            return _result(False, f"{fn.__name__}: {exc}", 0.0)
    return wrapper


def _brief(items: list[str], limit: int = 5) -> str:
    shown = "; ".join(str(i) for i in items[:limit])
    return shown + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


def verified_sources(workspace: Path, doc: dict[str, Any], prefixes: list[str]) -> list[str]:
    """Problems with the recorded sources: outside the trusted prefixes,
    missing, or changed since the shred (sha256 mismatch)."""
    problems = []
    sources = doc.get("sources") or []
    if not sources:
        problems.append("requirements.json lists no sources")
    for src in sources:
        rel = str(src.get("path", ""))
        if not rel.startswith(tuple(prefixes)):
            problems.append(f"source {rel!r} is not under {prefixes}")
            continue
        path = T.resolve(workspace, rel)
        if not path.is_file():
            problems.append(f"source {rel} is missing")
        elif T.sha256_file(path) != src.get("sha256"):
            problems.append(f"source {rel} changed since the shred (sha256 mismatch)")
    return problems


def _load(workspace: Path, params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    doc = T.load_requirements(workspace, params.get("requirements", T.REQUIREMENTS_PATH))
    return doc, verified_sources(workspace, doc, params.get("source_prefixes", ["inputs/"]))


def _recompute_rules(workspace: Path, doc: dict[str, Any]) -> list[dict[str, Any]]:
    rules = []
    for src in doc["sources"]:
        rules.extend(T.format_rules_text(T.read_text(workspace, src["path"]), src["path"]))
    return rules


# --- M1: shred, matrix, format rules, dates ------------------------------------

@_guarded
def shred_complete(workspace: Path, params: dict[str, Any], *, run=None) -> dict[str, Any]:
    """Every binding statement the deterministic shredder finds is in
    requirements.json with the right section/page, and every row is verbatim
    from a trusted, unchanged source."""
    doc, problems = _load(workspace, params)
    if problems:
        return _result(False, _brief(problems), 0.0)
    rows = doc["requirements"]
    ids = [r.get("id", "") for r in rows]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    bad_ids = [i for i in ids if not re.fullmatch(r"[A-Z]{1,4}-\d{1,4}", str(i))]
    if dupes or bad_ids:
        return _result(False, f"duplicate ids {dupes}; malformed ids {bad_ids[:5]}", 0.0)
    source_text = {s["path"]: T.read_text(workspace, s["path"]) for s in doc["sources"]}
    questionnaire = doc.get("mode") == "questionnaire"
    if questionnaire:
        expected = [{"text": q["text"], "source": s, "section": q["section"], "page": None}
                    for s in source_text for q in T.questionnaire_rows(workspace, s)]
    else:
        expected = [r for s, text in source_text.items() for r in T.shred_text(text, s)]
    normalized_sources = {s: T.normalize_ws(t) for s, t in source_text.items()}
    forged = [r.get("id") for r in rows
              if T.normalize_ws(str(r.get("text", ""))) not in normalized_sources.get(r.get("source"), "")
              or not T.normalize_ws(str(r.get("text", "")))]
    reported = {(r.get("source"), T.normalize_ws(str(r.get("text", "")))): r for r in rows}
    missing, misplaced = [], []
    for exp in expected:
        row = reported.get((exp["source"], T.normalize_ws(exp["text"])))
        if row is None:
            missing.append(exp["text"][:80])
        elif not questionnaire and (row.get("page") != exp["page"] or row.get("section") != exp["section"]):
            misplaced.append(f"{row.get('id')} (expected {exp['section']} p{exp['page']})")
    recall = 1.0 if not expected else (len(expected) - len(missing)) / len(expected)
    passed = not forged and not misplaced and recall >= float(params.get("min_recall", 1.0))
    parts = [f"{len(rows)} rows; recall {recall:.2f} of {len(expected)} binding statements"]
    if missing:
        parts.append(f"missing: {_brief(missing, 3)}")
    if forged:
        parts.append(f"not verbatim in source: {_brief(forged)}")
    if misplaced:
        parts.append(f"wrong section/page: {_brief(misplaced)}")
    return _result(passed, "; ".join(parts), round(recall, 4))


@_guarded
def matrix_consistent(workspace: Path, params: dict[str, Any], *, run=None) -> dict[str, Any]:
    """The compliance matrix has exactly one row per requirement with its
    verbatim text; with require_addressed, every row is addressed at a
    location recomputed from the draft's markers (or the answer sheet)."""
    doc, problems = _load(workspace, params)
    matrix_rel = params.get("matrix", T.MATRIX_PATH)
    rows = T.read_csv_rows(workspace, matrix_rel)
    if rows and set(T.MATRIX_COLUMNS) - set(rows[0].keys()):
        problems.append(f"missing columns {sorted(set(T.MATRIX_COLUMNS) - set(rows[0].keys()))}")
    reqs = {r["id"]: r for r in doc["requirements"]}
    seen: dict[str, int] = {}
    for row in rows:
        rid = row.get("req_id", "")
        seen[rid] = seen.get(rid, 0) + 1
        if rid not in reqs:
            problems.append(f"{rid}: not a requirement")
        elif T.normalize_ws(row.get("requirement", "")) != T.normalize_ws(reqs[rid].get("text", "")):
            problems.append(f"{rid}: requirement text differs from the shred")
    problems += [f"{rid}: {n} rows" for rid, n in seen.items() if n > 1]
    problems += [f"{rid}: no matrix row" for rid in reqs if rid not in seen]
    addressed = 0
    if params.get("require_addressed"):
        locations = T.response_locations(workspace, params.get("draft", T.DRAFT_PATH),
                                         params.get("checklist", T.CHECKLIST_PATH))
        answers_rel = params.get("answers", T.ANSWERS_PATH)
        answer_status = {}
        if doc.get("mode") == "questionnaire" and T.resolve(workspace, answers_rel).is_file():
            answer_status = {a.get("question_id"): a.get("status")
                             for a in T.read_csv_rows(workspace, answers_rel)}
        for row in rows:
            rid, status = row.get("req_id", ""), row.get("status", "")
            where = [w.strip() for w in row.get("response_section", "").split(";") if w.strip()]
            if rid in locations:
                if status != "addressed" or not where or not set(where) <= set(locations[rid]):
                    problems.append(f"{rid}: matrix location {where} vs draft markers {locations[rid]}")
                else:
                    addressed += 1
            elif answer_status.get(rid) in ("answered", "needs_review"):
                expected = "addressed" if answer_status[rid] == "answered" else "needs_review"
                if status != expected:
                    problems.append(f"{rid}: status {status!r}, answer sheet says {answer_status[rid]}")
                else:
                    addressed += 1
            else:
                problems.append(f"{rid}: not addressed (no marker in the draft or checklist)")
    score = addressed / len(reqs) if params.get("require_addressed") and reqs else None
    passed = not problems
    return _result(passed, "ok" if passed else _brief(problems), score)


@_guarded
def format_rules_captured(workspace: Path, params: dict[str, Any], *, run=None) -> dict[str, Any]:
    """format_rules.json holds every rule recomputed from the solicitation
    and nothing that is not stated there."""
    doc, problems = _load(workspace, params)
    if problems:
        return _result(False, _brief(problems), 0.0)
    data = json.loads(T.resolve(workspace, params.get("rules", T.FORMAT_RULES_PATH)).read_text(encoding="utf-8"))
    reported = data.get("rules", []) if isinstance(data, dict) else []
    expected = _recompute_rules(workspace, doc)
    have = {T.rule_key(r.get("kind"), r.get("value")) for r in reported}
    want = {T.rule_key(r["kind"], r["value"]) for r in expected}
    sources = " ".join(T.normalize_ws(T.read_text(workspace, s["path"])) for s in doc["sources"])
    missing = sorted(want - have)
    forged = [f"{r.get('kind')}={r.get('value')}" for r in reported
              if T.rule_key(r.get("kind"), r.get("value")) not in want
              and T.normalize_ws(str(r.get("text", ""))) not in sources]
    score = (len(want) - len(missing)) / len(want) if want else 1.0
    passed = not missing and not forged
    details = f"{len(want)} rules in the solicitation, {len(reported)} reported"
    if missing:
        details += f"; missing {_brief([f'{k}={v}' for k, v in missing])}"
    if forged:
        details += f"; not in the solicitation: {_brief(forged)}"
    return _result(passed, details, round(score, 4))


@_guarded
def dates_match_source(workspace: Path, params: dict[str, Any], *, run=None) -> dict[str, Any]:
    """Every date in the bid brief appears in the solicitation, and every
    deadline-like date in the solicitation appears in the brief. A
    questionnaire's due date comes from the customer, so it is skipped there."""
    doc, problems = _load(workspace, params)
    if doc.get("mode") == "questionnaire":
        return _result(True, "questionnaire engagement; dates come from intake", None)
    if problems:
        return _result(False, _brief(problems), 0.0)
    found = []
    for src in doc["sources"]:
        found.extend(T.dates_text(T.read_text(workspace, src["path"]), src["path"]))
    source_dates = {d["date"] for d in found}
    deadlines = {d["date"] for d in found if d["deadline"]}
    brief_dates = {iso for iso, _ in T.find_dates(T.read_text(workspace, params.get("path", BRIEF_PATH)))}
    invented = sorted(brief_dates - source_dates)
    omitted = sorted(deadlines - brief_dates)
    passed = not invented and not omitted
    details = f"{len(brief_dates)} dates in brief, {len(deadlines)} deadlines in the solicitation"
    if invented:
        details += f"; not in the solicitation: {invented}"
    if omitted:
        details += f"; deadlines missing from brief: {omitted}"
    return _result(passed, details, None)


# --- M2: evidence map, gaps, outline -------------------------------------------

@_guarded
def evidence_map_complete(workspace: Path, params: dict[str, Any], *, run=None) -> dict[str, Any]:
    """Each requirement has one evidence row: mapped to KB passages that
    exist, a gap listed in the SME question list, or not_applicable for
    format/submission/form rows only."""
    doc, problems = _load(workspace, params)
    rows = T.read_csv_rows(workspace, params.get("path", T.EVIDENCE_MAP_PATH))
    passages = T.kb_passages(workspace, params.get("kb_dir", T.KB_DIR))
    gaps_rel = params.get("gaps", GAPS_PATH)
    gaps_text = T.read_text(workspace, gaps_rel) if T.resolve(workspace, gaps_rel).is_file() else ""
    gap_ids = set(T._REQ_ID.findall(gaps_text))
    reqs = {r["id"]: r for r in doc["requirements"]}
    seen: dict[str, int] = {}
    mapped = 0
    for row in rows:
        rid, status = row.get("req_id", ""), row.get("status", "")
        seen[rid] = seen.get(rid, 0) + 1
        if rid not in reqs:
            problems.append(f"{rid}: not a requirement")
            continue
        if status not in EVIDENCE_STATUSES:
            problems.append(f"{rid}: bad status {status!r}")
        elif status == "mapped":
            refs = [p.strip() for p in row.get("passages", "").split(";") if p.strip()]
            unknown = [p for p in refs if p not in passages]
            if not refs or unknown:
                problems.append(f"{rid}: mapped to unknown passages {unknown or '(none)'}")
            else:
                mapped += 1
        elif status == "gap" and rid not in gap_ids:
            problems.append(f"{rid}: gap not in the SME question list {gaps_rel}")
        elif status == "not_applicable" and reqs[rid].get("type") not in T.COMPLIANCE_ONLY_TYPES:
            problems.append(f"{rid}: {reqs[rid].get('type')} requirement cannot be not_applicable")
    problems += [f"{rid}: {n} rows" for rid, n in seen.items() if n > 1]
    problems += [f"{rid}: no evidence row" for rid in reqs if rid not in seen]
    content = [r for r in reqs.values() if r.get("type") not in T.COMPLIANCE_ONLY_TYPES]
    score = mapped / len(content) if content else 1.0
    passed = not problems
    return _result(passed, f"{mapped}/{len(content)} content requirements mapped"
                   + ("" if passed else f"; {_brief(problems)}"), round(score, 4))


@_guarded
def outline_budget_ok(workspace: Path, params: dict[str, Any], *, run=None) -> dict[str, Any]:
    """Outline page budgets fit the solicitation's page limits (recomputed)
    and every content requirement is covered by a heading. A questionnaire
    has no page limits and is covered by its answer sheet instead."""
    doc, problems = _load(workspace, params)
    if doc.get("mode") == "questionnaire":
        return _result(True, "questionnaire engagement; coverage is checked on the answer sheet", None)
    if problems:
        return _result(False, _brief(problems), 0.0)
    report = T.outline_budget(workspace, params.get("path", T.OUTLINE_PATH),
                              params.get("requirements", T.REQUIREMENTS_PATH))
    if not report["volumes"]:
        problems.append("outline has no top-level (#) volume headings")
    problems += [f"over the page limit: {v}" for v in report["over_limit"]]
    if report["uncovered"]:
        problems.append(f"requirements with no heading: {_brief(report['uncovered'])}")
    if report["unknown_ids"]:
        problems.append(f"unknown ids in [covers:]: {report['unknown_ids']}")
    summary = ", ".join(f"{v['volume']}: {v['pages']:g}/{v['limit'] if v['limit'] is not None else '-'}"
                        for v in report["volumes"])
    return _result(not problems, summary + ("" if not problems else f"; {_brief(problems)}"), None)


# --- M3: draft limits, grounding, questionnaire answers ------------------------

def _prose_words(text: str) -> int:
    text = T._CITATION.sub(" ", T._COMMENT.sub(" ", text))
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’.,%$-]*", text))


@_guarded
def draft_within_limits(workspace: Path, params: dict[str, Any], *, run=None) -> dict[str, Any]:
    """Each draft volume's estimated pages (words / words_per_page) fit the
    page limit, and sections named in a word limit fit it; limits are
    recomputed from the solicitation."""
    doc, problems = _load(workspace, params)
    if problems:
        return _result(False, _brief(problems), 0.0)
    rules = _recompute_rules(workspace, doc)
    wpp = float(params.get("words_per_page", 500))
    sections = T.draft_sections(T.read_text(workspace, params.get("path", T.DRAFT_PATH)))
    volumes: dict[str, int] = {}
    current = None
    for sec in sections:
        if sec["level"] == 1:
            current = sec["heading"]
            volumes.setdefault(current, 0)
        if current is not None:
            volumes[current] += _prose_words(sec["body"])
    notes = []
    for volume, words in volumes.items():
        rule = T.page_limit_for(volume, rules)
        if rule is None:
            continue
        pages = words / wpp
        notes.append(f"{volume}: ~{pages:.1f}/{rule['value']:g} pages")
        if pages > rule["value"]:
            problems.append(f"{volume} is ~{pages:.1f} pages; limit {rule['value']:g} ({rule['section']})")
    for rule in (r for r in rules if r["kind"] == "word_limit"):
        text = rule["text"].lower()
        for sec in sections:
            title = sec["heading"].lower()
            if len(title) > 3 and title in text:
                words = _prose_words(sec["body"])
                notes.append(f"{sec['heading']}: {words}/{rule['value']:g} words")
                if words > rule["value"]:
                    problems.append(f"{sec['heading']} has {words} words; limit {rule['value']:g}")
    return _result(not problems, "; ".join(problems or notes or ["no page or word limits apply"]), None)


@_guarded
def claims_grounded(workspace: Path, params: dict[str, Any], *, run=None) -> dict[str, Any]:
    """Every [KB:]/[REQ:] citation resolves, every number and certification
    in a cited sentence is in the cited text, and no uncited sentence states
    a company fact (money, percentages, counts, certifications)."""
    doc, problems = _load(workspace, params)
    if problems:
        return _result(False, _brief(problems), 0.0)
    scan = T.scan_grounding(T.read_text(workspace, params.get("path", T.DRAFT_PATH)),
                            T.kb_passages(workspace, params.get("kb_dir", T.KB_DIR)),
                            T._req_texts(doc))
    issues = len(scan["unresolved"]) + len(scan["unsupported"]) + len(scan["uncited_claims"])
    total = scan["cited_sentences"] + len(scan["uncited_claims"])
    if scan["unresolved"]:
        problems.append(f"unresolved citations {_brief(scan['unresolved'])}")
    for item in scan["unsupported"][:3]:
        problems.append(f"{item['not_in_cited_text']} not in cited text: {item['sentence'][:90]}")
    uncited = scan["uncited_claims"]
    if len(uncited) > int(params.get("max_uncited", 0)):
        problems.append(f"{len(uncited)} uncited claims, e.g. {uncited[0]['sentence'][:90]}")
    if scan["cited_sentences"] < int(params.get("min_citations", 1)):
        problems.append(f"only {scan['cited_sentences']} cited sentences")
    score = 1.0 - issues / total if total else 0.0
    return _result(not problems, "; ".join(problems) or f"{scan['cited_sentences']} cited sentences, all supported",
                   round(max(score, 0.0), 4))


@_guarded
def questionnaire_answers_grounded(workspace: Path, params: dict[str, Any], *, run=None) -> dict[str, Any]:
    """Questionnaire mode: one answer row per source question; answered rows
    cite KB passages that contain every number and certification in the
    answer; everything else abstains as NEEDS REVIEW. Passes trivially for an
    RFP engagement."""
    doc, problems = _load(workspace, params)
    if doc.get("mode") != "questionnaire":
        return _result(True, "not a questionnaire engagement; nothing to check", None)
    if problems:
        return _result(False, _brief(problems), 0.0)
    src = doc["sources"][0]["path"]
    questions = [q["text"] for q in T.questionnaire_rows(workspace, src)]
    expected = {r["id"]: r.get("text", "") for r in doc["requirements"]}
    if sorted(T.normalize_ws(t) for t in expected.values()) != sorted(T.normalize_ws(q) for q in questions):
        problems.append("requirements.json questions differ from the questionnaire file")
    passages = T.kb_passages(workspace, params.get("kb_dir", T.KB_DIR))
    rows = T.read_csv_rows(workspace, params.get("path", T.ANSWERS_PATH))
    seen: set[str] = set()
    answered = abstained = 0
    for row in rows:
        qid, status, answer = row.get("question_id", ""), row.get("status", ""), row.get("answer", "")
        if qid not in expected or qid in seen:
            problems.append(f"{qid}: unknown or duplicate question")
            continue
        seen.add(qid)
        if T.normalize_ws(row.get("question", "")) != T.normalize_ws(expected[qid]):
            problems.append(f"{qid}: question text altered")
        if status == "answered":
            refs = [c.strip() for c in row.get("citations", "").split(";") if c.strip()]
            unknown = [r for r in refs if r not in passages]
            if not answer.strip() or answer.startswith(T.NEEDS_REVIEW) or not refs or unknown:
                problems.append(f"{qid}: answered without a resolvable citation ({unknown or 'none'})")
                continue
            missing = T.unsupported_tokens(answer, " ".join(passages[r] for r in refs))
            if missing:
                problems.append(f"{qid}: {missing} not in the cited passages")
                continue
            answered += 1
        elif status == "needs_review":
            if not answer.startswith(T.NEEDS_REVIEW):
                problems.append(f"{qid}: needs_review rows must abstain ({T.NEEDS_REVIEW}: ...)")
                continue
            abstained += 1
        else:
            problems.append(f"{qid}: bad status {status!r}")
    problems += [f"{qid}: no answer row" for qid in expected if qid not in seen]
    ratio = answered / len(expected) if expected else 0.0
    if ratio < float(params.get("min_answered_ratio", 0.0)):
        problems.append(f"answered ratio {ratio:.2f} below {params['min_answered_ratio']}")
    details = f"{answered} answered, {abstained} abstained of {len(expected)}"
    return _result(not problems, details + ("" if not problems else f"; {_brief(problems)}"), round(ratio, 4))


CHECK_DEFS: dict[str, Callable[..., dict[str, Any]]] = {
    "shred_complete": shred_complete,
    "matrix_consistent": matrix_consistent,
    "format_rules_captured": format_rules_captured,
    "dates_match_source": dates_match_source,
    "evidence_map_complete": evidence_map_complete,
    "outline_budget_ok": outline_budget_ok,
    "draft_within_limits": draft_within_limits,
    "claims_grounded": claims_grounded,
    "questionnaire_answers_grounded": questionnaire_answers_grounded,
}
