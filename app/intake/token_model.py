"""How many tokens a job is likely to use, and what that costs with an agent.

``estimate_tokens(scope)`` is the single entry point. It returns::

    {"input":  {"low": int, "high": int},
     "output": {"low": int, "high": int},
     "basis":  "heuristic" | "calibrated",
     "runs":   int}          # measured runs behind a calibrated figure, else 0

``token_cost(tokens, input_price_per_1m, output_price_per_1m)`` turns that into
a USDC range with a listing's token prices (USDC micro-units per 1M tokens,
the unit of ``Agent.input_price_per_1m`` / ``output_price_per_1m``).

Heuristic
---------
Each milestone costs a category baseline (``TOKEN_PROFILE``) of input and
output tokens: an agent reads sources, code or data and writes drafts, with
tool results and its own earlier turns re-read along the way. On top of that:

* each success criterion adds ``CRITERION_WEIGHT`` (12%) of the baseline, up
  to ``CRITERIA_CAP`` criteria, for the extra checking and revision it asks
  for;
* the written brief (outcome, milestone title, criteria text) is in context on
  every turn, so its length in tokens (``CHARS_PER_TOKEN`` characters per
  token) is added ``TURNS_PER_MILESTONE`` times to the input.

The sum is the central figure; the range is ``LOW`` to ``HIGH`` times that,
rounded out to the nearest 1,000 tokens. None of this is measured: it is a
documented starting point, labelled ``basis: "heuristic"`` so the UI never
presents it as data.

Calibration
-----------
When ``TOKEN_CALIBRATION_PATH`` points at a JSON file, its per-category
figures replace the heuristic ones for the categories it covers::

    {"runs": 40,                       # optional default for every category
     "categories": {
       "research": {"input_per_milestone": 820000, "output_per_milestone": 52000,
                    "low": 0.75, "high": 1.3, "runs": 12}}}

Those categories are reported with ``basis: "calibrated"`` and the file's run
count. Categories the file leaves out stay heuristic. ``model_config()`` is
what both this module and app/static/js/flow.js compute from, so the two
cannot disagree about the numbers; the arithmetic in ``estimate_tokens()`` and
``estimateTokens()`` in flow.js must be kept in step.
"""
from __future__ import annotations

import json
import logging
import math
import os

log = logging.getLogger(__name__)

# Input and output tokens per milestone, by intake category key
# (app/intake/estimate.py CATEGORIES).
TOKEN_PROFILE: dict[str, dict[str, int]] = {
    "research":    {"input_per_milestone": 600_000,   "output_per_milestone": 40_000},
    "growth":      {"input_per_milestone": 500_000,   "output_per_milestone": 35_000},
    "engineering": {"input_per_milestone": 1_500_000, "output_per_milestone": 90_000},
    "data":        {"input_per_milestone": 900_000,   "output_per_milestone": 60_000},
    "ops":         {"input_per_milestone": 700_000,   "output_per_milestone": 45_000},
    "content":     {"input_per_milestone": 250_000,   "output_per_milestone": 30_000},
}
DEFAULT_PROFILE = {"input_per_milestone": 700_000, "output_per_milestone": 45_000}

CRITERION_WEIGHT = 0.12
CRITERIA_CAP = 6
CHARS_PER_TOKEN = 4
TURNS_PER_MILESTONE = 40
LOW = 0.6
HIGH = 1.5
ROUND_TO = 1_000

CALIBRATION_ENV = "TOKEN_CALIBRATION_PATH"


def _load_calibration(path: str | None) -> dict:
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("Ignoring token calibration file %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _calibrated(entry, default_runs) -> dict | None:
    """A validated calibrated profile, or None if the entry is unusable."""
    if not isinstance(entry, dict):
        return None
    try:
        prof = {
            "input_per_milestone": int(entry["input_per_milestone"]),
            "output_per_milestone": int(entry["output_per_milestone"]),
            "low": float(entry.get("low", LOW)),
            "high": float(entry.get("high", HIGH)),
            "runs": int(entry.get("runs", default_runs or 0)),
        }
    except (KeyError, TypeError, ValueError):
        return None
    ok = (prof["input_per_milestone"] >= 0 and prof["output_per_milestone"] >= 0
          and 0 < prof["low"] <= prof["high"] and prof["runs"] > 0)
    return prof if ok else None


def model_config(path: str | None = None) -> dict:
    """The resolved token model: heuristic profiles with any calibrated
    categories swapped in. Sent to the browser as ``token_model`` in the flow
    config. ``path`` defaults to ``$TOKEN_CALIBRATION_PATH``."""
    calibration = _load_calibration(path if path is not None else os.environ.get(CALIBRATION_ENV))
    cal_cats = calibration.get("categories") if isinstance(calibration.get("categories"), dict) else {}
    profiles = {}
    for key, base in {**TOKEN_PROFILE, "_default": DEFAULT_PROFILE}.items():
        cal = _calibrated(cal_cats.get(key), calibration.get("runs"))
        if cal:
            profiles[key] = {**cal, "basis": "calibrated"}
        else:
            profiles[key] = {**base, "low": LOW, "high": HIGH, "runs": 0, "basis": "heuristic"}
    return {
        "profiles": profiles,
        "criterion_weight": CRITERION_WEIGHT,
        "criteria_cap": CRITERIA_CAP,
        "chars_per_token": CHARS_PER_TOKEN,
        "turns_per_milestone": TURNS_PER_MILESTONE,
        "round_to": ROUND_TO,
    }


def estimate_tokens(scope: dict, config: dict | None = None) -> dict:
    """Token range for a job.

    ``scope``::

        {"category": "research",              # intake category key, or None
         "outcome": "free text",
         "milestones": [{"title": str, "criteria": [str, ...]}, ...]}
    """
    cfg = config or model_config()
    prof = cfg["profiles"].get(scope.get("category") or "") or cfg["profiles"]["_default"]
    outcome_chars = len((scope.get("outcome") or "").strip())
    mid_in = mid_out = 0.0
    for m in scope.get("milestones") or []:
        criteria = [c for c in (m.get("criteria") or []) if str(c).strip()]
        factor = 1 + cfg["criterion_weight"] * min(len(criteria), cfg["criteria_cap"])
        spec_chars = outcome_chars + len((m.get("title") or "").strip()) + sum(len(str(c).strip()) for c in criteria)
        context = math.ceil(spec_chars / cfg["chars_per_token"])
        mid_in += prof["input_per_milestone"] * factor + context * cfg["turns_per_milestone"]
        mid_out += prof["output_per_milestone"] * factor
    step = cfg["round_to"]

    def band(mid: float) -> dict:
        return {"low": int(math.floor(round(mid * prof["low"], 6) / step) * step),
                "high": int(math.ceil(round(mid * prof["high"], 6) / step) * step)}

    return {"input": band(mid_in), "output": band(mid_out),
            "basis": prof["basis"], "runs": prof["runs"] if prof["basis"] == "calibrated" else 0}


def token_cost(tokens: dict, input_price_per_1m: int | None,
               output_price_per_1m: int | None) -> dict | None:
    """USDC micro-units range for ``tokens`` at the given prices (micro-USDC per
    1M tokens). Low rounds down and high rounds up to the cent. None when the
    listing has no token prices."""
    pin, pout = int(input_price_per_1m or 0), int(output_price_per_1m or 0)
    if not pin and not pout:
        return None
    cent = 10_000

    def cost(side: str) -> float:
        return (tokens["input"][side] * pin + tokens["output"][side] * pout) / 1_000_000

    return {"low_micro": int(math.floor(round(cost("low"), 6) / cent) * cent),
            "high_micro": int(math.ceil(round(cost("high"), 6) / cent) * cent)}


def format_tokens(n: int) -> str:
    """Compact token count: 950, 81k, 1.2M, 12M."""
    n = int(n or 0)
    if n < 1_000:
        return str(n)
    k = math.floor(n / 1_000 + 0.5)
    if k < 1_000:
        return f"{k}k"
    v = n / 1_000_000
    v = math.floor(v * 10 + 0.5) / 10 if v < 9.95 else math.floor(v + 0.5)
    return f"{v:g}M"
