"""
specialists/proposal_writer/checks.py - acceptance checks for the
proposal-writer specialist.

Each check is fn(workspace, params, *, run=None) -> {"passed", "details",
"score"} and recomputes from the source documents in inputs/ (solicitation,
questionnaire, knowledge base) with the same parsers the tools use. The
agent's own files are compared against that recomputation, never trusted.
Every check first validates requirements.json itself (_load): its sources
are every solicitation file under inputs/ and unchanged since the shred
(sha256), its mode follows the source, and every row is verbatim from a
source with a well-formed, unique id and the shredder's type. Beyond that,
a matrix row marked addressed with no marker in a substantive draft section,
a cited number or certification that is not in the cited passage, or a
page limit that the draft's headings dodge all fail.
"""
from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agentkit.errors import AgentKitError
from specialists.proposal_writer import tools as T

BRIEF_PATH = "deliverables/m1-shred/bid-brief.md"
GAPS_PATH = "deliverables/m2-outline/gaps.md"
EVIDENCE_STATUSES = {"mapped", "gap", "not_applicable"}
_KEY_DATES = re.compile(r"\bkey dates\b", re.I)


def _result(passed: bool | None, details: str, score: float | None = None) -> dict[str, Any]:
    return {"passed": passed, "details": details, "score": score}


def _guarded(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """A missing or malformed deliverable is a failed check, not a crash."""
    @functools.wraps(fn)
    def wrapper(workspace: Path, params: dict | None = None, *, run=None) -> dict[str, Any]:
        try:
            return fn(Path(workspace), dict(params or {}), run=run)
        except (AgentKitError, OSError, ValueError, KeyError, TypeError) as exc:
            return _result(False, f"{fn.__name__}: {exc}", 0.0)
    return wrapper


def _brief(items: list[str], limit: int = 5) -> str:
    shown = "; ".join(str(i) for i in items[:limit])
    return shown + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


# --- requirements.json, validated against the sources ------------------------------

def source_mode(doc: dict[str, Any]) -> str:
    """"questionnaire" when the shred's only source is a CSV, else "rfp": the
    mode follows the source, not the mode field the agent could edit."""
    sources = doc.get("sources") or []
    single_csv = len(sources) == 1 and str(sources[0].get("path", "")).lower().endswith(".csv")
    return "questionnaire" if single_csv else "rfp"


def _key(workspace: Path, rel: str) -> str:
    return T.relpath(workspace, T.resolve(workspace, rel)).lower()


def verified_sources(workspace: Path, doc: dict[str, Any], prefixes: list[str],
                     kb_dir: str = T.KB_DIR) -> list[str]:
    """Problems with the recorded sources: outside the trusted prefixes, in
    the knowledge base, missing, or changed since the shred (sha256
    mismatch); and solicitation files under inputs/ (outside the knowledge
    base) that were never shredded."""
    problems = []
    sources = doc.get("sources") or []
    if not sources:
        problems.append("requirements.json lists no sources")
    kb = _key(workspace, kb_dir) + "/"
    recorded = set()
    for src in sources:
        rel = str(src.get("path", ""))
        # judged on the normalized path: "inputs/../work/x.md" is not an input
        if not _key(workspace, rel).startswith(tuple(p.lower() for p in prefixes)):
            problems.append(f"source {rel!r} is not under {prefixes}")
            continue
        path = T.resolve(workspace, rel)
        recorded.add(_key(workspace, rel))
        if _key(workspace, rel).startswith(kb):
            problems.append(f"source {rel} is in the knowledge base, not the solicitation")
        elif not path.is_file():
            problems.append(f"source {rel} is missing")
        elif T.sha256_file(path) != src.get("sha256"):
            problems.append(f"source {rel} changed since the shred (sha256 mismatch)")
    inputs = T.resolve(workspace, "inputs")
    if inputs.is_dir():
        for path in sorted(inputs.rglob("*")):
            rel = T.relpath(workspace, path)
            if (path.is_file() and path.suffix.lower() in T.DOCUMENT_SUFFIXES
                    and not rel.lower().startswith(kb) and rel.lower() not in recorded):
                problems.append(f"{rel} is not shredded (every solicitation file is a source)")
    return problems


def _questions(workspace: Path, src: dict[str, Any]) -> list[dict[str, Any]]:
    """A questionnaire source's questions, read with the columns the shred
    recorded (auto-detected when it recorded none)."""
    return T.questionnaire_rows(workspace, src["path"], src.get("question_column") or None,
                                src.get("id_column") or None)


@dataclass
class Loaded:
    doc: dict[str, Any]
    problems: list[str]
    mode: str
    texts: dict[str, str] = field(default_factory=dict)       # source path -> text
    expected: list[dict[str, Any]] = field(default_factory=list)  # recomputed rows


def _load(workspace: Path, params: dict[str, Any]) -> Loaded:
    """requirements.json plus every problem with it; checks fail on any."""
    doc = T.load_requirements(workspace, params.get("requirements", T.REQUIREMENTS_PATH))
    kb_dir = params.get("kb_dir", T.KB_DIR)
    problems = verified_sources(workspace, doc, params.get("source_prefixes", ["inputs/"]), kb_dir)
    mode = source_mode(doc)
    if doc.get("mode") != mode:
        problems.append(f"requirements.json says mode {doc.get('mode')!r} but its source makes it {mode!r}")
    loaded = Loaded(doc, problems, mode)
    readable = [s for s in doc.get("sources") or [] if T.resolve(workspace, str(s.get("path", ""))).is_file()]
    loaded.texts = {s["path"]: T.read_text(workspace, s["path"]) for s in readable}
    if mode == "questionnaire":
        loaded.expected = [{"text": q["text"], "source": s["path"], "section": q["section"], "page": None,
                            "type": "question"} for s in readable for q in _questions(workspace, s)]
    else:
        loaded.expected = [r for s, text in loaded.texts.items() for r in T.shred_text(text, s)]
    problems += _row_problems(loaded)
    rows = doc["requirements"]
    if len(rows) < int(params.get("min_requirements", 1)):
        problems.append(f"{len(rows)} requirements in the shred; the solicitation must yield at least "
                        f"{int(params.get('min_requirements', 1))}")
    return loaded


def _row_problems(loaded: Loaded) -> list[str]:
    """Rows that are not verbatim from a source, ids that are malformed or
    repeated, and shredded rows whose type was changed (the type decides
    whether a checklist marker or not_applicable can stand in for content)."""
    rows = loaded.doc["requirements"]
    ids = [str(r.get("id", "")) for r in rows]
    problems = []
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    bad = [i for i in ids if not re.fullmatch(r"[A-Z]{1,4}-\d{1,4}", i)]
    if dupes:
        problems.append(f"duplicate ids {dupes}")
    if bad:
        problems.append(f"malformed ids {bad[:5]}")
    if loaded.mode == "questionnaire":
        allowed: dict[str, set[str]] = {}
        for exp in loaded.expected:
            allowed.setdefault(exp["source"], set()).add(T.normalize_ws(exp["text"]))
        forged = [r.get("id") for r in rows
                  if T.normalize_ws(str(r.get("text", ""))) not in allowed.get(r.get("source"), set())]
    else:
        sources = {s: T.normalize_ws(t) for s, t in loaded.texts.items()}
        forged = [r.get("id") for r in rows
                  if not T.normalize_ws(str(r.get("text", "")))
                  or T.normalize_ws(str(r.get("text", ""))) not in sources.get(r.get("source"), "")]
    if forged:
        problems.append(f"not verbatim in source: {_brief(forged)}")
    types: dict[tuple, set[str]] = {}
    for exp in loaded.expected:
        types.setdefault((exp["source"], T.normalize_ws(exp["text"])), set()).add(exp["type"])
    retyped = [f"{r.get('id')} ({r.get('type')} -> {'/'.join(sorted(want))})" for r in rows
               if (want := types.get((r.get("source"), T.normalize_ws(str(r.get("text", ""))))))
               and r.get("type") not in want]
    if retyped:
        problems.append(f"type differs from the shredder's: {_brief(retyped)}")
    return problems


# --- M1: shred, matrix, format rules, dates ------------------------------------

@_guarded
def shred_complete(workspace: Path, params: dict[str, Any], *, run=None) -> dict[str, Any]:
    """Every binding statement the deterministic shredder finds is in
    requirements.json with the right section/page (a sentence repeated in
    several sections needs one row per occurrence), and every row is
    verbatim from a trusted, unchanged source."""
    loaded = _load(workspace, params)
    if loaded.problems:
        return _result(False, _brief(loaded.problems), 0.0)
    rows = loaded.doc["requirements"]
    questionnaire = loaded.mode == "questionnaire"
    pool: dict[tuple, list[dict[str, Any]]] = {}
    for r in rows:
        pool.setdefault((r.get("source"), T.normalize_ws(str(r.get("text", "")))), []).append(r)

    def placed(row: dict[str, Any], exp: dict[str, Any]) -> bool:
        return questionnaire or (row.get("page") == exp["page"] and row.get("section") == exp["section"])

    unplaced = []
    for exp in loaded.expected:      # first pass: rows at the right place
        rows_for = pool.get((exp["source"], T.normalize_ws(exp["text"])), [])
        row = next((r for r in rows_for if placed(r, exp)), None)
        if row is None:
            unplaced.append(exp)
        else:
            rows_for.remove(row)
    missing, misplaced = [], []
    for exp in unplaced:             # second pass: the same text elsewhere
        rows_for = pool.get((exp["source"], T.normalize_ws(exp["text"])), [])
        if rows_for:
            misplaced.append(f"{rows_for.pop(0).get('id')} (expected {exp['section']} p{exp['page']})")
        else:
            missing.append(exp["text"][:80])
    expected = len(loaded.expected)
    recall = 1.0 if not expected else (expected - len(missing)) / expected
    passed = not misplaced and recall >= float(params.get("min_recall", 1.0))
    parts = [f"{len(rows)} rows; recall {recall:.2f} of {expected} binding statements"]
    if missing:
        parts.append(f"missing: {_brief(missing, 3)}")
    if misplaced:
        parts.append(f"wrong section/page: {_brief(misplaced)}")
    return _result(passed, "; ".join(parts), round(recall, 4))


@_guarded
def matrix_consistent(workspace: Path, params: dict[str, Any], *, run=None) -> dict[str, Any]:
    """The compliance matrix has exactly one row per requirement with its
    verbatim text; with require_addressed, every row's status and location
    match a recomputation: addressed where a substantive draft section (or,
    for format/submission/form rows, the checklist) carries its marker,
    open_item when the checklist lists it under Open items, and for a
    questionnaire the answer sheet's answered/needs_review."""
    loaded = _load(workspace, params)
    doc, problems = loaded.doc, list(loaded.problems)
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
        found, notes = T.response_status(workspace, doc, params.get("draft", T.DRAFT_PATH),
                                         params.get("checklist", T.CHECKLIST_PATH),
                                         int(params.get("min_section_words", T.MIN_SECTION_WORDS)))
        answers_rel = params.get("answers", T.ANSWERS_PATH)
        answer_status = {}
        if loaded.mode == "questionnaire" and T.resolve(workspace, answers_rel).is_file():
            answer_status = {a.get("question_id"): a.get("status") for a in T.read_csv_rows(workspace, answers_rel)}
        for row in rows:
            rid, status = row.get("req_id", ""), row.get("status", "")
            if rid not in reqs:
                continue
            where = [w.strip() for w in row.get("response_section", "").split(";") if w.strip()]
            if loaded.mode == "questionnaire":
                expected = {"answered": "addressed", "needs_review": "needs_review"}.get(answer_status.get(rid, ""))
                if expected is None:
                    problems.append(f"{rid}: no answer on the answer sheet")
                elif status != expected:
                    problems.append(f"{rid}: status {status!r}, answer sheet says {answer_status[rid]}")
                else:
                    addressed += expected == "addressed"
            elif rid in found:
                want = found[rid]
                if status != want["status"] or not where or not set(where) <= set(want["where"]):
                    problems.append(f"{rid}: matrix says {status!r} at {where}; recomputed "
                                    f"{want['status']!r} at {want['where']}")
                else:
                    addressed += want["status"] == "addressed"
            else:
                problems.append(f"{rid}: not addressed ({notes.get(rid, 'no marker in the draft or checklist')})")
    score = addressed / len(reqs) if params.get("require_addressed") and reqs else None
    passed = not problems
    return _result(passed, "ok" if passed else _brief(problems), score)


@_guarded
def format_rules_captured(workspace: Path, params: dict[str, Any], *, run=None) -> dict[str, Any]:
    """format_rules.json holds every rule recomputed from the solicitation
    and nothing that is not stated there."""
    loaded = _load(workspace, params)
    if loaded.problems:
        return _result(False, _brief(loaded.problems), 0.0)
    data = json.loads(T.resolve(workspace, params.get("rules", T.FORMAT_RULES_PATH)).read_text(encoding="utf-8"))
    reported = data.get("rules", []) if isinstance(data, dict) else []
    expected = T.solicitation_rules(workspace, loaded.doc)
    have = {T.rule_key(r.get("kind"), r.get("value")) for r in reported}
    want = {T.rule_key(r["kind"], r["value"]) for r in expected}
    sources = " ".join(T.normalize_ws(t) for t in loaded.texts.values())
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


def _section_text(text: str, heading: re.Pattern) -> str | None:
    """The body of the first section whose heading matches, with its
    subsections; None when no heading matches."""
    out, level = [], None
    for sec in T.draft_sections(text):
        if level is not None and sec["level"] <= level:
            break
        if level is None and sec["level"] and heading.search(sec["heading"]):
            level = sec["level"]
        if level is not None:
            out.append(sec["heading"] + "\n" + sec["body"])
    return "\n".join(out) if level is not None else None


@_guarded
def dates_match_source(workspace: Path, params: dict[str, Any], *, run=None) -> dict[str, Any]:
    """Every date in the bid brief's Key dates section appears in the
    solicitation, and every deadline-like date in the solicitation appears
    there. The customer's own schedule (internal review, SME deadlines) goes
    in another section. A questionnaire's due date comes from the customer,
    so it is skipped there."""
    loaded = _load(workspace, params)
    if loaded.problems:
        return _result(False, _brief(loaded.problems), 0.0)
    if loaded.mode == "questionnaire":
        return _result(True, "questionnaire engagement; dates come from intake", None)
    found = []
    for src, text in loaded.texts.items():
        found.extend(T.dates_text(text, src))
    source_dates = {d["date"] for d in found}
    deadlines = {d["date"] for d in found if d["deadline"]}
    brief = T.read_text(workspace, params.get("path", BRIEF_PATH))
    key_dates = _section_text(brief, _KEY_DATES)
    brief_dates = {iso for iso, _ in T.find_dates(brief if key_dates is None else key_dates)}
    invented = sorted(brief_dates - source_dates)
    omitted = sorted(deadlines - brief_dates)
    passed = not invented and not omitted
    where = "brief" if key_dates is None else "Key dates"
    details = f"{len(brief_dates)} dates in {where}, {len(deadlines)} deadlines in the solicitation"
    if invented:
        details += f"; not in the solicitation: {invented}"
    if omitted:
        details += f"; deadlines missing from {where}: {omitted}"
    return _result(passed, details, None)


# --- M2: evidence map, gaps, outline -------------------------------------------

@_guarded
def evidence_map_complete(workspace: Path, params: dict[str, Any], *, run=None) -> dict[str, Any]:
    """Each requirement has one evidence row: mapped to KB passages that
    exist and share a term with the requirement, a gap listed in the SME
    question list, or not_applicable for format/submission/form rows only."""
    loaded = _load(workspace, params)
    doc, problems = loaded.doc, list(loaded.problems)
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
            elif not any(T.passage_relevant(reqs[rid].get("text", ""), p, passages[p]) for p in refs):
                problems.append(f"{rid}: no mapped passage shares a term with the requirement; "
                                "map a relevant passage or list it as a gap")
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


def _limit_problems(report: dict[str, Any], unit: str) -> list[str]:
    problems = [f"{c['volume']} is {c['pages']:g} pages; limit {c['limit']:g} ({c['limit_source']})"
                for c in report["volumes"] if c["limit"] is not None and c["pages"] > c["limit"]]
    if report["outside_over"]:
        problems.append(f"{report['outside_pages']:g} pages of {unit} sit outside every page-limited "
                        "volume or section; put them under the volume they belong to"
                        + (f" (limits that match no heading: {_brief(report['unmatched_limits'], 2)})"
                           if report["unmatched_limits"] else ""))
    return problems


@_guarded
def outline_budget_ok(workspace: Path, params: dict[str, Any], *, run=None) -> dict[str, Any]:
    """Outline page budgets fit the solicitation's page limits (recomputed),
    no budget escapes them under a heading no limit names, and every content
    requirement is covered by a heading. A questionnaire has no page limits
    and is covered by its answer sheet instead."""
    loaded = _load(workspace, params)
    if loaded.problems:
        return _result(False, _brief(loaded.problems), 0.0)
    if loaded.mode == "questionnaire":
        return _result(True, "questionnaire engagement; coverage is checked on the answer sheet", None)
    report = T.outline_budget(workspace, params.get("path", T.OUTLINE_PATH),
                              params.get("requirements", T.REQUIREMENTS_PATH))
    problems = []
    if not report["headings"]:
        problems.append("outline has no headings")
    problems += _limit_problems(report, "budget")
    if report["uncovered"]:
        problems.append(f"requirements with no heading: {_brief(report['uncovered'])}")
    if report["unknown_ids"]:
        problems.append(f"unknown ids in [covers:]: {report['unknown_ids']}")
    summary = ", ".join(f"{v['volume']}: {v['pages']:g}/{v['limit'] if v['limit'] is not None else '-'}"
                        for v in report["volumes"])
    return _result(not problems, summary + ("" if not problems else f"; {_brief(problems)}"), None)


# --- M3: draft limits, grounding, questionnaire answers ------------------------

def words_per_page(params: dict[str, Any], rules: list[dict[str, Any]]) -> float:
    """Words per estimated page: words_per_page (default 500, single-spaced
    12 point), halved when the solicitation requires double spacing and
    reduced for a required font above 12 point."""
    wpp = float(params.get("words_per_page", 500))
    if any(r["kind"] == "spacing" and str(r["value"]).lower() == "double" for r in rules):
        wpp /= 2
    fonts = [float(r["value"]) for r in rules if r["kind"] == "font_size"]
    if fonts and max(fonts) > 12:
        wpp *= (12 / max(fonts)) ** 2
    return wpp


@_guarded
def draft_within_limits(workspace: Path, params: dict[str, Any], *, run=None) -> dict[str, Any]:
    """Each page-limited volume or section's estimated pages (words /
    words per page) fit its limit, no more than a page of prose sits outside
    every limited section, and sections named in a word limit fit it; limits
    are recomputed from the solicitation."""
    loaded = _load(workspace, params)
    if loaded.problems:
        return _result(False, _brief(loaded.problems), 0.0)
    rules = T.solicitation_rules(workspace, loaded.doc)
    wpp = words_per_page(params, rules)
    sections = T.draft_sections(T.read_text(workspace, params.get("path", T.DRAFT_PATH)))
    report = T.page_limit_report(
        [{"heading": s["heading"], "level": s["level"], "amount": T.prose_words(s["body"]) / wpp}
         for s in sections], rules, float(params.get("unlimited_pages", T.UNLIMITED_PAGES)))
    problems = _limit_problems(report, "prose")
    notes = [f"{c['volume']}: ~{c['pages']:.1f}/{c['limit']:g} pages" for c in report["volumes"]
             if c["limit"] is not None]
    for rule in (r for r in rules if r["kind"] == "word_limit"):
        text = rule["text"].lower()
        for sec in sections:
            title = sec["heading"].lower()
            if len(title) > 3 and title in text:
                words = T.prose_words(sec["body"])
                notes.append(f"{sec['heading']}: {words}/{rule['value']:g} words")
                if words > rule["value"]:
                    problems.append(f"{sec['heading']} has {words} words; limit {rule['value']:g}")
    if notes:
        notes.append(f"{wpp:g} words per page")
    return _result(not problems, "; ".join(problems or notes or ["no page or word limits apply"]), None)


@_guarded
def claims_grounded(workspace: Path, params: dict[str, Any], *, run=None) -> dict[str, Any]:
    """Every [KB:]/[REQ:] citation resolves; every company fact carries a
    [KB:] citation whose passage holds its numbers and certifications; a
    clause cited only to the solicitation restates a requirement figure and
    names no certification; no uncited sentence states a company fact."""
    loaded = _load(workspace, params)
    if loaded.problems:
        return _result(False, _brief(loaded.problems), 0.0)
    scan = T.scan_grounding(T.read_text(workspace, params.get("path", T.DRAFT_PATH)),
                            T.kb_passages(workspace, params.get("kb_dir", T.KB_DIR)),
                            T._req_texts(loaded.doc))
    problems = []
    issues = len(scan["unresolved"]) + len(scan["unsupported"]) + len(scan["uncited_claims"])
    total = scan["cited_sentences"] + len(scan["uncited_claims"])
    if scan["unresolved"]:
        problems.append(f"unresolved citations {_brief(scan['unresolved'])}")
    for item in scan["unsupported"][:3]:
        reasons = ([f"{item['not_in_cited_text']} not in cited text"] if item["not_in_cited_text"] else [])
        problems.append(f"{'; '.join(reasons + item.get('notes', []))}: {item['sentence'][:90]}")
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
    answer, name every certification the question asks about and share a
    term with it; everything else abstains as NEEDS REVIEW. Passes trivially
    for an RFP engagement."""
    loaded = _load(workspace, params)
    if loaded.problems:
        return _result(False, _brief(loaded.problems), 0.0)
    if loaded.mode != "questionnaire":
        return _result(True, "not a questionnaire engagement; nothing to check", None)
    problems = []
    doc = loaded.doc
    questions = [q["text"] for q in _questions(workspace, doc["sources"][0])]
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
            why = T.answer_problems(expected[qid], answer, refs, passages)
            if why:
                problems.append(f"{qid}: {'; '.join(why)}")
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
