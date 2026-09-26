"""
specialists/support_automation/tools.py - deterministic domain tools for the
support-automation specialist.

Each tool is a plain function fn(workspace, *, resolve_path=None, **args)
returning a dict, listed in TOOL_DEFS; the kit wraps them (tools_from_defs)
and hands them resolve_path, so every path the model supplies goes through
the policy gate: jailed to the workspace, inputs/ read-only, .agentkit/
refused, and every file a tool writes marked agent-authored (never a ledger
source). Called directly (by checks.py or tests) they fall back to the same
path rules without the authored bookkeeping. The model decides what the
intents are and writes the articles; these tools do the counting, redaction,
matching and replay so the numbers in every deliverable are reproducible.

Workspace files the tools understand:

    inputs/tickets.csv          ticket export: ticket_id, subject, body,
                                must_escalate (gold label, 1/0), plus optional
                                agent_reply, handle_minutes, status
    inputs/help_center/*.md     current help-center articles (.txt too);
                                optional front matter "intents: a, b", "title: ..."
    intent_rules.json           {"intents": [{"id", "label", "keywords": [...],
                                "automation": answer_only|with_tools|human_only}]}
    agent_config.json           {"escalation_rules": [{"category", "keywords": [...]}], ...}

Keywords (intent rules and escalation rules alike) match case-insensitively
at the start of a word, with any whitespace between the words of a phrase:
"sue" matches "sue" and "sued" but not "issue"; "cancel" matches
"cancelled". A keyword needs at least MIN_KEYWORD_CHARS characters.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from agentkit.errors import PolicyViolation
from agentkit.policy import PolicyGate

AUTOMATION_LEVELS = ("answer_only", "with_tools", "human_only")
OTHER_INTENT = "other"
ID_COLUMN = "ticket_id"
LABEL_COLUMN = "must_escalate"
REQUIRED_COLUMNS = (ID_COLUMN, "subject", "body", LABEL_COLUMN)
LABEL_VALUES = {"1": True, "true": True, "yes": True, "y": True,
                "0": False, "false": False, "no": False, "n": False}
MIN_KEYWORD_CHARS = 3
M1 = "deliverables/m1-discovery"
KB_SUFFIXES = (".md", ".txt")

# --- PII patterns -----------------------------------------------------------
# Emails by shape; card numbers only when the digit run passes the Luhn
# check, so order ids and dates are left alone; phone numbers in the forms
# below, each kept only when its digit count fits the form.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
PHONE_FORMS = (
    # North American with separators: (555) 201-3344, 555.201.3344, +1 555 201 3344
    (re.compile(r"(?<!\w)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\w)"), 10, 11),
    # international with a leading +: +44 20 7946 0958, +49 (30) 1234567
    (re.compile(r"(?<![\w+])\+\d{1,3}(?:[\s.-]?\(?\d{1,5}\)?){1,5}(?!\w)"), 8, 15),
    # national with a trunk 0: 07700 900123, 020 7946 0958
    (re.compile(r"(?<![\w+])0\d{1,4}(?:[\s.-]\d{2,6}){1,3}(?!\w)"), 10, 11),
    # bare digit runs of phone length: 5550142231, 15550142231
    (re.compile(r"(?<![\w.,/-])\d{10,11}(?![\w/-])"), 10, 11),
)

REDACTION_TOKENS = {"email": "[EMAIL]", "phone": "[PHONE]", "card": "[CARD]"}


Resolver = Callable[..., Path]
_GATE = PolicyGate()


def _resolve(workspace: Path, rel: str, *, write: bool = False) -> Path:
    """Workspace-relative path under the kit's path rules (no escape, lexical
    check before any filesystem access, inputs/ read-only, .agentkit/
    refused). Raises ValueError so checks report a readable failure."""
    try:
        return _GATE.resolve_path(Path(workspace), rel, write=write)
    except PolicyViolation as exc:
        raise ValueError(str(exc)) from None


def _resolver(workspace: Path, resolve_path: Resolver | None) -> Resolver:
    """The kit's resolve_path inside a run; _resolve for direct calls."""
    if resolve_path is not None:
        return resolve_path
    return lambda rel, *, write=False: _resolve(workspace, rel, write=write)


def _dir_files(res: Resolver, rel_dir: str, suffixes: Iterable[str] | None = (".md",)) -> list[Path]:
    """Files directly inside a workspace directory (only `suffixes`, or all
    files with None), each resolved through `res` so a link pointing out of
    the workspace fails."""
    root = res(rel_dir)
    if not root.is_dir():
        return []
    base = rel_dir.replace("\\", "/").rstrip("/")
    wanted = tuple(s.lower() for s in suffixes) if suffixes is not None else None
    return [res(f"{base}/{p.name}") for p in sorted(root.iterdir())
            if p.is_file() and (wanted is None or p.suffix.lower() in wanted)]


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Header and rows of a CSV. A UTF-8 byte-order mark (Excel's "CSV UTF-8")
    and spaces around column names are dropped; short rows read as ""."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        header = [h.strip() for h in next(reader, [])]
        rows = [{h: (cells[i] if i < len(cells) else "") for i, h in enumerate(header)}
                for cells in reader if cells]
    return header, rows


def _write_csv(path: Path, header: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def require_ids(header: list[str], where: str) -> None:
    if ID_COLUMN not in header:
        raise ValueError(f"{where} has no {ID_COLUMN} column (columns: {', '.join(header) or 'none'})")


def luhn_ok(digits: str) -> bool:
    """True when a 13-19 digit string passes the Luhn checksum."""
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _card(m: re.Match) -> bool:
    return luhn_ok(re.sub(r"\D", "", m.group()))


def _phone_spans(text: str) -> list[tuple[int, int]]:
    """Non-overlapping (start, end) spans of phone numbers, left to right."""
    spans: list[tuple[int, int]] = []
    for pattern, lo, hi in PHONE_FORMS:
        for m in pattern.finditer(text):
            if lo <= len(re.sub(r"\D", "", m.group())) <= hi:
                spans.append(m.span())
    out: list[tuple[int, int]] = []
    for start, end in sorted(spans, key=lambda s: (s[0], -s[1])):
        if out and start < out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def find_pii(text: str) -> dict[str, list[str]]:
    """Every email, phone number and Luhn-valid card number in `text`."""
    text = text or ""
    found: dict[str, list[str]] = {"email": EMAIL_RE.findall(text), "phone": [],
                                   "card": [m.group() for m in CARD_RE.finditer(text) if _card(m)]}
    scrubbed = CARD_RE.sub(lambda m: " " if _card(m) else m.group(), text)
    scrubbed = EMAIL_RE.sub(" ", scrubbed)
    found["phone"] = [scrubbed[a:b] for a, b in _phone_spans(scrubbed)]
    return found


def redact_text(text: str) -> tuple[str, dict[str, int]]:
    """Replace PII with typed tokens; returns (text, counts by kind)."""
    counts = {"email": 0, "phone": 0, "card": 0}

    def card(m: re.Match) -> str:
        if _card(m):
            counts["card"] += 1
            return REDACTION_TOKENS["card"]
        return m.group()

    def email(m: re.Match) -> str:
        counts["email"] += 1
        return REDACTION_TOKENS["email"]

    out = CARD_RE.sub(card, text or "")          # cards first: they look like phones
    out = EMAIL_RE.sub(email, out)
    for start, end in reversed(_phone_spans(out)):
        counts["phone"] += 1
        out = out[:start] + REDACTION_TOKENS["phone"] + out[end:]
    return out, counts


def _text_columns(header: list[str], columns: list[str] | None) -> list[str]:
    """Columns to redact: the ones asked for, else every column but the id."""
    return [c for c in (columns or header) if c in header and c != ID_COLUMN]


def redacted_export(workspace: Path, source: str, res: Resolver | None = None
                    ) -> tuple[list[str], list[dict[str, str]]]:
    """The ticket export with every column but the id redacted, in memory:
    what redact_tickets writes by default. Checks recompute from this rather
    than from a copy the agent wrote."""
    header, rows = _read_csv(_resolver(workspace, res)(source))
    require_ids(header, source)
    cols = _text_columns(header, None)
    for row in rows:
        for col in cols:
            row[col] = redact_text(row.get(col) or "")[0]
    return header, rows


# --- keywords ---------------------------------------------------------------

_KEYWORD_CACHE: dict[str, re.Pattern] = {}


def _keyword_re(keyword: str) -> re.Pattern:
    key = " ".join(keyword.lower().split())
    pattern = _KEYWORD_CACHE.get(key)
    if pattern is None:
        pattern = re.compile(r"(?<!\w)" + r"\s+".join(re.escape(w) for w in key.split(" ")))
        _KEYWORD_CACHE[key] = pattern
    return pattern


def keyword_hit(keyword: str, text: str) -> bool:
    """True when `keyword` starts at a word boundary somewhere in `text`
    (case-insensitive; see the module docstring)."""
    return bool(_keyword_re(keyword).search((text or "").lower()))


def keyword_problems(keywords: Any, where: str) -> list[str]:
    """Why a keyword list is unusable: not a list, empty, or holding
    anything but text of at least MIN_KEYWORD_CHARS characters (a bare
    string or "" would match nearly every ticket)."""
    if not isinstance(keywords, list) or not keywords:
        return [f"{where}: keywords must be a non-empty list of phrases"]
    return [f"{where}: keyword {kw!r} must be text of at least {MIN_KEYWORD_CHARS} characters"
            for kw in keywords if not isinstance(kw, str) or len(kw.strip()) < MIN_KEYWORD_CHARS]


def _usable(keywords: Any) -> list[str]:
    if not isinstance(keywords, list):
        return []
    return [kw for kw in keywords if isinstance(kw, str) and len(kw.strip()) >= MIN_KEYWORD_CHARS]


# --- tools ------------------------------------------------------------------

def redact_tickets(workspace: Path, *, resolve_path: Resolver | None = None,
                   input_path: str = "inputs/tickets.csv",
                   output_path: str = f"{M1}/tickets_redacted.csv",
                   columns: list[str] | None = None, **_: Any) -> dict:
    """Redact emails, phones and card numbers from every column of a ticket
    CSV except ticket_id (or only `columns`). Row count, order and ids are
    preserved; counts are returned."""
    res = _resolver(workspace, resolve_path)
    header, rows = _read_csv(res(input_path))
    require_ids(header, input_path)
    cols = _text_columns(header, columns)
    totals = {"email": 0, "phone": 0, "card": 0}
    for row in rows:
        for col in cols:
            row[col], counts = redact_text(row.get(col) or "")
            for k, v in counts.items():
                totals[k] += v
    _write_csv(res(output_path, write=True), header, rows)
    return {"output_path": output_path, "rows": len(rows), "columns": cols, "redactions": totals}


def scan_pii(workspace: Path, *, resolve_path: Resolver | None = None, path: str,
             columns: list[str] | None = None, **_: Any) -> dict:
    """Count PII left in a CSV (every column, or `columns`) or any text file."""
    target = _resolver(workspace, resolve_path)(path)
    hits: list[dict] = []
    if target.suffix.lower() == ".csv":
        header, rows = _read_csv(target)
        cols = columns or header
        for i, row in enumerate(rows):
            for col in cols:
                for kind, values in find_pii(row.get(col) or "").items():
                    hits += [{"row": i + 1, "column": col, "kind": kind} for _ in values]
    else:
        for kind, values in find_pii(target.read_text(encoding="utf-8")).items():
            hits += [{"kind": kind} for _ in values]
    counts = {k: sum(1 for h in hits if h["kind"] == k) for k in ("email", "phone", "card")}
    return {"path": path, "total": len(hits), "counts": counts, "hits": hits[:50]}


def load_intent_rules(workspace: Path, rules_path: str, res: Resolver | None = None) -> list[dict]:
    """Parse and validate intent_rules.json; raises ValueError on bad shape."""
    data = json.loads(_resolver(workspace, res)(rules_path).read_text(encoding="utf-8"))
    intents = data.get("intents") if isinstance(data, dict) else None
    if not isinstance(intents, list) or not intents:
        raise ValueError("intent rules need a non-empty 'intents' list")
    seen = set()
    for item in intents:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            raise ValueError(f"intent needs a text id: {item!r}")
        problems = keyword_problems(item.get("keywords"), f"intent {item['id']}")
        if problems:
            raise ValueError("; ".join(problems))
        if item["id"] in seen or item["id"] == OTHER_INTENT:
            raise ValueError(f"duplicate or reserved intent id: {item['id']}")
        if item.get("automation", "answer_only") not in AUTOMATION_LEVELS:
            raise ValueError(f"automation must be one of {AUTOMATION_LEVELS}: {item['id']}")
        seen.add(item["id"])
    return intents


def classify_text(text: str, intents: list[dict]) -> str:
    """The intent with the most keyword hits wins; ties go to the earlier
    intent in the rules file; no hit -> 'other'."""
    low = (text or "").lower()
    best, best_hits = OTHER_INTENT, 0
    for item in intents:
        hits = sum(1 for kw in _usable(item.get("keywords")) if _keyword_re(kw).search(low))
        if hits > best_hits:
            best, best_hits = item["id"], hits
    return best


def label_rows(rows: list[dict], intents: list[dict]) -> list[dict]:
    """Set row["intent"] from subject and body; returns the rows."""
    for row in rows:
        row["intent"] = classify_text(f"{row.get('subject', '')} {row.get('body', '')}", intents)
    return rows


def intent_volumes(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        out[row["intent"]] = out.get(row["intent"], 0) + 1
    return out


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compute_taxonomy(rows: list[dict], intents: list[dict]) -> list[dict]:
    """Volume, share, mean handle time and escalation rate per intent."""
    total = len(rows) or 1
    by_intent: dict[str, list[dict]] = {}
    for row in rows:
        by_intent.setdefault(row["intent"], []).append(row)
    meta = {i["id"]: i for i in intents}
    out = []
    for intent_id in [i["id"] for i in intents] + [OTHER_INTENT]:
        group = by_intent.get(intent_id, [])
        minutes = [m for m in (_to_float(r.get("handle_minutes")) for r in group) if m is not None]
        escalated = sum(1 for r in group if (r.get("status") or "").lower() == "escalated")
        out.append({
            "intent": intent_id,
            "label": meta.get(intent_id, {}).get("label", "Unclassified"),
            "automation": meta.get(intent_id, {}).get("automation", "human_only"),
            "volume": len(group),
            "share": round(len(group) / total, 4),
            "avg_handle_minutes": round(sum(minutes) / len(minutes), 1) if minutes else "",
            "escalation_rate": round(escalated / len(group), 4) if group else 0.0,
        })
    out.sort(key=lambda r: (r["intent"] == OTHER_INTENT, -r["volume"], r["intent"]))
    return out


TAXONOMY_HEADER = ["intent", "label", "automation", "volume", "share", "avg_handle_minutes", "escalation_rate"]


def build_intent_taxonomy(workspace: Path, *, resolve_path: Resolver | None = None,
                          tickets_path: str = f"{M1}/tickets_redacted.csv",
                          rules_path: str = f"{M1}/intent_rules.json",
                          labeled_path: str = f"{M1}/tickets_labeled.csv",
                          output_path: str = f"{M1}/intent_taxonomy.csv",
                          holdout_path: str = f"{M1}/eval_holdout.csv",
                          **_: Any) -> dict:
    """Label every ticket with the keyword rules and write the taxonomy table
    (volume, share, handle time, escalation rate, automation level). Once the
    held-out split exists, the labeled file marks each ticket's split and the
    result lists unclassified build tickets to read next."""
    res = _resolver(workspace, resolve_path)
    intents = load_intent_rules(workspace, rules_path, res)
    header, rows = _read_csv(res(tickets_path))
    label_rows(rows, intents)
    extra = ["intent"]
    held: set[str] | None = None
    hold_file = res(holdout_path)
    if hold_file.is_file():
        held = {r.get(ID_COLUMN) for r in _read_csv(hold_file)[1]}
        for row in rows:
            row["split"] = "holdout" if row.get(ID_COLUMN) in held else "build"
        extra.append("split")
    _write_csv(res(labeled_path, write=True), header + [c for c in extra if c not in header], rows)
    table = compute_taxonomy(rows, intents)
    _write_csv(res(output_path, write=True), TAXONOMY_HEADER, table)
    classified = sum(r["volume"] for r in table if r["intent"] != OTHER_INTENT)
    out = {"tickets": len(rows), "coverage": round(classified / (len(rows) or 1), 4),
           "taxonomy_path": output_path, "labeled_path": labeled_path,
           "top": [(r["intent"], r["volume"]) for r in table[:10]]}
    if held is not None:
        out["unclassified_build_ids"] = [r.get(ID_COLUMN) for r in rows
                                         if r["intent"] == OTHER_INTENT and r["split"] == "build"][:25]
    return out


def parse_article(path: Path) -> dict:
    """Title, declared intents, sources and body of a markdown article.
    Front matter is an optional leading block between '---' lines."""
    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].strip().splitlines():
                key, sep, value = line.partition(":")
                if sep:
                    meta[key.strip().lower()] = value.strip()
            body = text[end + 4:]
    split = lambda v: [s.strip() for s in re.split(r"[,;]", v.strip("[] ")) if s.strip()]  # noqa: E731
    title = meta.get("title") or next((ln.lstrip("# ").strip() for ln in body.splitlines() if ln.startswith("#")), path.stem)
    return {"path": path, "title": title, "intents": split(meta.get("intents", "")),
            "sources": split(meta.get("sources", "")), "body": body}


def article_matches(article: dict, intent: dict) -> bool:
    """An article covers an intent if it declares it, or if at least two of
    the intent's keywords appear in its title+body (one when it has one)."""
    if intent["id"] in article["intents"]:
        return True
    low = f"{article['title']} {article['body']}".lower()
    keywords = _usable(intent.get("keywords"))
    hits = sum(1 for kw in keywords if _keyword_re(kw).search(low))
    return bool(keywords) and hits >= min(2, len(keywords))


def compute_coverage(workspace: Path, intents: list[dict], volumes: Mapping[str, int], kb_dir: str,
                     res: Resolver | None = None) -> list[dict]:
    """Per intent: ticket volume, covered/gap, and the articles covering it."""
    articles = [parse_article(p) for p in _dir_files(_resolver(workspace, res), kb_dir, KB_SUFFIXES)]
    rows = []
    for intent in intents:
        matches = [a["path"].name for a in articles if article_matches(a, intent)]
        rows.append({"intent": intent["id"], "volume": int(volumes.get(intent["id"], 0)),
                     "status": "covered" if matches else "gap", "articles": ";".join(matches)})
    rows.sort(key=lambda r: (-r["volume"], r["intent"]))
    return rows


def _read_taxonomy(workspace: Path, path: str, res: Resolver | None = None) -> list[dict]:
    return _read_csv(_resolver(workspace, res)(path))[1]


def kb_coverage(workspace: Path, *, resolve_path: Resolver | None = None,
                rules_path: str = f"{M1}/intent_rules.json",
                taxonomy_path: str = f"{M1}/intent_taxonomy.csv",
                kb_dir: str = "inputs/help_center",
                output_path: str = f"{M1}/kb_gap_map.csv", **_: Any) -> dict:
    """Map each intent to the help-center articles that cover it; intents with
    none are gaps, ranked by ticket volume."""
    res = _resolver(workspace, resolve_path)
    intents = load_intent_rules(workspace, rules_path, res)
    volumes = {r["intent"]: int(r["volume"]) for r in _read_taxonomy(workspace, taxonomy_path, res)}
    rows = compute_coverage(workspace, intents, volumes, kb_dir, res)
    _write_csv(res(output_path, write=True), ["intent", "volume", "status", "articles"], rows)
    gaps = [r for r in rows if r["status"] == "gap"]
    return {"output_path": output_path, "intents": len(rows), "gaps": [(g["intent"], g["volume"]) for g in gaps]}


# --- policy numbers -----------------------------------------------------------
# "within 30 days", "a 30-day window", "2 business days", "$50", "10%"
_UNITS = r"(?:business[\s-]+days?|days?|hours?|weeks?|months?|years?)"
_POLICY_NUM_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?|\d+(?:\.\d+)?\s?%|\d+(?:\.\d+)?(?:\s+|-)" + _UNITS + r"\b",
                            re.IGNORECASE)
_UNIT_NAMES = {"business day": "business days", "day": "days", "hour": "hours", "week": "weeks",
               "month": "months", "year": "years"}
# Lines that stand alone instead of continuing a wrapped sentence.
_BLOCK_LINE = re.compile(r"^(#{1,6}\s|[-*+]\s|\d+[.)]\s|\|)")
# Clause breaks inside a sentence: a contrast usually starts a new subject
# ("not refundable, but support may offer a credit of up to $20").
_CLAUSE_BREAK = re.compile(r";|\bbut\b|\bhowever\b|\bexcept\b|\balthough\b", re.IGNORECASE)


def _number(raw: str) -> str:
    n = raw.replace(",", "")
    return n.rstrip("0").rstrip(".") if "." in n else n


def normalize_value(raw: str) -> str:
    """'$ 60.00' -> '$60', '30-day' -> '30 days', '2 business day' -> '2 business days'."""
    v = " ".join(raw.lower().replace("-", " ").split())
    if v.startswith("$"):
        return "$" + _number(v[1:].strip())
    if v.endswith("%"):
        return _number(v[:-1].strip()) + "%"
    num, _, unit = v.partition(" ")
    unit = unit[:-1] if unit.endswith("s") else unit
    return f"{_number(num)} {_UNIT_NAMES.get(unit, unit)}".strip()


def value_unit(value: str) -> str:
    if value.startswith("$"):
        return "$"
    if value.endswith("%"):
        return "%"
    return value.partition(" ")[2]


def _sentences(text: str) -> Iterable[str]:
    """Sentences with hard-wrapped lines joined; headings, list items and
    table rows start a new one."""
    split = lambda s: re.split(r"(?<=[.!?])\s+", s)  # noqa: E731
    for para in re.split(r"\n\s*\n", text or ""):
        buf: list[str] = []
        for line in para.splitlines():
            s = line.strip()
            if not s:
                continue
            if _BLOCK_LINE.match(s) or s.startswith("#"):
                if buf:
                    yield from split(" ".join(buf))
                    buf = []
                if s.startswith("#"):
                    yield s
                    continue
            buf.append(s)
        if buf:
            yield from split(" ".join(buf))


def clauses(text: str) -> Iterable[str]:
    for sentence in _sentences(text):
        yield from _CLAUSE_BREAK.split(sentence)


def policy_values(text: str) -> set[str]:
    """Every normalized policy number (days, hours, amounts, ...) in text."""
    return {normalize_value(v) for v in _POLICY_NUM_RE.findall(text or "")}


def policy_numbers(text: str, terms: list[str]) -> dict[str, set[str]]:
    """For each term, the normalized numeric values stated in clauses that
    mention it (e.g. 'refund' -> {'30 days'}). Terms match as substrings, so
    'refund' also covers 'refunds' and 'refundable'."""
    out: dict[str, set[str]] = {t: set() for t in terms}
    for clause in clauses(text):
        values = policy_values(clause)
        if not values:
            continue
        low = clause.lower()
        for term in terms:
            if term.lower() in low:
                out[term] |= values
    return out


def find_contradictions(workspace: Path, *, resolve_path: Resolver | None = None, terms: list[str],
                        paths: list[str] | None = None, kb_dir: str = "inputs/help_center",
                        tickets_path: str | None = None, reply_column: str = "agent_reply", **_: Any) -> dict:
    """Flag policy terms (refund, cancellation, shipping, ...) whose numeric
    values differ between sources: help-center articles, macro/policy files
    and, optionally, agent replies in the ticket export. A term with more than
    one distinct value of the same unit is a candidate contradiction for a
    human to resolve - the tool does not decide which value is right."""
    res = _resolver(workspace, resolve_path)
    sources: dict[str, str] = {}
    for p in _dir_files(res, kb_dir, KB_SUFFIXES):
        sources[f"{kb_dir.rstrip('/')}/{p.name}"] = p.read_text(encoding="utf-8")
    for rel in paths or []:
        sources[rel] = res(rel).read_text(encoding="utf-8")
    if tickets_path:
        _, rows = _read_csv(res(tickets_path))
        for row in rows:
            if row.get(reply_column):
                sources[f"{tickets_path}#{row.get(ID_COLUMN, '?')}"] = row[reply_column]
    seen: dict[str, dict[str, list[str]]] = {t: {} for t in terms}
    for name, text in sources.items():
        for term, values in policy_numbers(text, terms).items():
            for v in values:
                seen[term].setdefault(v, []).append(name)
    conflicts = []
    for term, values in seen.items():
        by_unit: dict[str, list[str]] = {}
        for v in values:
            by_unit.setdefault(value_unit(v), []).append(v)
        for unit, vals in by_unit.items():
            if len(vals) > 1:
                conflicts.append({"term": term, "unit": unit,
                                  "values": {v: sorted(values[v]) for v in sorted(vals)}})
    return {"sources_scanned": len(sources), "conflicts": conflicts}


# --- held-out split and replay -------------------------------------------------

def _split_bucket(ticket_id: str, seed: str) -> float:
    digest = hashlib.sha256(f"{seed}:{ticket_id}".encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def _truthy(value: Any) -> bool:
    return LABEL_VALUES.get(str(value or "").strip().lower(), False)


def holdout_ids(rows: list[dict], fraction: float, seed: str) -> set[str]:
    """The held-out ticket ids: within each must_escalate class, the
    round(fraction * n) tickets with the lowest seeded hash (at least one when
    a class has two or more), so the held-out set always carries positives.
    Same seed, ids and labels -> same split on any machine."""
    fraction = float(fraction)
    if not 0 < fraction < 1:
        raise ValueError("holdout_fraction must be between 0 and 1")
    groups: dict[bool, list[str]] = {True: [], False: []}
    for row in rows:
        groups[_truthy(row.get(LABEL_COLUMN))].append(str(row.get(ID_COLUMN)))
    out: set[str] = set()
    for ids in groups.values():
        k = int(len(ids) * fraction + 0.5)
        if k == 0 and len(ids) >= 2:
            k = 1
        out.update(sorted(ids, key=lambda t: (_split_bucket(t, seed), t))[:k])
    return out


def split_eval_set(workspace: Path, *, resolve_path: Resolver | None = None,
                   tickets_path: str = f"{M1}/tickets_redacted.csv",
                   holdout_path: str = f"{M1}/eval_holdout.csv",
                   build_path: str = f"{M1}/build_split.csv",
                   holdout_fraction: float = 0.3, seed: str = "support-automation", **_: Any) -> dict:
    """Seal a held-out eval set before any ticket is read: a deterministic,
    hash-based split, stratified by must_escalate, into build and held-out
    tickets (same seed, ids and labels -> same split, any machine)."""
    res = _resolver(workspace, resolve_path)
    header, rows = _read_csv(res(tickets_path))
    require_ids(header, tickets_path)
    held = holdout_ids(rows, holdout_fraction, seed)
    hold = [r for r in rows if r[ID_COLUMN] in held]
    build = [r for r in rows if r[ID_COLUMN] not in held]
    _write_csv(res(holdout_path, write=True), header, hold)
    _write_csv(res(build_path, write=True), header, build)
    return {"holdout_path": holdout_path, "build_path": build_path, "holdout": len(hold),
            "build": len(build), "must_escalate_in_holdout": sum(1 for r in hold if _truthy(r.get(LABEL_COLUMN))),
            "must_escalate_in_build": sum(1 for r in build if _truthy(r.get(LABEL_COLUMN)))}


def load_agent_config(workspace: Path, config_path: str, res: Resolver | None = None) -> dict:
    data = json.loads(_resolver(workspace, res)(config_path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("agent config must be a JSON object")
    return data


def escalation_rule_problems(rules: Any) -> list[str]:
    """Why escalation rules are unusable: not a list of objects, a missing
    category, or keywords that are not a list of real phrases."""
    if not isinstance(rules, list) or not rules:
        return ["escalation_rules must be a non-empty list"]
    problems = []
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            problems.append(f"escalation_rules[{i}] is not an object")
            continue
        cat = rule.get("category")
        if not isinstance(cat, str) or not cat.strip():
            problems.append(f"escalation_rules[{i}] needs a category")
        problems += keyword_problems(rule.get("keywords"), f"escalation rule {cat or i}")
    return problems


def escalation_decision(text: str, rules: list[dict]) -> str | None:
    """Category of the first escalation rule whose keyword appears, else None.
    Malformed keywords (not a list, shorter than MIN_KEYWORD_CHARS) are
    ignored, never treated as matching everything."""
    low = (text or "").lower()
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict):
            continue
        if any(_keyword_re(kw).search(low) for kw in _usable(rule.get("keywords"))):
            return rule.get("category") or "escalate"
    return None


def recall_lower_bound(hits: int, total: int, z: float = 1.96) -> float:
    """Wilson score lower bound of hits/total (95% by default); 0 for none."""
    if total <= 0:
        return 0.0
    p = hits / total
    centre = p + z * z / (2 * total)
    margin = z * ((p * (1 - p) + z * z / (4 * total)) / total) ** 0.5
    return round((centre - margin) / (1 + z * z / total), 4)


def compute_replay(rows: list[dict], rules: list[dict]) -> dict:
    tp = fp = fn = tn = 0
    misses = []
    for row in rows:
        gold = _truthy(row.get(LABEL_COLUMN))
        pred = escalation_decision(f"{row.get('subject', '')} {row.get('body', '')}", rules) is not None
        if gold and pred:
            tp += 1
        elif gold:
            fn += 1
            misses.append(row.get(ID_COLUMN))
        elif pred:
            fp += 1
        else:
            tn += 1
    recall = tp / (tp + fn) if tp + fn else 1.0
    precision = tp / (tp + fp) if tp + fp else 1.0
    return {"tickets": len(rows), "true_pos": tp, "false_pos": fp, "false_neg": fn, "true_neg": tn,
            "must_escalate_recall": round(recall, 4), "escalation_precision": round(precision, 4),
            "recall_lower_bound_95": recall_lower_bound(tp, tp + fn), "missed_ticket_ids": misses}


def replay_escalations(workspace: Path, *, resolve_path: Resolver | None = None,
                       config_path: str = "deliverables/m3-agent-config/agent_config.json",
                       eval_path: str = f"{M1}/eval_holdout.csv",
                       output_path: str = "deliverables/m3-agent-config/escalation_replay.json",
                       **_: Any) -> dict:
    """Replay the config's escalation rules over held-out tickets and score
    them against the gold must_escalate labels (recall matters most)."""
    res = _resolver(workspace, resolve_path)
    config = load_agent_config(workspace, config_path, res)
    _, rows = _read_csv(res(eval_path))
    result = compute_replay(rows, config.get("escalation_rules") or [])
    out = res(output_path, write=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    problems = escalation_rule_problems(config.get("escalation_rules"))
    return {"output_path": output_path, **result, **({"rule_problems": problems} if problems else {})}


_TOOL_COMMON = {"type": "object", "additionalProperties": False}


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {**_TOOL_COMMON, "properties": props, "required": required or []}


_S = {"type": "string"}
_SL = {"type": "array", "items": {"type": "string"}}

TOOL_DEFS: list[dict] = [
    {"name": "redact_tickets", "risk": "write", "function": redact_tickets,
     "description": "Redact emails, phone numbers and Luhn-valid card numbers from every column of a ticket "
                    "CSV except ticket_id (or only `columns`) and write the redacted copy. Returns row count "
                    "and redactions by kind.",
     "input_schema": _schema({"input_path": _S, "output_path": _S, "columns": _SL})},
    {"name": "scan_pii", "risk": "read", "function": scan_pii,
     "description": "Count emails, phone numbers and card numbers remaining in a CSV or text file.",
     "input_schema": _schema({"path": _S, "columns": _SL}, ["path"])},
    {"name": "split_eval_set", "risk": "write", "function": split_eval_set,
     "description": "Seal the held-out eval set: split the redacted tickets deterministically (stratified by "
                    "must_escalate) into build_split.csv and eval_holdout.csv. Run it right after redaction, "
                    "before reading any ticket; read and cite build tickets only from then on.",
     "input_schema": _schema({"tickets_path": _S, "holdout_path": _S, "build_path": _S,
                              "holdout_fraction": {"type": "number"}, "seed": _S})},
    {"name": "build_intent_taxonomy", "risk": "write", "function": build_intent_taxonomy,
     "description": "Label each ticket using the keyword rules in intent_rules.json and write the intent "
                    "taxonomy table (volume, share, handle time, escalation rate, automation level). The "
                    "labeled file marks each ticket's split; unclassified build tickets are listed.",
     "input_schema": _schema({"tickets_path": _S, "rules_path": _S, "labeled_path": _S, "output_path": _S,
                              "holdout_path": _S})},
    {"name": "kb_coverage", "risk": "write", "function": kb_coverage,
     "description": "Match every intent to the knowledge-base articles that cover it and write the gap map, "
                    "ranked by ticket volume.",
     "input_schema": _schema({"rules_path": _S, "taxonomy_path": _S, "kb_dir": _S, "output_path": _S})},
    {"name": "find_contradictions", "risk": "read", "function": find_contradictions,
     "description": "List policy terms whose stated numbers (days, hours, amounts, percentages) differ across "
                    "articles, policy/macro files and optionally agent replies. Flags candidates only.",
     "input_schema": _schema({"terms": _SL, "paths": _SL, "kb_dir": _S, "tickets_path": _S,
                              "reply_column": _S}, ["terms"])},
    {"name": "replay_escalations", "risk": "write", "function": replay_escalations,
     "description": "Replay the agent config's escalation rules over the held-out tickets and report "
                    "must-escalate recall (with its 95% lower bound), precision and missed ticket ids.",
     "input_schema": _schema({"config_path": _S, "eval_path": _S, "output_path": _S})},
]
