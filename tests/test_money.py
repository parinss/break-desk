"""
test_money.py — parsing and arithmetic.

The most important assertions in this file are the ones about what the parsers
*refuse*. A reconciliation engine that reads an unparseable figure as zero
reports a flat position, and a flat position looks like a closed one. Silence is
the dangerous failure here, not a crash.
"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

import _util  # noqa: F401 - path bootstrap

import money
from money import (
    ParseError,
    fmt_money,
    fmt_qty,
    parse_eu_date,
    parse_eu_decimal,
    parse_iso_date,
    parse_us_date,
    parse_us_decimal,
    pct_diff,
    q_money,
    q_qty,
    q_rate,
)


class TestUsDecimal(unittest.TestCase):
    def test_thousands_and_decimals(self):
        self.assertEqual(parse_us_decimal("1,200.000"), Decimal("1200"))
        self.assertEqual(parse_us_decimal("214.35"), Decimal("214.35"))
        self.assertEqual(parse_us_decimal("310,570.00"), Decimal("310570"))

    def test_accounting_parentheses_are_negative(self):
        self.assertEqual(parse_us_decimal("(1,234.56)"), Decimal("-1234.56"))
        self.assertEqual(parse_us_decimal("(4,287.50)"), Decimal("-4287.50"))

    def test_leading_minus(self):
        self.assertEqual(parse_us_decimal("-42,870.00"), Decimal("-42870"))

    def test_currency_decoration_is_stripped(self):
        self.assertEqual(parse_us_decimal("USD 1,000.00"), Decimal("1000"))
        self.assertEqual(parse_us_decimal("$1,000.00"), Decimal("1000"))

    def test_no_precision_is_lost(self):
        # The whole reason this module exists. As floats these are 0.30000000000000004.
        total = parse_us_decimal("0.10") + parse_us_decimal("0.20")
        self.assertEqual(total, Decimal("0.30"))
        self.assertNotIsInstance(total, float)


class TestEuDecimal(unittest.TestCase):
    def test_swapped_separators(self):
        self.assertEqual(parse_eu_decimal("1.200,000"), Decimal("1200"))
        self.assertEqual(parse_eu_decimal("214,35"), Decimal("214.35"))
        self.assertEqual(parse_eu_decimal("1.208.479,27"), Decimal("1208479.27"))

    def test_swiss_apostrophe_grouping(self):
        self.assertEqual(parse_eu_decimal("1'234,50"), Decimal("1234.50"))

    def test_leading_minus(self):
        self.assertEqual(parse_eu_decimal("-25.265,00"), Decimal("-25265"))

    def test_same_value_from_both_locales(self):
        self.assertEqual(parse_us_decimal("1,300.000"), parse_eu_decimal("1.300,000"))


class TestParsersRefuse(unittest.TestCase):
    """A bad figure must stop the run, never become a zero."""

    def test_empty_and_none(self):
        for bad in (None, "", "   "):
            with self.assertRaises(ParseError):
                parse_us_decimal(bad)
            with self.assertRaises(ParseError):
                parse_eu_decimal(bad)

    def test_not_a_number(self):
        for bad in ("abc", "1.2.3", "12,34,56.7.8", "N/A", "--5"):
            with self.assertRaises(ParseError):
                parse_us_decimal(bad)

    def test_bare_sign_or_point(self):
        for bad in ("-", ".", "(-)"):
            with self.assertRaises(ParseError):
                parse_us_decimal(bad)

    def test_eu_parser_rejects_us_formatting(self):
        # "1,200.000" under EU rules is not a well-formed number, and reading it
        # as 1.2 would be far worse than refusing it.
        with self.assertRaises(ParseError):
            parse_eu_decimal("1,200.000")


class TestDates(unittest.TestCase):
    def test_three_formats_from_three_sources(self):
        self.assertEqual(parse_us_date("06/12/2026"), date(2026, 6, 12))
        self.assertEqual(parse_eu_date("30.06.2026"), date(2026, 6, 30))
        self.assertEqual(parse_iso_date("2026-05-18"), date(2026, 5, 18))

    def test_day_month_order_is_not_guessed(self):
        # 06/12 is 12 June in the US file and 6 December in the EU one. Both
        # parse; a single lenient parser would silently pick one.
        self.assertEqual(parse_us_date("06/12/2026"), date(2026, 6, 12))
        self.assertEqual(parse_eu_date("06.12.2026"), date(2026, 12, 6))

    def test_malformed_dates_raise(self):
        for bad in (None, "", "2026-13-01", "31/02/2026", "not a date"):
            with self.assertRaises(ParseError):
                parse_iso_date(bad)


class TestQuantisation(unittest.TestCase):
    def test_money_is_two_places_half_up(self):
        self.assertEqual(q_money(Decimal("1.005")), Decimal("1.01"))
        self.assertEqual(q_money(Decimal("1.004")), Decimal("1.00"))
        self.assertEqual(q_money(Decimal("-1.005")), Decimal("-1.01"))

    def test_quantities_carry_three_places(self):
        self.assertEqual(q_qty(Decimal("1200")), Decimal("1200.000"))
        self.assertEqual(q_qty(Decimal("0.0005")), Decimal("0.001"))

    def test_rates_carry_six(self):
        self.assertEqual(q_rate(Decimal("0.917")), Decimal("0.917000"))
        self.assertEqual(q_rate(Decimal("0.8939999")), Decimal("0.894000"))


class TestPctDiff(unittest.TestCase):
    def test_ordinary_case(self):
        self.assertEqual(pct_diff(Decimal("110"), Decimal("100")), Decimal("10"))
        self.assertEqual(pct_diff(Decimal("90"), Decimal("100")), Decimal("-10"))

    def test_zero_expected_returns_none_not_zero(self):
        # None is the third state: "no meaningful comparison". Zero would read
        # as agreement and infinity as catastrophe; both would be inventions.
        self.assertIsNone(pct_diff(Decimal("5"), Decimal("0")))

    def test_negative_expected_uses_magnitude(self):
        self.assertEqual(pct_diff(Decimal("-90"), Decimal("-100")), Decimal("10"))


class TestFormatting(unittest.TestCase):
    def test_money(self):
        self.assertEqual(fmt_money(Decimal("1234.5"), "USD"), "USD 1,234.50")
        self.assertEqual(fmt_money(Decimal("-4287.5"), "EUR"), "EUR -4,287.50")

    def test_whole_quantities_lose_the_decimals(self):
        self.assertEqual(fmt_qty(Decimal("1200")), "1,200")
        self.assertEqual(fmt_qty(Decimal("2400.000")), "2,400")

    def test_fractional_quantities_keep_three(self):
        self.assertEqual(fmt_qty(Decimal("1200.5")), "1,200.500")

    def test_round_trip_through_the_us_parser(self):
        original = Decimal("1234567.89")
        self.assertEqual(parse_us_decimal(fmt_money(original, "USD")), original)


class TestSwiftNumbers(unittest.TestCase):
    """
    ISO 15022 field format: comma decimal, no thousands separator, and the comma
    is mandatory. The strictness is the feature — see the last test in the class.
    """

    def test_whole_numbers_carry_a_trailing_comma(self):
        self.assertEqual(money.parse_swift_decimal("1800,"), Decimal("1800"))
        self.assertEqual(money.parse_swift_decimal("0,"), Decimal("0"))

    def test_fractions(self):
        self.assertEqual(money.parse_swift_decimal("238,90"), Decimal("238.90"))
        self.assertEqual(money.parse_swift_decimal("0,001"), Decimal("0.001"))

    def test_a_missing_comma_is_refused(self):
        """`1800` is not a well-formed SWIFT amount, and accepting it would mean
        accepting output from something that does not know the format."""
        self.assertRaises(ParseError, money.parse_swift_decimal, "1800")

    def test_thousands_separators_are_refused(self):
        for raw in ("1.800,00", "1,800.00", "1 800,00"):
            self.assertRaises(ParseError, money.parse_swift_decimal, raw)

    def test_signs_and_blanks_are_refused(self):
        for raw in ("-1800,", "", "   ", None, "USD1800,", "1800,,"):
            self.assertRaises(ParseError, money.parse_swift_decimal, raw)

    def test_a_comma_is_a_decimal_point_and_this_is_the_trap(self):
        """
        `1,800` is one point eight. It is *not* one thousand eight hundred.

        This is the single most dangerous string in the format, because it is
        also a perfectly ordinary US number meaning something 1,000x larger. A
        parser that "helpfully" accepted both conventions would read a serialiser
        bug as a position, and the two readings are three orders of magnitude
        apart. There is no tolerance that catches that; only refusing to guess
        does.
        """
        self.assertEqual(money.parse_swift_decimal("1,800"), Decimal("1.800"))
        self.assertNotEqual(money.parse_swift_decimal("1,800"), Decimal("1800"))


class TestSwiftDates(unittest.TestCase):
    def test_yyyymmdd(self):
        self.assertEqual(money.parse_swift_date("20260630"), date(2026, 6, 30))

    def test_separators_are_refused(self):
        for raw in ("2026-06-30", "30.06.2026", "06/30/2026", "260630"):
            self.assertRaises(ParseError, money.parse_swift_date, raw)


class TestConstants(unittest.TestCase):
    def test_precision_constants_are_decimals(self):
        for const in (money.QTY_DP, money.MONEY_DP, money.RATE_DP, money.ZERO):
            self.assertIsInstance(const, Decimal)


if __name__ == "__main__":
    unittest.main()
