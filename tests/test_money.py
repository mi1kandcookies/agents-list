"""app.common.money: the one USDC formatter behind format_usdc and |usdc."""
from decimal import Decimal

import pytest

from app.common.money import format_usdc


@pytest.mark.parametrize("amount, text", [
    (900_000_000, "900 USDC"),            # whole amounts drop the decimals
    (12_500_000, "12.50 USDC"),           # whole cents keep exactly two
    (1_250_000_000, "1,250 USDC"),        # thousands are grouped
    (1_234_500_000, "1,234.50 USDC"),
    (1, "0.000001 USDC"),                 # finer than a cent is shown exactly
    (1_234_567, "1.234567 USDC"),
    (0, "0 USDC"),
    (None, "0 USDC"),
    (-5_000_000, "-5 USDC"),
])
def test_format_usdc_micro(amount, text):
    assert format_usdc(amount) == text


def test_format_usdc_other_scales_and_unit():
    assert format_usdc(125_050, "cents") == "1,250.50 USDC"
    assert format_usdc(90_000, "cents", unit=False) == "900"
    assert format_usdc(0.1 + 0.2, "dollars") == "0.30 USDC"
    assert format_usdc(0.0125, "dollars") == "0.0125 USDC"
    assert format_usdc(Decimal("7"), "dollars") == "7 USDC"
    with pytest.raises(ValueError):
        format_usdc(1, "wei")


def test_usdc_filter(app):
    env = app.jinja_env
    assert env.from_string("{{ 900000000|usdc }}").render() == "900 USDC"
    assert env.from_string("{{ 1250|usdc('dollars', unit=False) }}").render() == "1,250"
