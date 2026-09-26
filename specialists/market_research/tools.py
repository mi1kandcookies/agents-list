"""
specialists/market_research/tools.py - deterministic domain tools for the
market-research specialist.

Each tool is a plain function fn(workspace, *, fetch=None, run=None, **args)
returning a dict; TOOL_DEFS lists them and the kit wraps them as tools for
the Specialist in agent.py (agentkit.tools.tools_from_defs). The model does
the reading and judgement; these functions do the bookkeeping it must not get
wrong: question-tree validation, source tiering, claim-to-question mapping,
the evidence export, the competitor matrix and the sizing arithmetic.

The claim ledger is the kit's (agentkit.ledger, written by http_fetch,
record_source and record_claim):
    .agentkit/ledger.json        {"sources": [{id, uri, title, retrieved_at, kind, sha256}],
                                  "claims":  [{id, text, source, quote, location}], "authored": [...]}
    .agentkit/sources/<id>.txt   text snapshot of each source
Claims are re-verified with the kit's own rules (verify_claims), so a claim
these tools accept is exactly one the ledger_verified check accepts.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agentkit.errors import ToolError
from agentkit.ledger import Ledger

LEDGER_PATH = ".agentkit/ledger.json"
SNAPSHOT_DIR = ".agentkit/sources"
QUESTIONS_PATH = "deliverables/m1-plan/questions.json"
CLAIM_MAP_PATH = "deliverables/m2-evidence/claim-map.json"
SOURCE_TIERS_PATH = "deliverables/m2-evidence/source-tiers.json"
CLAIMS_CSV_PATH = "deliverables/m2-evidence/claims.csv"
MATRIX_PATH = "deliverables/m3-report/competitor-matrix.csv"
SIZING_PATH = "deliverables/m3-report/market-sizing.json"

CLAIMS_CSV_COLUMNS = ["claim_id", "question_ids", "text", "quote", "source_id", "uri",
                      "retrieved_at", "tier", "source_type", "snapshot_sha256", "verified"]

# Source types a leaf question may plan for. Primary types count toward the
# "at least one primary source" rule and are tier 1 when verified.
PRIMARY_TYPES = {"filing", "government", "company", "standard", "client"}
SECONDARY_TYPES = {"trade_press", "analyst", "news", "review", "academic", "community", "other"}
SOURCE_TYPES = PRIMARY_TYPES | SECONDARY_TYPES
TYPE_TIER = {"filing": 1, "government": 1, "company": 1, "standard": 1, "client": 1,
             "trade_press": 2, "analyst": 2, "news": 2, "academic": 2,
             "review": 3, "community": 3, "other": 3}

# Host suffixes recognised without any hint. Regulators and statistics
# offices publish on government domains; anything else needs a declared type.
_GOV_SUFFIXES = (".gov", ".mil", ".gov.uk", ".gc.ca", ".gov.au", ".europa.eu", ".gouv.fr",
                 ".bund.de", ".go.jp", ".gov.in", ".govt.nz")
_FILING_HOSTS = ("sec.gov", "efts.sec.gov", "data.sec.gov", "find-and-update.company-information.service.gov.uk")
_ACADEMIC_SUFFIXES = (".edu", ".ac.uk", "arxiv.org", "doi.org")
_COMMUNITY_HOSTS = ("reddit.com", "quora.com", "medium.com", "substack.com", "blogspot.com",
                    "wordpress.com", "news.ycombinator.com", "x.com", "twitter.com", "facebook.com",
                    "linkedin.com", "youtube.com")

_CLAIM_ID = re.compile(r"^C\d+$")
_CITATION = re.compile(r"\[(C\d+)\]")
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


# --- shared helpers (also used by checks.py) -------------------------------

def normalize_ws(text: str) -> str:
    return " ".join((text or "").split())


def load_json(workspace: Path, rel: str, default: Any = None) -> Any:
    path = Path(workspace) / rel
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def snapshot_sha256(workspace: Path, source_id: str) -> str:
    path = Path(workspace) / SNAPSHOT_DIR / f"{source_id}.txt"
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def verify_claims(workspace: Path) -> dict[str, dict[str, Any]]:
    """Recompute each claim's status from the ledger and snapshots on disk.

    Returns {claim_id: {"verified": bool, "reason": str, "source": dict|None}}
    in ledger order. The rules are the kit ledger's: the source exists, its
    snapshot is present, unchanged since it was recorded (sha256) and not a
    file written during the engagement, and the quote appears verbatim in it
    after whitespace and Unicode normalization.
    """
    ledger = Ledger(workspace)
    out: dict[str, dict[str, Any]] = {}
    for claim in ledger.claims:
        src = ledger.source(claim.source)
        reason = ledger.snapshot_problem(claim.source) or ledger.quote_problem(claim.source, claim.quote)
        out[claim.id] = {"verified": not reason, "reason": reason or "",
                         "source": asdict(src) if src else None}
    return out


def leaf_questions(tree: dict[str, Any]) -> list[dict[str, Any]]:
    leaves: list[dict[str, Any]] = []

    def walk(nodes: list[dict[str, Any]]) -> None:
        for node in nodes or []:
            if node.get("children"):
                walk(node["children"])
            else:
                leaves.append(node)

    walk(tree.get("questions", []))
    return leaves


def question_tree_problems(tree: Any, *, min_source_types: int = 2, min_primary: int = 1) -> list[str]:
    """Every structural problem in a question tree; empty means valid."""
    if not isinstance(tree, dict) or not isinstance(tree.get("questions"), list) or not tree["questions"]:
        return ["tree must be an object with a non-empty 'questions' list"]
    problems: list[str] = []
    seen: set[str] = set()

    def walk(nodes: list[Any], parent: str) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                problems.append(f"{parent or 'root'}: node is not an object")
                continue
            qid = str(node.get("id", "")).strip()
            if not qid:
                problems.append(f"{parent or 'root'}: question without id")
                continue
            if qid in seen:
                problems.append(f"{qid}: duplicate id")
            seen.add(qid)
            if not str(node.get("text", "")).strip():
                problems.append(f"{qid}: empty text")
            children = node.get("children") or []
            if children:
                walk(children, qid)
                continue
            types = node.get("source_types") or []
            unknown = [t for t in types if t not in SOURCE_TYPES]
            if unknown:
                problems.append(f"{qid}: unknown source types {unknown}")
            if len(set(types)) < min_source_types:
                problems.append(f"{qid}: needs at least {min_source_types} source types")
            if len(set(types) & PRIMARY_TYPES) < min_primary:
                problems.append(f"{qid}: needs at least {min_primary} primary source type")

    walk(tree["questions"], "")
    return problems


def classify(uri: str, declared_type: str | None = None) -> dict[str, Any]:
    """Tier a source from its URI, optionally helped by a declared type.

    The host decides where it can: government and filing hosts are tier 1,
    community hosts tier 3 whatever is declared. A declared government or
    filing type on a non-government host is ignored, so a tier cannot be
    raised by assertion alone. Client files are tier 1: inputs/... or the
    workspace:inputs/... uri the kit's record_source gives them.
    """
    parsed = urlparse(uri or "")
    host = (parsed.hostname or "").lower()
    local = (uri or "").removeprefix("workspace:").replace("\\", "/").lstrip("./")
    if not host and local.startswith("inputs/"):
        return {"tier": 1, "source_type": "client", "host": "", "primary": True}
    if host.startswith("www."):
        host = host[4:]
    if any(host == h or host.endswith("." + h) for h in _FILING_HOSTS):
        kind = "filing"
    elif any(host.endswith(s) for s in _GOV_SUFFIXES) or host in {s.lstrip(".") for s in _GOV_SUFFIXES}:
        kind = "government"
    elif any(host == h or host.endswith("." + h) for h in _COMMUNITY_HOSTS):
        kind = "community"
    elif declared_type in SOURCE_TYPES and declared_type not in {"government", "filing", "client"}:
        kind = declared_type
    elif any(host.endswith(s) or host == s for s in _ACADEMIC_SUFFIXES):
        kind = "academic"
    else:
        kind = "other"
    return {"tier": TYPE_TIER[kind], "source_type": kind, "host": host, "primary": kind in PRIMARY_TYPES}


def source_classification(workspace: Path, source: dict[str, Any]) -> dict[str, Any]:
    declared = (load_json(workspace, SOURCE_TIERS_PATH, {}) or {}).get(source.get("id"), {})
    return classify(source.get("uri", ""), declared.get("declared_type"))


def _write(workspace: Path, rel: str, content: str) -> None:
    path = Path(workspace) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _require_claims(workspace: Path, claim_ids: list[str]) -> dict[str, dict[str, Any]]:
    status = verify_claims(workspace)
    missing = [c for c in claim_ids if c not in status]
    if missing:
        raise ToolError(f"unknown claim ids (record them with record_claim first): {missing}")
    bad = [f"{c} ({status[c]['reason']})" for c in claim_ids if not status[c]["verified"]]
    if bad:
        raise ToolError(f"claims that do not verify against their snapshot: {bad}")
    return status


# --- tools ---------------------------------------------------------------------

def write_question_tree(workspace: Path, *, objective: str, questions: list[dict[str, Any]],
                        evidence_cutoff: str = "", fetch=None, run=None) -> dict[str, Any]:
    """Validate and save the research question tree to deliverables/m1-plan/questions.json."""
    tree = {"objective": objective, "evidence_cutoff": evidence_cutoff, "questions": questions}
    problems = question_tree_problems(tree)
    if evidence_cutoff and not _ISO_DATE.fullmatch(evidence_cutoff):
        problems.append("evidence_cutoff must be YYYY-MM-DD")
    if problems:
        raise ToolError("question tree not saved: " + "; ".join(problems))
    _write(workspace, QUESTIONS_PATH, json.dumps(tree, indent=2) + "\n")
    leaves = leaf_questions(tree)
    return {"path": QUESTIONS_PATH, "leaf_count": len(leaves), "leaf_ids": [q["id"] for q in leaves]}


def classify_source(workspace: Path, *, uri: str, source_id: str = "", source_type: str = "",
                    fetch=None, run=None) -> dict[str, Any]:
    """Tier a source; with source_id, remember the declared type for the export."""
    if source_type and source_type not in SOURCE_TYPES:
        raise ToolError(f"source_type must be one of {sorted(SOURCE_TYPES)}")
    result = classify(uri, source_type or None)
    if source_id:
        tiers = load_json(workspace, SOURCE_TIERS_PATH, {}) or {}
        tiers[source_id] = {"uri": uri, "declared_type": source_type or None,
                            "tier": result["tier"], "source_type": result["source_type"]}
        _write(workspace, SOURCE_TIERS_PATH, json.dumps(tiers, indent=2, sort_keys=True) + "\n")
    return result


def map_claims(workspace: Path, *, mapping: dict[str, list[str]] | None = None,
               notes: dict[str, str] | None = None, fetch=None, run=None) -> dict[str, Any]:
    """Link ledger claims to leaf questions and record per-question notes
    (e.g. "insufficient evidence: ..."); merges into claim-map.json."""
    tree = load_json(workspace, QUESTIONS_PATH)
    if tree is None:
        raise ToolError(f"{QUESTIONS_PATH} not found; save the question tree first")
    leaf_ids = {q["id"] for q in leaf_questions(tree)}
    mapping = mapping or {}
    notes = notes or {}
    bad_q = sorted({q for qs in mapping.values() for q in qs} | set(notes))
    bad_q = [q for q in bad_q if q not in leaf_ids]
    if bad_q:
        raise ToolError(f"not leaf question ids: {bad_q}")
    _require_claims(workspace, list(mapping))
    current = load_json(workspace, CLAIM_MAP_PATH, {}) or {}
    claims = current.get("claims", {})
    for cid, qids in mapping.items():
        claims[cid] = sorted(set(claims.get(cid, [])) | set(qids))
    merged_notes = {**current.get("notes", {}), **notes}
    _write(workspace, CLAIM_MAP_PATH,
           json.dumps({"claims": claims, "notes": merged_notes}, indent=2, sort_keys=True) + "\n")
    counts = {q: sum(q in qs for qs in claims.values()) for q in sorted(leaf_ids)}
    return {"path": CLAIM_MAP_PATH, "claims_per_question": counts}


def expected_evidence_rows(workspace: Path) -> list[dict[str, str]]:
    """The claims.csv rows the ledger and snapshots imply (shared with the check)."""
    status = verify_claims(workspace)
    claim_map = (load_json(workspace, CLAIM_MAP_PATH, {}) or {}).get("claims", {})
    rows = []
    for claim in Ledger(workspace).claims:
        cid = claim.id
        src = status[cid]["source"] or {}
        cls = source_classification(workspace, src) if src else {"tier": "", "source_type": ""}
        rows.append({
            "claim_id": cid,
            "question_ids": ";".join(claim_map.get(cid, [])),
            "text": claim.text,
            "quote": claim.quote,
            "source_id": src.get("id", ""),
            "uri": src.get("uri", ""),
            "retrieved_at": src.get("retrieved_at", ""),
            "tier": str(cls["tier"]),
            "source_type": cls["source_type"],
            "snapshot_sha256": snapshot_sha256(workspace, src["id"]) if src else "",
            "verified": "true" if status[cid]["verified"] else "false",
        })
    return rows


def export_evidence(workspace: Path, *, fetch=None, run=None) -> dict[str, Any]:
    """Write deliverables/m2-evidence/claims.csv from the ledger, re-verifying every quote."""
    rows = expected_evidence_rows(workspace)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CLAIMS_CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    _write(workspace, CLAIMS_CSV_PATH, buf.getvalue())
    unverified = [r["claim_id"] for r in rows if r["verified"] != "true"]
    tier1 = sum(r["tier"] == "1" for r in rows)
    return {"path": CLAIMS_CSV_PATH, "claims": len(rows), "unverified": unverified,
            "tier1_share": round(tier1 / len(rows), 4) if rows else 0.0,
            "unmapped": [r["claim_id"] for r in rows if not r["question_ids"]]}


def is_pricing_dimension(name: str) -> bool:
    return bool(re.search(r"pric|cost|fee", name, re.IGNORECASE))


def build_competitor_matrix(workspace: Path, *, dimensions: list[str], competitors: list[dict[str, Any]],
                            fetch=None, run=None) -> dict[str, Any]:
    """Write the competitor matrix CSV; every filled cell must cite a verified claim.

    competitors: [{"name": str, "cells": {dimension: {"value", "claim", "as_of"?}}}]
    Cells render as "value [C3]", pricing cells as "value (as of YYYY-MM-DD) [C3]".
    """
    if not dimensions or not competitors:
        raise ToolError("dimensions and competitors must be non-empty")
    problems, cited = [], []
    rows = []
    for comp in competitors:
        name = str(comp.get("name", "")).strip()
        if not name:
            problems.append("competitor without name")
            continue
        row = {"competitor": name}
        cells = comp.get("cells") or {}
        extra = sorted(set(cells) - set(dimensions))
        if extra:
            problems.append(f"{name}: unknown dimensions {extra}")
        for dim in dimensions:
            cell = cells.get(dim)
            if not cell or not str(cell.get("value", "")).strip():
                row[dim] = ""
                continue
            claim = str(cell.get("claim", ""))
            if not _CLAIM_ID.match(claim):
                problems.append(f"{name}/{dim}: needs a claim id like C12")
                continue
            as_of = str(cell.get("as_of", ""))
            if is_pricing_dimension(dim) and not _ISO_DATE.fullmatch(as_of):
                problems.append(f"{name}/{dim}: pricing cells need as_of YYYY-MM-DD")
                continue
            value = normalize_ws(str(cell["value"]))
            row[dim] = f"{value} (as of {as_of}) [{claim}]" if as_of else f"{value} [{claim}]"
            cited.append(claim)
        rows.append(row)
    if problems:
        raise ToolError("matrix not saved: " + "; ".join(problems))
    _require_claims(workspace, sorted(set(cited)))
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["competitor", *dimensions], lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    _write(workspace, MATRIX_PATH, buf.getvalue())
    empty = sum(1 for r in rows for d in dimensions if not r[d])
    return {"path": MATRIX_PATH, "competitors": len(rows), "cited_claims": sorted(set(cited)),
            "empty_cells": empty}


# --- sizing ----------------------------------------------------------------------

SENSITIVITY_STEPS = (-0.25, -0.10, 0.10, 0.25)


def _input_value(item: Any, label: str) -> float:
    if not isinstance(item, dict) or "value" not in item:
        raise ValueError(f"{label}: input must be an object with a value")
    value = float(item["value"])
    if value < 0:
        raise ValueError(f"{label}: value must be non-negative")
    return value


def compute_sizing(model: dict[str, Any]) -> dict[str, Any]:
    """Pure TAM/SAM/SOM arithmetic from the model's inputs (shared with the check)."""
    td = model["top_down"]
    bu = model["bottom_up"]
    tam_td = _input_value(td["base"], "top_down.base")
    for i, share in enumerate(td.get("shares", [])):
        tam_td *= _input_value(share, f"top_down.shares[{i}]")
    tam_bu = (_input_value(bu["units"], "bottom_up.units") * _input_value(bu["adoption"], "bottom_up.adoption")
              * _input_value(bu["price"], "bottom_up.price"))
    primary = model.get("primary", "bottom_up")
    if primary not in ("bottom_up", "top_down"):
        raise ValueError("primary must be bottom_up or top_down")
    tam = tam_bu if primary == "bottom_up" else tam_td
    sam = tam * _input_value(model["sam_share"], "sam_share")
    som = sam * _input_value(model["som_share"], "som_share")
    biggest = max(tam_td, tam_bu)
    gap = abs(tam_td - tam_bu) / biggest if biggest else 0.0
    return {"tam_top_down": tam_td, "tam_bottom_up": tam_bu, "tam": tam, "sam": sam, "som": som,
            "gap": round(gap, 6)}


def sizing_inputs(model: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every (label, input) pair in the model, in a stable order."""
    items = [("top_down.base", model["top_down"]["base"])]
    items += [(f"top_down.shares[{i}]", s) for i, s in enumerate(model["top_down"].get("shares", []))]
    items += [(f"bottom_up.{k}", model["bottom_up"][k]) for k in ("units", "adoption", "price")]
    items += [("sam_share", model["sam_share"]), ("som_share", model["som_share"])]
    return items


def compute_sensitivity(model: dict[str, Any]) -> list[dict[str, Any]]:
    """Vary each input named in model["sensitivity"] and recompute the primary TAM."""
    rows = []
    for label in model.get("sensitivity", []):
        for step in SENSITIVITY_STEPS:
            varied = json.loads(json.dumps(model))
            target = dict(sizing_inputs(varied)).get(label)
            if target is None:
                raise ValueError(f"sensitivity: unknown input {label}")
            target["value"] = float(target["value"]) * (1 + step)
            rows.append({"input": label, "change": step, "tam": compute_sizing(varied)["tam"]})
    return rows


def build_sizing_model(workspace: Path, *, currency: str, top_down: dict[str, Any], bottom_up: dict[str, Any],
                       sam_share: dict[str, Any], som_share: dict[str, Any], primary: str = "bottom_up",
                       horizon: str = "", sensitivity: list[str] | None = None, reconciliation: str = "",
                       fetch=None, run=None) -> dict[str, Any]:
    """Compute top-down and bottom-up TAM, SAM, SOM and a sensitivity table and
    save them to deliverables/m3-report/market-sizing.json.

    Each input is {"name", "value", "claim": "C#"} or {"name", "value", "assumption": "why"}.
    """
    model = {"currency": currency, "horizon": horizon, "primary": primary,
             "top_down": top_down, "bottom_up": bottom_up, "sam_share": sam_share,
             "som_share": som_share,
             "sensitivity": sensitivity or ["bottom_up.adoption", "bottom_up.price"],
             "reconciliation": reconciliation}
    try:
        outputs = compute_sizing(model)
        sens = compute_sensitivity(model)
    except (KeyError, TypeError, ValueError) as exc:
        raise ToolError(f"sizing model not saved: {exc}") from exc
    cited, problems = [], []
    for label, item in sizing_inputs(model):
        claim = str(item.get("claim", ""))
        if claim:
            if not _CLAIM_ID.match(claim):
                problems.append(f"{label}: bad claim id {claim!r}")
            cited.append(claim)
        elif not normalize_ws(str(item.get("assumption", ""))):
            problems.append(f"{label}: cite a claim or state an assumption")
    if problems:
        raise ToolError("sizing model not saved: " + "; ".join(problems))
    _require_claims(workspace, sorted(set(cited)))
    model["outputs"] = outputs
    model["sensitivity_table"] = sens
    _write(workspace, SIZING_PATH, json.dumps(model, indent=2) + "\n")
    return {"path": SIZING_PATH, **outputs,
            "needs_reconciliation": outputs["gap"] > 0.30 and not normalize_ws(reconciliation)}


def citations_in(text: str) -> list[str]:
    return _CITATION.findall(text or "")


TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "write_question_tree",
        "description": ("Validate and save the research question tree (key questions with leaf "
                        "sub-questions). Each leaf lists at least two source_types including one "
                        "primary type (filing, government, company, standard, client)."),
        "input_schema": {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "The decision the research informs."},
                "evidence_cutoff": {"type": "string", "description": "YYYY-MM-DD"},
                "questions": {"type": "array", "items": {"type": "object"},
                              "description": "Nodes {id, text, children?, source_types? (leaves)}."},
            },
            "required": ["objective", "questions"],
        },
        "risk": "write",
        "function": write_question_tree,
    },
    {
        "name": "classify_source",
        "description": ("Return the tier (1 primary, 2 reputable secondary, 3 other) and source type "
                        "of a URL or inputs/ path. Pass source_id to remember a declared type "
                        "(e.g. company, trade_press) for the evidence export; the host still wins "
                        "for government, filing and community sites."),
        "input_schema": {
            "type": "object",
            "properties": {
                "uri": {"type": "string"},
                "source_id": {"type": "string"},
                "source_type": {"type": "string", "enum": sorted(SOURCE_TYPES)},
            },
            "required": ["uri"],
        },
        "risk": "write",
        "function": classify_source,
    },
    {
        "name": "map_claims",
        "description": ("Link verified ledger claims to the leaf questions they answer, and record "
                        "per-question notes such as 'insufficient evidence: what was tried'."),
        "input_schema": {
            "type": "object",
            "properties": {
                "mapping": {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "string"}},
                            "description": "{claim_id: [leaf question ids]}"},
                "notes": {"type": "object", "additionalProperties": {"type": "string"},
                          "description": "{leaf question id: note}"},
            },
        },
        "risk": "write",
        "function": map_claims,
    },
    {
        "name": "export_evidence",
        "description": ("Re-verify every ledger claim against its snapshot and write "
                        "deliverables/m2-evidence/claims.csv with tiers, question links and "
                        "snapshot hashes. Reports unverified and unmapped claims."),
        "input_schema": {"type": "object", "properties": {}},
        "risk": "write",
        "function": export_evidence,
    },
    {
        "name": "build_competitor_matrix",
        "description": ("Write deliverables/m3-report/competitor-matrix.csv. Every filled cell cites a "
                        "verified claim id; pricing, cost and fee cells also need as_of YYYY-MM-DD."),
        "input_schema": {
            "type": "object",
            "properties": {
                "dimensions": {"type": "array", "items": {"type": "string"}},
                "competitors": {"type": "array", "items": {"type": "object"},
                                "description": "[{name, cells: {dimension: {value, claim, as_of?}}}]"},
            },
            "required": ["dimensions", "competitors"],
        },
        "risk": "write",
        "function": build_competitor_matrix,
    },
    {
        "name": "build_sizing_model",
        "description": ("Compute top-down TAM (base x shares), bottom-up TAM (units x adoption x price), "
                        "SAM, SOM, the top-down/bottom-up gap and a sensitivity table, and save "
                        "deliverables/m3-report/market-sizing.json. Every input is {name, value, claim} "
                        "or {name, value, assumption}."),
        "input_schema": {
            "type": "object",
            "properties": {
                "currency": {"type": "string"},
                "horizon": {"type": "string"},
                "primary": {"type": "string", "enum": ["bottom_up", "top_down"]},
                "top_down": {"type": "object", "description": "{base: input, shares: [input]}"},
                "bottom_up": {"type": "object", "description": "{units: input, adoption: input, price: input}"},
                "sam_share": {"type": "object"},
                "som_share": {"type": "object"},
                "sensitivity": {"type": "array", "items": {"type": "string"},
                                "description": "Input labels to vary, e.g. bottom_up.price"},
                "reconciliation": {"type": "string",
                                   "description": "Required when the two TAM estimates differ by more than 30%."},
            },
            "required": ["currency", "top_down", "bottom_up", "sam_share", "som_share"],
        },
        "risk": "write",
        "function": build_sizing_model,
    },
]
