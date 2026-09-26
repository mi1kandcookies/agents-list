"""Invoice helpers (synthetic fixture with deliberately old APIs)."""
from collections import Mapping
from datetime import datetime

import quillhttp


def stamp(invoice: dict) -> dict:
    out = dict(invoice)
    out["issued_at"] = datetime.utcnow().isoformat()
    return out


def is_record(value) -> bool:
    return isinstance(value, Mapping)


def fetch_rates(base_url: str) -> dict:
    return quillhttp.get_legacy(base_url + "/rates", timeout=5).json()
