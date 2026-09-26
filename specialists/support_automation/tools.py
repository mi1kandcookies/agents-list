"""
specialists/support_automation/tools.py - deterministic domain tools for the
support-automation specialist.

Each tool is a plain function fn(workspace, *, fetch=None, run=None, **args)
returning a dict; agent.py wraps them for the kit. The model decides what the
intents are and writes the articles; these tools do the counting, redaction,
matching and replay so the numbers in every deliverable are reproducible.

Workspace files the tools understand:

    inputs/tickets.csv          ticket export: ticket_id, subject, body, plus
                                optional agent_reply, handle_minutes, status,
                                must_escalate (gold label, 1/0)
    inputs/help_center/*.md     current help-center articles; optional front
                                matter lines "intents: a, b" and "title: ..."
    intent_rules.json           {"intents": [{"id", "label", "keywords": [...],
                                "automation": answer_only|with_tools|human_only}]}
    agent_config.json           {"escalation_rules": [{"category", "keywords": [...]}], ...}
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

AUTOMATION_LEVELS = ("answer_only", "with_tools", "human_only")
OTHER_INTENT = "other"

# --- PII patterns -----------------------------------------------------------
# Emails and phone numbers by shape; card numbers only when the digit run
# passes the Luhn check, so order ids and dates are left alone.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\w)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\w)")
CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")

REDACTION_TOKENS = {"email": "[EMAIL]", "phone": "[PHONE]", "card": "[CARD]"}


def _resolve(workspace: Path, rel: str) -> Path:
    """Workspace-relative path; refuses anything that escapes the workspace."""
    root = Path(workspace).resolve()
    path = (root / rel).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"path escapes workspace: {rel}")
    return path


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = [dict(r) for r in reader]
        return list(reader.fieldnames or []), rows


def _write_csv(path: Path, header: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


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


def find_pii(text: str) -> dict[str, list[str]]:
    """Every email, phone and Luhn-valid card number in `text`."""
    found: dict[str, list[str]] = {"email": EMAIL_RE.findall(text or ""), "phone": [], "card": []}
    for m in CARD_RE.finditer(text or ""):
        if luhn_ok(re.sub(r"\D", "", m.group())):
            found["card"].append(m.group())
    scrubbed = CARD_RE.sub(lambda m: " " if luhn_ok(re.sub(r"\D", "", m.group())) else m.group(), text or "")
    found["phone"] = PHONE_RE.findall(scrubbed)
    return found


def redact_text(text: str) -> tuple[str, dict[str, int]]:
    """Replace PII with typed tokens; returns (text, counts by kind)."""
    counts = {"email": 0, "phone": 0, "card": 0}

    def _card(m: re.Match) -> str:
        if luhn_ok(re.sub(r"\D", "", m.group())):
            counts["card"] += 1
            return REDACTION_TOKENS["card"]
        return m.group()

    def _sub(kind: str):
        def inner(m: re.Match) -> str:
            counts[kind] += 1
            return REDACTION_TOKENS[kind]
        return inner

    out = CARD_RE.sub(_card, text or "")          # cards first: they look like phones
    out = EMAIL_RE.sub(_sub("email"), out)
    out = PHONE_RE.sub(_sub("phone"), out)
    return out, counts


def _text_columns(header: list[str], columns: list[str] | None) -> list[str]:
    if columns:
        return [c for c in columns if c in header]
    return [c for c in header if c in ("subject", "body", "agent_reply", "requester", "notes")]


# --- tools ------------------------------------------------------------------

def redact_tickets(workspace: Path, *, fetch=None, run=None, input_path: str = "inputs/tickets.csv",
                   output_path: str = "deliverables/m1-discovery/tickets_redacted.csv",
                   columns: list[str] | None = None, **_: Any) -> dict:
    """Redact emails, phones and card numbers from the free-text columns of a
    ticket CSV. Row count and ids are preserved; counts are returned."""
    header, rows = _read_csv(_resolve(workspace, input_path))
    cols = _text_columns(header, columns)
    totals = {"email": 0, "phone": 0, "card": 0}
    for row in rows:
        for col in cols:
            row[col], counts = redact_text(row.get(col) or "")
            for k, v in counts.items():
                totals[k] += v
    _write_csv(_resolve(workspace, output_path), header, rows)
    return {"output_path": output_path, "rows": len(rows), "columns": cols, "redactions": totals}


def scan_pii(workspace: Path, *, fetch=None, run=None, path: str, columns: list[str] | None = None,
             **_: Any) -> dict:
    """Count PII left in a CSV (text columns) or any text file."""
    target = _resolve(workspace, path)
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


def load_intent_rules(workspace: Path, rules_path: str) -> list[dict]:
    """Parse and validate intent_rules.json; raises ValueError on bad shape."""
    data = json.loads(_resolve(workspace, rules_path).read_text(encoding="utf-8"))
    intents = data.get("intents") if isinstance(data, dict) else None
    if not isinstance(intents, list) or not intents:
        raise ValueError("intent rules need a non-empty 'intents' list")
    seen = set()
    for item in intents:
        if not item.get("id") or not isinstance(item.get("keywords"), list) or not item["keywords"]:
            raise ValueError(f"intent needs an id and keywords: {item!r}")
        if item["id"] in seen or item["id"] == OTHER_INTENT:
            raise ValueError(f"duplicate or reserved intent id: {item['id']}")
        if item.get("automation", "answer_only") not in AUTOMATION_LEVELS:
            raise ValueError(f"automation must be one of {AUTOMATION_LEVELS}: {item['id']}")
        seen.add(item["id"])
    return intents


def classify_text(text: str, intents: list[dict]) -> str:
    """First-best keyword match: the intent with the most keyword hits wins;
    ties go to the earlier intent in the rules file; no hit -> 'other'."""
    low = (text or "").lower()
    best, best_hits = OTHER_INTENT, 0
    for item in intents:
        hits = sum(1 for kw in item["keywords"] if kw.lower() in low)
        if hits > best_hits:
            best, best_hits = item["id"], hits
    return best


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


def build_intent_taxonomy(workspace: Path, *, fetch=None, run=None,
                          tickets_path: str = "deliverables/m1-discovery/tickets_redacted.csv",
                          rules_path: str = "deliverables/m1-discovery/intent_rules.json",
                          labeled_path: str = "deliverables/m1-discovery/tickets_labeled.csv",
                          output_path: str = "deliverables/m1-discovery/intent_taxonomy.csv",
                          **_: Any) -> dict:
    """Label every ticket with the keyword rules and write the taxonomy table
    (volume, share, handle time, escalation rate, automation level)."""
    intents = load_intent_rules(workspace, rules_path)
    header, rows = _read_csv(_resolve(workspace, tickets_path))
    for row in rows:
        row["intent"] = classify_text(f"{row.get('subject', '')} {row.get('body', '')}", intents)
    _write_csv(_resolve(workspace, labeled_path), header + (["intent"] if "intent" not in header else []), rows)
    table = compute_taxonomy(rows, intents)
    _write_csv(_resolve(workspace, output_path), TAXONOMY_HEADER, table)
    classified = sum(r["volume"] for r in table if r["intent"] != OTHER_INTENT)
    return {"tickets": len(rows), "coverage": round(classified / (len(rows) or 1), 4),
            "taxonomy_path": output_path, "labeled_path": labeled_path,
            "top": [(r["intent"], r["volume"]) for r in table[:10]]}


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
    hits = sum(1 for kw in intent["keywords"] if kw.lower() in low)
    return hits >= min(2, len(intent["keywords"]))


def compute_coverage(workspace: Path, intents: list[dict], taxonomy: list[dict], kb_dir: str) -> list[dict]:
    kb = _resolve(workspace, kb_dir)
    articles = [parse_article(p) for p in sorted(kb.glob("*.md"))] if kb.is_dir() else []
    volume = {r["intent"]: int(r["volume"]) for r in taxonomy}
    rows = []
    for intent in intents:
        matches = [a["path"].name for a in articles if article_matches(a, intent)]
        rows.append({"intent": intent["id"], "volume": volume.get(intent["id"], 0),
                     "status": "covered" if matches else "gap", "articles": ";".join(matches)})
    rows.sort(key=lambda r: (-r["volume"], r["intent"]))
    return rows


def _read_taxonomy(workspace: Path, path: str) -> list[dict]:
    return _read_csv(_resolve(workspace, path))[1]


def kb_coverage(workspace: Path, *, fetch=None, run=None,
                rules_path: str = "deliverables/m1-discovery/intent_rules.json",
                taxonomy_path: str = "deliverables/m1-discovery/intent_taxonomy.csv",
                kb_dir: str = "inputs/help_center",
                output_path: str = "deliverables/m1-discovery/kb_gap_map.csv", **_: Any) -> dict:
    """Map each intent to the help-center articles that cover it; intents with
    none are gaps, ranked by ticket volume."""
    intents = load_intent_rules(workspace, rules_path)
    rows = compute_coverage(workspace, intents, _read_taxonomy(workspace, taxonomy_path), kb_dir)
    _write_csv(_resolve(workspace, output_path), ["intent", "volume", "status", "articles"], rows)
    gaps = [r for r in rows if r["status"] == "gap"]
    return {"output_path": output_path, "intents": len(rows), "gaps": [(g["intent"], g["volume"]) for g in gaps]}


# "within 30 days", "up to 14 business days", "$50", "48 hours"
_POLICY_NUM_RE = re.compile(r"(\$\s?\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:business\s+days|days|hours|weeks|months|%))",
                            re.IGNORECASE)


def policy_numbers(text: str, terms: list[str]) -> dict[str, set[str]]:
    """For each term, the normalized numeric values stated in sentences that
    mention it (e.g. 'refund' -> {'30 days'})."""
    out: dict[str, set[str]] = {t: set() for t in terms}
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        low = sentence.lower()
        values = {re.sub(r"\s+", " ", v.lower().replace("$ ", "$")) for v in _POLICY_NUM_RE.findall(sentence)}
        if not values:
            continue
        for term in terms:
            if term.lower() in low:
                out[term] |= values
    return out


def find_contradictions(workspace: Path, *, fetch=None, run=None, terms: list[str],
                        paths: list[str] | None = None, kb_dir: str = "inputs/help_center",
                        tickets_path: str | None = None, reply_column: str = "agent_reply", **_: Any) -> dict:
    """Flag policy terms (refund, cancellation, shipping, ...) whose numeric
    values differ between sources: help-center articles, macro/policy files
    and, optionally, agent replies in the ticket export. A term with more than
    one distinct value of the same unit is a candidate contradiction for a
    human to resolve - the tool does not decide which value is right."""
    sources: dict[str, str] = {}
    kb = _resolve(workspace, kb_dir)
    if kb.is_dir():
        for p in sorted(kb.glob("*.md")):
            sources[f"{kb_dir}/{p.name}"] = p.read_text(encoding="utf-8")
    for rel in paths or []:
        sources[rel] = _resolve(workspace, rel).read_text(encoding="utf-8")
    if tickets_path:
        _, rows = _read_csv(_resolve(workspace, tickets_path))
        for row in rows:
            if row.get(reply_column):
                sources[f"{tickets_path}#{row.get('ticket_id', '?')}"] = row[reply_column]
    seen: dict[str, dict[str, list[str]]] = {t: {} for t in terms}
    for name, text in sources.items():
        for term, values in policy_numbers(text, terms).items():
            for v in values:
                seen[term].setdefault(v, []).append(name)
    conflicts = []
    for term, values in seen.items():
        by_unit: dict[str, list[str]] = {}
        for v in values:
            unit = "$" if v.startswith("$") else re.sub(r"[\d.\s]+", "", v, count=1)
            by_unit.setdefault(unit, []).append(v)
        for unit, vals in by_unit.items():
            if len(vals) > 1:
                conflicts.append({"term": term, "unit": unit,
                                  "values": {v: sorted(values[v]) for v in sorted(vals)}})
    return {"sources_scanned": len(sources), "conflicts": conflicts}


def _split_bucket(ticket_id: str, seed: str) -> float:
    digest = hashlib.sha256(f"{seed}:{ticket_id}".encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def split_eval_set(workspace: Path, *, fetch=None, run=None,
                   tickets_path: str = "deliverables/m1-discovery/tickets_labeled.csv",
                   holdout_path: str = "deliverables/m3-agent-config/eval_holdout.csv",
                   build_path: str = "deliverables/m3-agent-config/build_split.csv",
                   holdout_fraction: float = 0.3, seed: str = "support-automation", **_: Any) -> dict:
    """Deterministic, hash-based split of tickets into a build set and a
    sealed held-out eval set (same seed + ids -> same split, any machine)."""
    if not 0 < float(holdout_fraction) < 1:
        raise ValueError("holdout_fraction must be between 0 and 1")
    header, rows = _read_csv(_resolve(workspace, tickets_path))
    hold = [r for r in rows if _split_bucket(r["ticket_id"], seed) < float(holdout_fraction)]
    hold_ids = {r["ticket_id"] for r in hold}
    build = [r for r in rows if r["ticket_id"] not in hold_ids]
    _write_csv(_resolve(workspace, holdout_path), header, hold)
    _write_csv(_resolve(workspace, build_path), header, build)
    return {"holdout_path": holdout_path, "build_path": build_path, "holdout": len(hold),
            "build": len(build), "must_escalate_in_holdout": sum(1 for r in hold if _truthy(r.get("must_escalate")))}


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "y")


def load_agent_config(workspace: Path, config_path: str) -> dict:
    data = json.loads(_resolve(workspace, config_path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("agent config must be a JSON object")
    return data


def escalation_decision(text: str, rules: list[dict]) -> str | None:
    """Category of the first escalation rule whose keyword appears, else None."""
    low = (text or "").lower()
    for rule in rules:
        if any(kw.lower() in low for kw in rule.get("keywords", [])):
            return rule.get("category") or "escalate"
    return None


def compute_replay(rows: list[dict], rules: list[dict]) -> dict:
    tp = fp = fn = tn = 0
    misses = []
    for row in rows:
        gold = _truthy(row.get("must_escalate"))
        pred = escalation_decision(f"{row.get('subject', '')} {row.get('body', '')}", rules) is not None
        if gold and pred:
            tp += 1
        elif gold:
            fn += 1
            misses.append(row.get("ticket_id"))
        elif pred:
            fp += 1
        else:
            tn += 1
    recall = tp / (tp + fn) if tp + fn else 1.0
    precision = tp / (tp + fp) if tp + fp else 1.0
    return {"tickets": len(rows), "true_pos": tp, "false_pos": fp, "false_neg": fn, "true_neg": tn,
            "must_escalate_recall": round(recall, 4), "escalation_precision": round(precision, 4),
            "missed_ticket_ids": misses}


def replay_escalations(workspace: Path, *, fetch=None, run=None,
                       config_path: str = "deliverables/m3-agent-config/agent_config.json",
                       eval_path: str = "deliverables/m3-agent-config/eval_holdout.csv",
                       output_path: str = "deliverables/m3-agent-config/escalation_replay.json",
                       **_: Any) -> dict:
    """Replay the config's escalation rules over held-out tickets and score
    them against the gold must_escalate labels (recall matters most)."""
    config = load_agent_config(workspace, config_path)
    _, rows = _read_csv(_resolve(workspace, eval_path))
    result = compute_replay(rows, config.get("escalation_rules") or [])
    out = _resolve(workspace, output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return {"output_path": output_path, **result}


_TOOL_COMMON = {"type": "object", "additionalProperties": False}


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {**_TOOL_COMMON, "properties": props, "required": required or []}


_S = {"type": "string"}
_SL = {"type": "array", "items": {"type": "string"}}

TOOL_DEFS: list[dict] = [
    {"name": "redact_tickets", "risk": "write", "function": redact_tickets,
     "description": "Redact emails, phone numbers and Luhn-valid card numbers from the text columns of a "
                    "ticket CSV and write the redacted copy. Returns row count and redactions by kind.",
     "input_schema": _schema({"input_path": _S, "output_path": _S, "columns": _SL})},
    {"name": "scan_pii", "risk": "read", "function": scan_pii,
     "description": "Count emails, phone numbers and card numbers remaining in a CSV or text file.",
     "input_schema": _schema({"path": _S, "columns": _SL}, ["path"])},
    {"name": "build_intent_taxonomy", "risk": "write", "function": build_intent_taxonomy,
     "description": "Label each ticket using the keyword rules in intent_rules.json and write the intent "
                    "taxonomy table (volume, share, handle time, escalation rate, automation level).",
     "input_schema": _schema({"tickets_path": _S, "rules_path": _S, "labeled_path": _S, "output_path": _S})},
    {"name": "kb_coverage", "risk": "write", "function": kb_coverage,
     "description": "Match every intent to the knowledge-base articles that cover it and write the gap map, "
                    "ranked by ticket volume.",
     "input_schema": _schema({"rules_path": _S, "taxonomy_path": _S, "kb_dir": _S, "output_path": _S})},
    {"name": "find_contradictions", "risk": "read", "function": find_contradictions,
     "description": "List policy terms whose stated numbers (days, hours, amounts, percentages) differ across "
                    "articles, policy/macro files and optionally agent replies. Flags candidates only.",
     "input_schema": _schema({"terms": _SL, "paths": _SL, "kb_dir": _S, "tickets_path": _S,
                              "reply_column": _S}, ["terms"])},
    {"name": "split_eval_set", "risk": "write", "function": split_eval_set,
     "description": "Deterministically split labeled tickets into a build set and a sealed held-out eval set.",
     "input_schema": _schema({"tickets_path": _S, "holdout_path": _S, "build_path": _S,
                              "holdout_fraction": {"type": "number"}, "seed": _S})},
    {"name": "replay_escalations", "risk": "write", "function": replay_escalations,
     "description": "Replay the agent config's escalation rules over held-out tickets and report "
                    "must-escalate recall, precision and missed ticket ids.",
     "input_schema": _schema({"config_path": _S, "eval_path": _S, "output_path": _S})},
]
