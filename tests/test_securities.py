"""
test_securities.py — the crosswalk that makes cross-custodian work at all.

One custodian keys on symbol and CUSIP, the other on ISIN. Everything in the
report that compares two custodians depends on those three keys landing on the
same instrument, and on the resolver being right about which key to trust when
they disagree.
"""

from __future__ import annotations

import unittest

import _util
from _util import AAPL, META, NVDA

import securities


class TestResolution(unittest.TestCase):
    def test_by_symbol(self):
        self.assertEqual(securities.isin_for(symbol="AAPL"), AAPL)

    def test_by_cusip(self):
        self.assertEqual(securities.isin_for(cusip="037833100"), AAPL)

    def test_cusip_wins_when_the_two_disagree(self):
        """
        Tickers are recycled between issuers and across venues; CUSIPs are not.
        When a statement's ticker and CUSIP point at different instruments, the
        CUSIP is the one that has not been reused.
        """
        self.assertEqual(securities.isin_for(symbol="AAPL", cusip="67066G104"), NVDA)

    def test_case_and_whitespace_are_not_a_different_instrument(self):
        self.assertEqual(securities.isin_for(symbol="  aapl "), AAPL)

    def test_an_unknown_key_resolves_to_none_rather_than_raising(self):
        """The caller decides whether an unmappable holding is fatal. Here it is
        — normalize.py raises — but that is the pipeline's policy, not this
        module's."""
        self.assertIsNone(securities.isin_for(symbol="WIDGET"))
        self.assertIsNone(securities.isin_for(cusip="999999999"))
        self.assertIsNone(securities.isin_for())


class TestFormerSymbols(unittest.TestCase):
    def test_a_superseded_ticker_still_resolves(self):
        """
        A statement built from a stale security master says FB where it means
        META. The position is still Meta Platforms and must reconcile as such —
        refusing to resolve it turns cosmetic staleness into a phantom break.
        """
        self.assertEqual(securities.isin_for(symbol="FB"), META)

    def test_former_symbols_are_tried_last(self):
        current = [s.symbol for s in securities.ALL]
        self.assertNotIn("FB", current)
        self.assertEqual(securities.isin_for(symbol="META"), META)

    def test_the_staleness_is_still_reportable(self):
        """Resolving it silently would hide a real finding, so the fact that the
        ticker is superseded is available separately."""
        self.assertTrue(securities.is_former_symbol("FB", META))
        self.assertFalse(securities.is_former_symbol("META", META))
        self.assertFalse(securities.is_former_symbol("", META))
        self.assertFalse(securities.is_former_symbol("FB", AAPL))


class TestLookups(unittest.TestCase):
    def test_current_symbol(self):
        self.assertEqual(securities.current_symbol(META), "META")
        self.assertEqual(securities.current_symbol("XX0000000000"), "")

    def test_name_falls_back_to_the_identifier(self):
        """Better a bare ISIN in a report than a blank where a name should be."""
        self.assertEqual(securities.name_for(AAPL), "Apple Inc.")
        self.assertEqual(securities.name_for("XX0000000000"), "XX0000000000")

    def test_currency(self):
        self.assertEqual(securities.ccy_for(AAPL), "USD")
        self.assertEqual(securities.ccy_for("NL0010273215"), "EUR")
        self.assertIsNone(securities.ccy_for("XX0000000000"))

    def test_unmapped_surfaces_what_it_cannot_resolve(self):
        self.assertEqual(securities.unmapped(["AAPL", "WIDGET", AAPL]), ["WIDGET"])


class TestMasterIntegrity(unittest.TestCase):
    def test_identifiers_are_unique(self):
        for index in (securities.BY_ISIN, securities.BY_SYMBOL, securities.BY_CUSIP):
            self.assertEqual(len(index), len(securities.ALL))

    def test_every_entry_has_a_cusip(self):
        for sec in securities.ALL:
            self.assertIn(sec.isin, securities.CUSIP_BY_ISIN)
            self.assertEqual(len(securities.CUSIP_BY_ISIN[sec.isin]), 9)

    def test_isins_are_well_formed(self):
        for sec in securities.ALL:
            self.assertTrue(securities.ISIN_RE.match(sec.isin), sec.isin)


if __name__ == "__main__":
    unittest.main()
