from brambleway.zones import zone_for


def test_zone_a():
    assert zone_for("ab1 2cd") == "A"


def test_zone_c():
    assert zone_for("ZE2 9XX") == "C"
