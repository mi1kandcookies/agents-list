"""Read an uploaded statement of work into a draft scope for the job flow.

    scope = parse_upload(data, "sow.pdf")     # bytes of a .pdf/.docx/.txt/.md
    scope = parse_text("Objective: ...")      # pasted text

The result prefills the guided flow (/new), the /jobs/new form and the MCP
``submit_sow`` tool. It is a draft the buyer reviews, never a contract: the
statement of work that gets hashed is still built by app/engagements/sow.py
from what the buyer confirms.

Two strategies:

* When ``LLM_URL`` is configured, the model is asked for strict JSON in the
  scope schema (short timeout). Its answer is validated; amounts that do not
  appear in the document are dropped, and anything it leaves empty is filled
  from the rule-based parse.
* Otherwise, and whenever the model fails, a deterministic parser reads
  section headings (Objective, Scope, Deliverables, Milestones, Acceptance
  criteria, Timeline, Budget), bullet lists, tables, dates, USD/USDC amounts
  and "N weeks" durations.

Nothing is invented: a field the document does not state comes back null with
a warning. The file itself is never written to disk; only its name and SHA-256
leave this module (plus the first MAX_TEXT characters of extracted text).
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from app.intake.estimate import CATEGORIES

MAX_BYTES = 5 * 1024 * 1024
MAX_TEXT = 20_000          # extracted text returned with the scope
LLM_TEXT = 12_000          # extracted text sent to the model
LLM_TIMEOUT = 20
MAX_MILESTONES = 8         # the flow's limit (flow.js MAX_MILESTONES)
MAX_OUTCOME = 2000         # the flow's outcome textarea
MAX_CRITERION = 200
EXTENSIONS = (".pdf", ".docx", ".txt", ".md")
DEADLINE_MODES = ("asap", "week", "month", "flexible", "date")


class SowParseError(ValueError):
    def __init__(self, message: str, code: str = "INVALID_REQUEST", status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


# ── text extraction ───────────────────────────────────────────────────────
def safe_filename(name: str | None) -> str:
    """Basename without control characters, at most 200 characters."""
    base = re.split(r"[\\/]", str(name or ""))[-1]
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip()
    return base[:200]


def extension(filename: str) -> str:
    m = re.search(r"\.[A-Za-z0-9]+$", filename or "")
    return m.group(0).lower() if m else ""


def check_upload(filename: str, size: int) -> None:
    if extension(filename) not in EXTENSIONS:
        raise SowParseError("Upload a .pdf, .docx, .txt or .md file.", "UNSUPPORTED_FILE_TYPE", 415)
    if size > MAX_BYTES:
        raise SowParseError("That file is larger than 5 MB.", "FILE_TOO_LARGE", 413)
    if size == 0:
        raise SowParseError("That file is empty.", "EMPTY_DOCUMENT", 422)


def extract_text(data: bytes, filename: str) -> tuple[str, int | None]:
    """(text, page count or None). Headings and list items keep a Markdown-ish
    marker ("# ", "- ") so the rule-based parser can see the structure."""
    ext = extension(filename)
    if ext == ".pdf":
        return _pdf_text(data)
    if ext == ".docx":
        return _docx_text(data), None
    for enc in ("utf-8-sig", "utf-16"):
        try:
            text = data.decode(enc)
            if "\x00" not in text:
                return text, None
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), None


def _pdf_text(data: bytes) -> tuple[str, int]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise SowParseError("That PDF is password-protected. Remove the password or paste the text.",
                                "UNREADABLE_DOCUMENT", 422)
        pages = [(p.extract_text() or "") for p in reader.pages]
    except SowParseError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
        raise SowParseError("That PDF could not be read.", "UNREADABLE_DOCUMENT", 422) from exc
    return "\n\n".join(pages), len(pages)


def _docx_text(data: bytes) -> str:
    import zipfile

    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    try:
        doc = Document(io.BytesIO(data))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise SowParseError("That .docx file could not be read.", "UNREADABLE_DOCUMENT", 422) from exc
    out = []
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(child, doc)
            text = para.text.strip()
            if not text:
                continue
            style = (para.style.name if para.style is not None else "") or ""
            numbered = child.find(".//{*}numPr") is not None
            if style.startswith("Heading") or style == "Title":
                out.append("# " + text)
            elif numbered or style.startswith("List"):
                level = child.find(".//{*}numPr/{*}ilvl")
                depth = int(level.get(level.keys()[0])) if level is not None and level.keys() else 0
                m = re.search(r"(\d)$", style)
                if m and not numbered:
                    depth = int(m.group(1)) - 1
                out.append("  " * max(0, depth) + "- " + text)
            else:
                out.append(text)
        elif tag == "tbl":
            out.append("")
            for row in Table(child, doc).rows:
                cells = [" ".join(c.text.split()) for c in row.cells]
                out.append("| " + " | ".join(cells) + " |")
            out.append("")
    return "\n".join(out)


# ── rule-based parser ─────────────────────────────────────────────────────
SECTIONS = [
    ("acceptance", ("acceptance", "success criteria", "definition of done", "done when",
                    "quality criteria", "completion criteria")),
    ("milestones", ("milestone", "milestones", "phases", "payment schedule", "work plan", "workplan",
                    "stages", "project plan")),
    ("deliverables", ("deliverable", "deliverables", "outputs")),
    ("timeline", ("timeline", "schedule", "deadline", "timing", "duration", "delivery date",
                  "due date", "completion date", "term")),
    ("budget", ("budget", "fee", "fees", "price", "pricing", "cost", "costs", "compensation",
                "payment", "payment terms", "rate", "total")),
    ("objective", ("objective", "objectives", "goal", "goals", "purpose", "outcome", "outcomes",
                   "overview", "summary", "background", "introduction", "project description",
                   "about the project")),
    ("scope", ("scope", "scope of work", "description", "requirements", "work to be performed",
               "services", "tasks")),
]

_BULLET = re.compile(r"^(?:[-*•▪◦·‣–]|\d{1,2}[.)]|[a-z][.)]|\[[ xX]?\])\s+")
_MS_MARK = re.compile(
    r"^(?:#+\s*)?(?:\*\*)?(?:milestone|phase|stage|sprint|deliverable|m)\s*"
    r"(\d{1,2}|[ivx]{1,4}|[a-h])\b\s*(?:\*\*)?\s*[:.)\-–—]*\s*(.*)$", re.I)
_AMOUNT = re.compile(
    r"(?:US\s?\$|\$)\s?(?P<a>\d[\d,]*(?:\.\d+)?)(?:\s?(?P<ak>[kK])\b)?(?:\s?(?:USDC|USD)\b)?"
    r"|\b(?:USDC|USD)\s?(?P<b>\d[\d,]*(?:\.\d+)?)(?:\s?(?P<bk>[kK])\b)?"
    r"|\b(?P<c>\d[\d,]*(?:\.\d+)?)(?:\s?(?P<ck>[kK]))?\s?(?:USDC|USD|dollars)\b", re.I)
_PERCENT = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s?%")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE_ISO = re.compile(r"\b(20\d\d)-(\d{1,2})-(\d{1,2})\b")
_DATE_DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MON + r",?\s+(20\d\d)\b", re.I)
_DATE_MDY = re.compile(r"\b" + _MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d\d)\b", re.I)
_DATE_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d\d)\b")
_DURATION = re.compile(r"\b(\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|twelve)"
                       r"\s*(?:-\s*)?(day|week|month)s?\b", re.I)
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "twelve": 12}
_DEADLINE_WORDS = re.compile(r"\b(deadline|due|deliver(?:ed|y)?\s+by|complete[d]?\s+by|completion|"
                             r"no later than|end date|finish(?:ed)?\s+by|by the end of|within)\b", re.I)
_FIELD_LINE = re.compile(r"^(amount|payment|fee|price|cost|budget|value)\s*[:\-–—]\s*", re.I)
_CRITERIA_LINE = re.compile(r"^(acceptance(?: criteria)?|criteria|done when|definition of done|"
                            r"success criteria)\s*[:\-–—]\s*", re.I)
_DATE_LINE = re.compile(r"^(due|date|deadline|timeline|duration|timing|week|weeks)\b\s*[:\-–—]?", re.I)


def _classify(heading: str) -> str | None:
    h = heading.lower()
    for kind, words in SECTIONS:
        for w in words:
            if re.search(r"\b" + re.escape(w) + r"\b", h):
                return kind
    return None


def _clean(text: str) -> str:
    text = re.sub(r"\*\*|__|`", "", text)
    return " ".join(text.split()).strip()


def _heading(line: str) -> tuple[str, str, bool] | None:
    """(heading, inline rest, strong) when ``line`` starts a section, else
    None. "Strong" headings (Markdown, bold, capitals, a bare keyword line)
    always start a section; "Key: value" lines inside a milestone do not."""
    s = line.strip()
    if not s or s.startswith("|"):
        return None
    m = re.match(r"^#{1,6}\s+(.*)$", s)
    if m:
        return _clean(m.group(1)).rstrip(":"), "", True
    m = re.match(r"^\*\*(.+?)\*\*:?\s*$", s)
    if m:
        return _clean(m.group(1)).rstrip(":"), "", True
    m = re.match(r"^(?:\d{1,2}(?:\.\d{1,2})*[.)]?\s+)?([A-Za-z][A-Za-z /&'()-]{1,48}?)\s*:\s*(.*)$", s)
    if m and len(m.group(1).split()) <= 5 and not _BULLET.match(s):
        head, rest = m.group(1), m.group(2)
        if not rest or _classify(head):
            return _clean(head), rest.strip(), head.isupper()
    m = re.match(r"^(?:\d{1,2}(?:\.\d{1,2})*[.)]?\s+)?([A-Za-z][A-Za-z /&'()-]{1,48})$", s)
    if m and len(m.group(1).split()) <= 5:
        head = m.group(1).strip()
        numbered = head != s
        if _classify(head) and (numbered or head.isupper() or len(head.split()) <= 4):
            return _clean(head), "", True
    if s.isupper() and len(s) <= 60 and re.search(r"[A-Z]{3}", s) and not _BULLET.match(s):
        return _clean(s), "", True
    return None


def _sections(text: str) -> tuple[str | None, list[dict]]:
    """Split into [{kind, heading, lines:[(indent, text)]}]; returns (title, sections)."""
    title = None
    sections = [{"kind": "preamble", "heading": "", "lines": []}]
    in_milestone = False
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.expandtabs(4).rstrip()
        if not line.strip():
            sections[-1]["lines"].append((0, ""))
            continue
        bare = _BULLET.sub("", line.strip()).lstrip("#").strip()
        if _MS_MARK.match(bare):
            in_milestone = True
        head = None if _MS_MARK.match(bare) else _heading(line)
        if head and (head[2] or not in_milestone):
            kind = _classify(head[0])
            if kind is None and title is None and len(sections) == 1 \
                    and not any(t for _, t in sections[0]["lines"]):
                title = re.sub(r"^statement of work\s*[:\-–—]?\s*", "", head[0], flags=re.I) or None
                continue
            if kind is not None:
                in_milestone = False
                sections.append({"kind": kind, "heading": head[0], "lines": []})
                if head[1]:
                    sections[-1]["lines"].append((0, head[1]))
                continue
        indent = len(line) - len(line.lstrip(" "))
        sections[-1]["lines"].append((indent, line.strip()))
    if title is None:
        pre = sections[0]["lines"]
        for i, (_, t) in enumerate(pre):
            if not t:
                continue
            if len(t.split()) <= 10 and not re.search(r"[.!?]$", t) and not _BULLET.match(t):
                title = re.sub(r"^(?:project|title|job|statement of work)\s*[:\-–—]\s*", "", _clean(t),
                               flags=re.I)
                pre[i] = (0, "")
            break
    return title, sections


def _of(sections, kind) -> list[dict]:
    return [s for s in sections if s["kind"] == kind]


def _paragraphs(lines) -> list[str]:
    out, cur = [], []
    for _, t in lines:
        if t and _MS_MARK.match(_BULLET.sub("", t).lstrip("#").strip()):
            break
        if not t:
            if cur:
                out.append(" ".join(cur))
                cur = []
            continue
        if _BULLET.match(t) or t.startswith("|"):
            if cur:
                out.append(" ".join(cur))
                cur = []
            continue
        cur.append(_clean(t))
    if cur:
        out.append(" ".join(cur))
    return [p for p in out if p]


def _items(lines) -> list[str]:
    """Bullet items (and plain lines when there are no bullets)."""
    bullets = [_clean(_BULLET.sub("", t)) for _, t in lines if t and _BULLET.match(t)]
    if bullets:
        return [b for b in bullets if b]
    return [_clean(t) for _, t in lines if t and not t.startswith("|")]


def _decimal(num: str, k: str | None) -> Decimal | None:
    try:
        d = Decimal(num.replace(",", ""))
    except InvalidOperation:
        return None
    if k:
        d *= 1000
    return d if d > 0 else None


def amounts(text: str) -> list[Decimal]:
    out = []
    for m in _AMOUNT.finditer(text or ""):
        num = m.group("a") or m.group("b") or m.group("c")
        k = m.group("ak") or m.group("bk") or m.group("ck")
        d = _decimal(num, k)
        if d is not None:
            out.append(d)
    return out


def _strip_amounts(text: str) -> str:
    text = re.sub(r"\(\s*(?:" + _AMOUNT.pattern + r")[^)]*\)", "", text, flags=re.I)
    text = _AMOUNT.sub("", text)
    text = re.sub(r"\(\s*\d{1,3}(?:\.\d+)?\s?%[^)]*\)", "", text)
    text = re.sub(r"\(\s*\)", "", text)
    return text.strip(" \t-–—:|,;").strip()


def _num(d: Decimal | None):
    if d is None:
        return None
    d = d.quantize(Decimal("0.01"))
    return int(d) if d == d.to_integral_value() else float(d)


def _criterion(text: str) -> str:
    t = _clean(_BULLET.sub("", text.strip()))
    t = _CRITERIA_LINE.sub("", t)
    if len(t) > MAX_CRITERION:
        t = t[:MAX_CRITERION - 1].rstrip() + "…"
    return t


def _split_criteria(text: str) -> list[str]:
    parts = re.split(r"\s*;\s*|\s*<br\s*/?>\s*", text)
    return [c for c in (_criterion(p) for p in parts) if c]


def _table_rows(lines) -> list[list[str]]:
    rows = []
    for _, t in lines:
        if t.startswith("|") and t.count("|") >= 2:
            cells = [_clean(c) for c in t.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) or not c for c in cells):
                continue
            rows.append(cells)
    return rows


_COLS = {
    "title": re.compile(r"milestone|deliverable|title|phase|name|stage|item", re.I),
    "acceptance": re.compile(r"accept|criteria|done|definition|success", re.I),
    "amount": re.compile(r"amount|payment|fee|price|cost|usd|\$|budget|value", re.I),
}


def _milestones_from_table(rows) -> list[dict]:
    if not rows:
        return []
    cols = {}
    header = rows[0]
    if not amounts(" ".join(header)):
        for i, cell in enumerate(header):
            for key, rx in _COLS.items():
                if key not in cols and rx.search(cell):
                    cols[key] = i
                    break
    body = rows[1:] if cols else rows
    out = []
    for cells in body:
        def cell(key):
            i = cols.get(key)
            return cells[i] if i is not None and i < len(cells) else ""
        title = cell("title")
        if not title:
            title = next((c for c in cells if c and not re.fullmatch(r"(?:m|#)?\s*\d{1,2}\.?", c, re.I)
                          and not amounts(c)), "")
        money = amounts(cell("amount")) or amounts(" ".join(c for c in cells if c != title))
        acc = cell("acceptance")
        if not acc and "acceptance" not in cols:
            rest = [c for c in cells if c and c != title and not amounts(c)
                    and not re.fullmatch(r"(?:m|#)?\s*\d{1,2}\.?", c, re.I)]
            acc = max(rest, key=len) if rest else ""
        title = _strip_amounts(title)
        if not title or (_classify(title) == "budget" and not acc):
            continue   # e.g. a "Total | $5,000" row
        out.append({"title": title[:120], "acceptance": _split_criteria(acc) if acc else [],
                    "amount": money[0] if money else None, "pct": None})
    return out


def _milestones_from_lines(lines, *, explicit_only: bool) -> list[dict]:
    """Milestones marked "Milestone 1: …" / "Phase 2 - …" anywhere, or (when
    not ``explicit_only``) the top-level list items of a Milestones section."""
    out: list[dict] = []
    cur = None
    base = None
    tops = [ind for ind, t in lines if t and _BULLET.match(t)]
    top_indent = min(tops) if tops else 0
    top = [t for ind, t in lines if t and _BULLET.match(t) and ind <= top_indent]
    # Text from PDFs loses indentation, so nested criteria can sit at the same
    # level as the milestones. Tell them apart by list style ("1." items with
    # "-" criteria), or by amounts (only the milestone lines carry one).
    numbered = [bool(re.match(r"\d", t)) for t in top]
    priced = [bool(amounts(t)) for t in top]
    if top and any(numbered) and not all(numbered):
        is_milestone = (lambda t: bool(re.match(r"\d", t)) == numbered[0])
    elif top and priced[0] and not all(priced):
        is_milestone = (lambda t: bool(amounts(t)))
    else:
        is_milestone = (lambda t: True)
    for indent, t in lines:
        if not t:
            continue
        stripped = _BULLET.sub("", t).strip() if _BULLET.match(t) else t
        mark = _MS_MARK.match(stripped.lstrip("#").strip())
        is_top_item = (not explicit_only and _BULLET.match(t) and indent <= top_indent
                       and (base is None or base == "items") and is_milestone(t))
        if mark or is_top_item:
            if mark:
                base = base or "marks"
                title_text = mark.group(2)
            else:
                base = "items"
                title_text = stripped
            money = amounts(title_text)
            pct = _PERCENT.search(title_text)
            parts = re.split(r"\s+[-–—:]\s+|:\s+", _strip_amounts(title_text), maxsplit=1)
            title = _clean(parts[0]).rstrip(".")
            cur = {"title": title[:120] or f"Milestone {len(out) + 1}", "acceptance": [],
                   "amount": money[0] if money else None,
                   "pct": Decimal(pct.group(1)) if pct and not money else None}
            if len(parts) > 1 and parts[1].strip():
                cur["acceptance"].extend(_split_criteria(parts[1]))
            out.append(cur)
            continue
        if cur is None:
            continue
        body = _clean(_BULLET.sub("", t))
        if _FIELD_LINE.match(body):
            money = amounts(body)
            pct = _PERCENT.search(body)
            if money and cur["amount"] is None:
                cur["amount"] = money[0]
            elif pct and cur["amount"] is None:
                cur["pct"] = Decimal(pct.group(1))
            continue
        if _DATE_LINE.match(body):
            continue
        if _CRITERIA_LINE.match(body):
            cur["acceptance"].extend(_split_criteria(_CRITERIA_LINE.sub("", body)))
            continue
        if base == "items" and indent <= top_indent and not _BULLET.match(t):
            continue   # prose between list items
        c = _criterion(body)
        if c and not re.fullmatch(r"(acceptance(?: criteria)?|criteria)\s*:?", c, re.I):
            cur["acceptance"].append(c)
    return out


def _find_date(text: str, today: date) -> tuple[date | None, bool]:
    """(date, ambiguous) for the first date in ``text``."""
    found = []
    for m in _DATE_ISO.finditer(text):
        found.append((m.start(), _mk(int(m.group(1)), int(m.group(2)), int(m.group(3))), False))
    for m in _DATE_DMY.finditer(text):
        found.append((m.start(), _mk(int(m.group(3)), _MONTHS[m.group(2)[:3].lower()], int(m.group(1))), False))
    for m in _DATE_MDY.finditer(text):
        found.append((m.start(), _mk(int(m.group(3)), _MONTHS[m.group(1)[:3].lower()], int(m.group(2))), False))
    for m in _DATE_SLASH.finditer(text):
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if a > 12:
            found.append((m.start(), _mk(y, b, a), False))
        else:
            found.append((m.start(), _mk(y, a, b), b <= 12 and a != b))
    found = [f for f in found if f[1] is not None]
    if not found:
        return None, False
    found.sort(key=lambda f: f[0])
    return found[0][1], found[0][2]


def _mk(y, m, d) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _duration_days(text: str) -> int | None:
    m = _DURATION.search(text)
    if not m:
        return None
    word = m.group(1).lower()
    n = int(word) if word.isdigit() else _WORDS[word]
    unit = m.group(2).lower()
    return n * {"day": 1, "week": 7, "month": 30}[unit]


def _deadline(sections, today: date, warnings: list[str]) -> dict:
    none = {"date": None, "mode": None, "text": None}
    keyword_lines = [t for s in sections if s["kind"] not in ("milestones",)
                     for _, t in s["lines"] if t and _DEADLINE_WORDS.search(t)]
    timeline_lines = [t for s in _of(sections, "timeline") for _, t in s["lines"] if t]
    ms_lines = [t for s in _of(sections, "milestones") for _, t in s["lines"] if t]
    for group in (keyword_lines, timeline_lines):
        for line in group:
            low = line.lower()
            if re.search(r"\b(asap|as soon as possible|urgent(?:ly)?|immediately)\b", low):
                return {"date": None, "mode": "asap", "text": _clean(line)}
            if re.search(r"\b(flexible|no (?:fixed |hard )?deadline)\b", low):
                return {"date": None, "mode": "flexible", "text": _clean(line)}
            d, ambiguous = _find_date(line, today)
            if d:
                return _dated(d, today, _clean(line), ambiguous, warnings)
    for group in (keyword_lines, timeline_lines):
        for line in group:
            days = _duration_days(line)
            if days:
                warnings.append(f"The deadline is a duration (“{_clean(line)[:80]}”); "
                                "it is counted from today.")
                mode = "week" if days == 7 else "month" if days == 30 else "date"
                return {"date": (today + timedelta(days=days)).isoformat(), "mode": mode,
                        "text": _clean(line)}
    dates = [d for d in (_find_date(t, today)[0] for t in timeline_lines + ms_lines) if d]
    if dates:
        return _dated(max(dates), today, "latest date in the timeline", False, warnings)
    warnings.append("No deadline found in the document.")
    return none


def _dated(d: date, today: date, text: str, ambiguous: bool, warnings) -> dict:
    if d < today:
        warnings.append(f"The deadline in the document ({d.isoformat()}) has already passed.")
        return {"date": None, "mode": None, "text": text}
    if ambiguous:
        warnings.append(f"Read {text[:60]!r} as month/day ({d.isoformat()}). Check the deadline.")
    return {"date": d.isoformat(), "mode": "date", "text": text}


def _budget(sections, text: str, warnings: list[str]) -> Decimal | None:
    lines = [t for s in _of(sections, "budget") for _, t in s["lines"] if t]
    candidates = [ln for ln in lines if re.search(r"\b(total|budget|not to exceed|fixed|cap|maximum)\b", ln, re.I)]
    candidates += lines
    if not lines:
        candidates = [t for s in sections if s["kind"] not in ("milestones",) for _, t in s["lines"]
                      if t and re.search(r"\b(budget|total (?:fee|price|cost)|not to exceed|fixed fee|"
                                         r"total)\b", t, re.I)]
    for line in candidates:
        found = amounts(line)
        if not found:
            continue
        if len(found) >= 2 and re.search(r"\d\s*(?:-|–|—|to)\s*(?:US\s?\$|\$|USD\s?|USDC\s?)?\d", line):
            warnings.append(f"The budget is a range ({_clean(line)[:80]}); used the upper figure.")
            return max(found[:2])
        return found[0]
    return None


def _category(text: str, weight_text: str) -> str | None:
    words = re.findall(r"[a-z0-9]+", text.lower())
    heavy = re.findall(r"[a-z0-9]+", weight_text.lower())
    best, best_score = None, 0
    for c in CATEGORIES:
        kws = set(c["keywords"])
        score = sum(1 for w in words if w in kws) + 2 * sum(1 for w in heavy if w in kws)
        if score > best_score:
            best, best_score = c["key"], score
    return best


def brief_of(outcome: str | None) -> str | None:
    """Mirror of flow.js briefFromOutcome()."""
    t = " ".join((outcome or "").split())
    if not t:
        return None
    m = re.match(r"^[\s\S]*?[.!?](?=\s|$)", t)
    first = m.group(0) if m else t
    if len(first) > 140:
        first = re.sub(r"\s+\S*$", "", first[:137]) + "…"
    first = first[0].upper() + first[1:]
    if not re.search(r"[.!?…]$", first):
        first += "."
    return first


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind("\n"))
    return (cut[:stop + 1] if stop > limit // 2 else cut).rstrip()


def heuristic_scope(text: str, *, today: date | None = None) -> dict:
    today = today or date.today()
    warnings: list[str] = []
    notes: list[str] = []
    title, sections = _sections(text)

    # Outcome: the Objective section, else Scope, else the opening prose.
    outcome = None
    for kind in ("objective", "scope", "preamble"):
        paras = [p for s in _of(sections, kind) for p in _paragraphs(s["lines"])]
        if kind == "preamble":
            paras = [p for p in paras if len(p.split()) >= 6]
        if not paras and kind != "preamble":
            items = [i for s in _of(sections, kind) for i in _items(s["lines"])]
            paras = ["; ".join(items)] if items else []
        if paras:
            outcome = _truncate("\n\n".join(paras), MAX_OUTCOME)
            break
    if not outcome:
        warnings.append("No objective or outcome found in the document.")

    # Milestones: explicit "Milestone N" markers anywhere, else a table or the
    # list in a Milestones section.
    ms_lines = [ln for s in _of(sections, "milestones") for ln in s["lines"]]
    milestones = [m for s in sections if s["kind"] not in ("acceptance", "timeline", "budget")
                  for m in _milestones_from_lines(s["lines"], explicit_only=True)]
    if not milestones and ms_lines:
        milestones = _milestones_from_table(_table_rows(ms_lines)) or \
            _milestones_from_lines(ms_lines, explicit_only=False)
    if not milestones:
        for s in _of(sections, "deliverables"):
            rows = _milestones_from_table(_table_rows(s["lines"]))
            if rows and any(r["amount"] for r in rows):
                milestones = rows
    job_criteria = [c for s in _of(sections, "acceptance") for i in _items(s["lines"])
                    for c in _split_criteria(i)]
    if not milestones:
        job_criteria += [c for s in _of(sections, "deliverables") for i in _items(s["lines"])
                         for c in _split_criteria(i)]
    if len(milestones) > MAX_MILESTONES:
        warnings.append(f"The document lists {len(milestones)} milestones; kept the first {MAX_MILESTONES}.")
        milestones = milestones[:MAX_MILESTONES]

    budget = _budget(sections, text, warnings)
    for m in milestones:
        if m["amount"] is None and m["pct"] is not None and budget:
            m["amount"] = (budget * m["pct"] / 100).quantize(Decimal("0.01"))
    ms_sum = sum((m["amount"] for m in milestones if m["amount"] is not None), Decimal(0))
    have_all = bool(milestones) and all(m["amount"] is not None for m in milestones)
    if budget is None and have_all:
        budget = ms_sum
        notes.append("Budget is the sum of the milestone amounts.")
    elif budget is not None and have_all and ms_sum != budget:
        warnings.append(f"Milestone amounts add up to {_num(ms_sum)} USDC but the budget says "
                        f"{_num(budget)} USDC.")
    if budget is None:
        warnings.append("No budget found in the document.")
    if milestones and not have_all:
        warnings.append("Some milestones have no amount in the document.")

    if job_criteria:
        if len(milestones) == 1:
            milestones[0]["acceptance"].extend(job_criteria)
        elif milestones:
            missing = [m for m in milestones if not m["acceptance"]]
            if missing:
                missing[-1]["acceptance"].extend(job_criteria)
                notes.append("Job-wide acceptance criteria were added to "
                             f"“{missing[-1]['title']}”.")
    if not milestones:
        warnings.append("No milestones found in the document.")
    elif any(not m["acceptance"] for m in milestones):
        warnings.append("Some milestones have no acceptance criteria in the document.")

    category = _category(text, " ".join([title or "", outcome or ""]))
    if not category:
        warnings.append("Could not tell the kind of work from the document.")

    brief = brief_of(outcome) or (brief_of(title) if title else None)
    return _result(outcome=outcome, brief=brief, title=title, category=category,
                   milestones=[{"title": m["title"], "acceptance": _dedupe(m["acceptance"]),
                                "amount_usdc": _num(m["amount"])} for m in milestones],
                   acceptance=_dedupe(job_criteria),
                   deadline=_deadline(sections, today, warnings),
                   budget=_num(budget), warnings=warnings, notes=notes, method="heuristic")


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for i in items:
        if i.lower() not in seen:
            seen.add(i.lower())
            out.append(i)
    return out


def _result(*, outcome, brief, title, category, milestones, acceptance, deadline, budget,
            warnings, notes, method) -> dict:
    cat = next((c for c in CATEGORIES if c["key"] == category), None)
    return {"method": method, "title": title, "outcome": outcome, "brief": brief,
            "category_key": cat["key"] if cat else None,
            "category_label": cat["label"] if cat else None,
            "agent_category": cat["agent_category"] if cat else None,
            "milestones": milestones, "acceptance": acceptance,
            "deadline": deadline, "budget_usdc": budget,
            "warnings": warnings, "notes": notes}


# ── model-assisted parser ─────────────────────────────────────────────────
_LLM_SYSTEM = (
    "You read a statement of work and return its facts as JSON. Use only what the document "
    "says. If the document does not state something, use null (or an empty list). Never "
    "estimate amounts or dates. Reply with one JSON object and nothing else.")


def _llm_prompt(text: str, today: date) -> str:
    keys = ", ".join(c["key"] for c in CATEGORIES)
    return (
        "Return JSON with exactly these keys:\n"
        '{"outcome": string|null  (the result the buyer wants, in their words, <= 2000 chars),\n'
        ' "brief": string|null  (one sentence, <= 140 chars),\n'
        f' "category_key": one of [{keys}] or null,\n'
        ' "milestones": [{"title": string, "acceptance": [string], "amount_usdc": number|null}],\n'
        ' "deadline": {"date": "YYYY-MM-DD"|null, "mode": "asap"|"week"|"month"|"flexible"|"date"|null},\n'
        ' "budget_usdc": number|null}\n'
        f"Today is {today.isoformat()}. Amounts are in US dollars / USDC.\n\n"
        "Document:\n<<<\n" + text[:LLM_TEXT] + "\n>>>")


def _json_object(reply: str) -> dict:
    s = reply.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in reply")
    obj = json.loads(s[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("reply is not an object")
    return obj


def _positive(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        raise ValueError("amount is not a number") from None
    if not d.is_finite() or d <= 0:
        return None
    return d


def _str(value, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expected a string")
    v = " ".join(value.split()) if "\n" not in value else value.strip()
    return v[:limit] if v else None


def llm_scope(text: str, *, today: date | None = None, fallback: dict | None = None) -> dict:
    """Ask the configured model; raise on anything unusable. ``fallback`` (the
    rule-based scope) fills fields the model left empty."""
    from app import llm
    today = today or date.today()
    obj = _json_object(llm.chat(_LLM_SYSTEM, _llm_prompt(text, today), timeout=LLM_TIMEOUT))
    warnings: list[str] = []
    notes = ["Read with the configured language model; amounts were checked against the document."]
    known = set(amounts(text)) | {_decimal(n, None) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}

    outcome = _str(obj.get("outcome"), MAX_OUTCOME)
    brief = _str(obj.get("brief"), 140)
    category = obj.get("category_key")
    if category not in {c["key"] for c in CATEGORIES}:
        category = None

    raw_ms = obj.get("milestones") or []
    if not isinstance(raw_ms, list):
        raise ValueError("milestones is not a list")
    milestones = []
    for m in raw_ms[:MAX_MILESTONES]:
        if not isinstance(m, dict):
            raise ValueError("milestone is not an object")
        title = _str(m.get("title"), 120)
        if not title:
            continue
        acc = m.get("acceptance") or []
        if isinstance(acc, str):
            acc = [acc]
        if not isinstance(acc, list):
            raise ValueError("acceptance is not a list")
        amount = _positive(m.get("amount_usdc"))
        if amount is not None and amount not in known:
            warnings.append(f"Dropped an amount for “{title}” that is not in the document.")
            amount = None
        milestones.append({"title": title,
                           "acceptance": _dedupe([c for c in (_criterion(str(a)) for a in acc) if c]),
                           "amount_usdc": _num(amount)})

    budget = _positive(obj.get("budget_usdc"))
    ms_sum = sum((Decimal(str(m["amount_usdc"])) for m in milestones if m["amount_usdc"]), Decimal(0))
    if budget is not None and budget not in known and budget != ms_sum:
        warnings.append("Dropped a budget that is not in the document.")
        budget = None

    dl = obj.get("deadline") if isinstance(obj.get("deadline"), dict) else {}
    mode = dl.get("mode") if dl.get("mode") in DEADLINE_MODES else None
    d = None
    if dl.get("date"):
        try:
            d = date.fromisoformat(str(dl["date"])[:10])
        except ValueError:
            d = None
        if d is not None and d < today:
            warnings.append(f"The deadline in the document ({d.isoformat()}) has already passed.")
            d = None
    if mode == "date" and d is None:
        mode = None
    deadline = {"date": d.isoformat() if d else None, "mode": mode if (mode or d) else None,
                "text": None}
    if d and not mode:
        deadline["mode"] = "date"

    fb = fallback or {}
    if not outcome and fb.get("outcome"):
        outcome = fb["outcome"]
    if not brief:
        brief = brief_of(outcome) or fb.get("brief")
    if not category and fb.get("category_key"):
        category = fb["category_key"]
    if not milestones and fb.get("milestones"):
        milestones = fb["milestones"]
    if budget is None and fb.get("budget_usdc") is not None:
        budget = Decimal(str(fb["budget_usdc"]))
    if not deadline["mode"] and (fb.get("deadline") or {}).get("mode"):
        deadline = fb["deadline"]

    if not outcome:
        warnings.append("No objective or outcome found in the document.")
    if not milestones:
        warnings.append("No milestones found in the document.")
    elif any(m["amount_usdc"] is None for m in milestones):
        warnings.append("Some milestones have no amount in the document.")
    if milestones and any(not m["acceptance"] for m in milestones):
        warnings.append("Some milestones have no acceptance criteria in the document.")
    if budget is None:
        warnings.append("No budget found in the document.")
    if not deadline["mode"]:
        warnings.append("No deadline found in the document.")
    if not category:
        warnings.append("Could not tell the kind of work from the document.")
    return _result(outcome=outcome, brief=brief, title=fb.get("title"), category=category,
                   milestones=milestones, acceptance=fb.get("acceptance") or [], deadline=deadline,
                   budget=_num(budget), warnings=warnings, notes=notes, method="llm")


# ── entry points ──────────────────────────────────────────────────────────
def _llm_configured() -> bool:
    from app import llm
    return bool(llm.LLM_URL)


def parse_text(text: str, *, filename: str | None = None, sha256: str | None = None,
               pages: int | None = None, size: int | None = None, today: date | None = None,
               use_llm: bool | None = None) -> dict:
    text = (text or "").replace("\x00", "")
    if not text.strip():
        raise SowParseError("No text found in that document. If it is a scan, paste the text instead.",
                            "EMPTY_DOCUMENT", 422)
    raw = text.encode("utf-8")
    from app.intake import demo_sow
    if demo_sow.is_demo(text):
        scope = demo_sow.scope(today)
        use_llm = False
    else:
        scope = heuristic_scope(text, today=today)
    if use_llm is None:
        use_llm = _llm_configured()
    if use_llm:
        try:
            scope = llm_scope(text, today=today, fallback=scope)
        except Exception:  # noqa: BLE001 - any model failure falls back to the rules
            scope["notes"].insert(0, "The language model could not read this document; "
                                     "it was parsed with rules instead.")
    scope["source"] = {"filename": filename or "pasted-text.txt",
                       "sha256": sha256 or hashlib.sha256(raw).hexdigest(),
                       "pages": pages, "bytes": size if size is not None else len(raw),
                       "chars": len(text), "truncated": len(text) > MAX_TEXT}
    scope["text"] = text[:MAX_TEXT]
    return scope


def parse_upload(data: bytes, filename: str, **kwargs) -> dict:
    name = safe_filename(filename)
    check_upload(name, len(data))
    text, pages = extract_text(data, name)
    return parse_text(text, filename=name, sha256=hashlib.sha256(data).hexdigest(), pages=pages,
                      size=len(data), **kwargs)
