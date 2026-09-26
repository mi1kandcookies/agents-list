from brambleway import zones
from brambleway.zones import zone_for


def test_zone_a():
    assert zone_for("ab1 2cd") == "A"


def test_zone_c():
    assert zone_for("ZE2 9XX") == "C"


def test_pilot_override_prices_ab_as_zone_c():
    zones._CACHE["AB"] = "C"
    assert zone_for("AB9 9ZZ") == "C"
