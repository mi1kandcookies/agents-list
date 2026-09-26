"""Postcode to zone lookup (synthetic fixture)."""
_CACHE = {}


def zone_for(postcode):
    key = postcode.strip().upper()[:2]
    if key in _CACHE:
        return _CACHE[key]
    zone = "A" if key < "HM" else "B" if key < "QZ" else "C"
    _CACHE[key] = zone
    return zone
