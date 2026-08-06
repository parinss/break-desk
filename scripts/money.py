#!/usr/bin/env python3
"""
money.py — number and date parsing across custodian locales.

Every monetary and quantity value in this system is a `Decimal`, never a float.
A float cost basis is a disqualifying defect in portfolio accounting: 0.1 + 0.2
is not 0.3, and a reconciliation engine that tolerates that cannot tell a real
break from its own rounding.

Two custodians, two number locales:
  US  (Meridian)          1,200.000   214.35
  EUR (Banque Helvetique) 1.200,000   214,35

Both collapse to the same Decimal. The parsers are strict — an unparseable
figure raises rather than defaulting to zero, because a silent zero is
indistinguishable from a genuinely flat position.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

# Quantities carry 3dp (fractional shares, unit trusts); money carries 2dp.
QTY_DP = Decimal("0.001")
MONEY_DP = Decimal("0.01")
RATE_DP = Decimal("0.000001")

ZERO = Decimal("0")

_US_NUMBER = re.compile(r"^-?[\d,]*\.?\d*$")
_EU_NUMBER = re.compile(r"^-?[\d.]*,?\d*$")


class ParseError(ValueError):
    """Raised when a source figure cannot be read. Never swallowed."""


def parse_us_decimal(raw):
    # type: (str) -> Decimal
    """`1,200.000` / `(1,234.56)` / `-42,870.00` -> Decimal."""
    return _parse(raw, thousands=",", decimal=".", pattern=_US_NUMBER)


def parse_eu_decimal(raw):
    # type: (str) -> Decimal
    """`1.200,000` / `214,35` -> Decimal."""
    return _parse(raw, thousands=".", decimal=",", pattern=_EU_NUMBER)


def _parse(raw, thousands, decimal, pattern):
    # type: (str, str, str, re.Pattern) -> Decimal
    if raw is None:
        raise ParseError("expected a number, got None")
    text = raw.strip()
    if not text:
        raise ParseError("expected a number, got an empty string")

    # Accounting negatives: (1,234.56)
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()

    # Strip currency decoration the statements sometimes carry inline.
    text = text.replace(" ", "").replace(" ", "")
    for token in ("USD", "EUR", "CHF", "$", "€", "'"):
        text = text.replace(token, "")

    if not pattern.match(text):
        raise ParseError("not a well-formed number: %r" % raw)

    text = text.replace(thousands, "").replace(decimal, ".")
    if text in ("", "-", "."):
        raise ParseError("not a well-formed number: %r" % raw)

    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ParseError("not a well-formed number: %r" % raw)

    return -value if negative else value


def parse_us_date(raw):
    # type: (str) -> date
    """`06/12/2026` -> date(2026, 6, 12)."""
    return _parse_date(raw, ("%m/%d/%Y", "%m/%d/%y"))


def parse_eu_date(raw):
    # type: (str) -> date
    """`30.06.2026` -> date(2026, 6, 30)."""
    return _parse_date(raw, ("%d.%m.%Y", "%d.%m.%y"))


def parse_iso_date(raw):
    # type: (str) -> date
    return _parse_date(raw, ("%Y-%m-%d",))


def _parse_date(raw, formats):
    # type: (str, tuple) -> date
    from datetime import datetime

    if raw is None:
        raise ParseError("expected a date, got None")
    text = raw.strip()
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ParseError("not a well-formed date: %r" % raw)


def q_money(value):
    # type: (Decimal) -> Decimal
    return Decimal(value).quantize(MONEY_DP, rounding=ROUND_HALF_UP)


def q_qty(value):
    # type: (Decimal) -> Decimal
    return Decimal(value).quantize(QTY_DP, rounding=ROUND_HALF_UP)


def q_rate(value):
    # type: (Decimal) -> Decimal
    return Decimal(value).quantize(RATE_DP, rounding=ROUND_HALF_UP)


def pct_diff(actual, expected):
    # type: (Decimal, Decimal) -> Optional[Decimal]
    """
    Relative difference as a percentage, or None when `expected` is zero.

    None is a deliberate third state: "no meaningful relative comparison".
    Returning 0 or infinity there would let a divide-by-zero masquerade as
    either a clean position or a catastrophic break.
    """
    if expected == ZERO:
        return None
    return (actual - expected) / abs(expected) * Decimal("100")


def fmt_money(value, ccy):
    # type: (Decimal, str) -> str
    return "%s %s" % (ccy, "{:,.2f}".format(q_money(value)))


def fmt_qty(value):
    # type: (Decimal) -> str
    q = q_qty(value)
    if q == q.to_integral_value():
        return "{:,.0f}".format(q)
    return "{:,.3f}".format(q)
