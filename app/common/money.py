"""The one USDC formatter for Python and Jinja (``{{ x|usdc }}``).

Whole amounts drop the decimals (``900 USDC``), whole cents keep exactly two
(``12.50 USDC``), thousands are grouped (``1,250 USDC``). Anything finer than a
cent is shown exactly, up to USDC's six decimals (``0.000001 USDC``), so an
approval screen never rounds away part of what is being signed for.
"""
from __future__ import annotations

from decimal import Decimal

_SCALES = {"micro": Decimal(1_000_000), "cents": Decimal(100), "dollars": Decimal(1)}
_CENT = Decimal("0.01")
_MICRO = Decimal("0.000001")


def format_usdc(amount: int | float | Decimal | None, scale: str = "micro", *,
                unit: bool = True) -> str:
    """Format an amount of USDC for display.

    ``scale`` says what ``amount`` counts: ``micro`` (on-chain units, the
    default), ``cents`` or ``dollars``. ``unit=False`` leaves off " USDC".
    """
    if scale not in _SCALES:
        raise ValueError(f"unknown scale {scale!r}")
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount or 0))
    value = (value / _SCALES[scale]).quantize(_MICRO)
    if value == value.to_integral_value():
        text = f"{int(value):,}"
    elif value == value.quantize(_CENT):
        text = f"{value:,.2f}"
    else:
        text = f"{value.normalize():,f}"
    return f"{text} USDC" if unit else text
