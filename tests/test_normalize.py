"""
test_normalize.py — the boundary between two custodians' formats and one model.

Two assertions here matter more than the rest.

The first is provenance: every Position and Transaction claims a file and a line
number, and the whole report rests on those citations being literally true. So
this suite opens the files and checks that the cited line says what the citation
says it says. A provenance record that drifts by one line is worse than none —
it looks authoritative and sends the reviewer to the wrong row.

The second is sign convention. A sign error in a rollforward is indistinguishable
from a real break, and the merger legs are the case where no rule keyed on the
activity code alone can work it out.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from datetime import date
from decimal import Decimal

import _util
from _util import AAPL, META, MSFT, NVDA, SPCX, TSLA, VOD

import generate
import normalize
from money import ParseError
from normalize import (
    BHP,
    MERIDIAN,
    load_period,
    parse_bhp_positions,
    parse_bhp_transactions,
    parse_corporate_actions,
    parse_meridian_positions,
    parse_meridian_transactions,
)


class StatementsFixture(unittest.TestCase):
    """Generates a fresh set of statements into a temp directory per class."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="break-desk-normalize-")
        generate.write_all(cls.dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.dir, name)

    def line_of(self, filename, lineno):
        with io.open(self.path(filename), encoding="utf-8") as fh:
            return fh.read().splitlines()[lineno - 1]


class TestProvenanceIsLiterallyTrue(StatementsFixture):
    def _check(self, records):
        self.assertTrue(records)
        for rec in records:
            src = rec.source
            actual = self.line_of(src.file, src.line)
            if src.excerpt.endswith("..."):
                self.assertTrue(actual.startswith(src.excerpt[:-3]), src.cite())
            else:
                self.assertEqual(actual.rstrip("\n"), src.excerpt, src.cite())

    def test_meridian_positions(self):
        self._check(parse_meridian_positions(
            self.path("meridian_positions_2026-06-30.csv")).positions)

    def test_meridian_transactions(self):
        self._check(parse_meridian_transactions(
            self.path("meridian_transactions_2026Q2.csv")))

    def test_bhp_positions(self):
        snap = parse_bhp_positions(self.path("bhp_vermoegensausweis_2026-06-30.txt"))
        self._check(snap.positions)
        self._check(snap.fx_rates)

    def test_bhp_transactions(self):
        self._check(parse_bhp_transactions(self.path("bhp_bewegungen_2026Q2.txt")))

    def test_corporate_actions(self):
        self._check(parse_corporate_actions(self.path("corporate_actions_2026Q2.csv")))

    def test_line_numbers_are_one_indexed_as_a_human_counts(self):
        snap = parse_meridian_positions(self.path("meridian_positions_2026-06-30.csv"))
        first = snap.positions[0]
        self.assertGreater(first.source.line, 1, "the header is not line zero")


class TestMeridianPositions(StatementsFixture):
    def setUp(self):
        self.snap = parse_meridian_positions(
            self.path("meridian_positions_2026-06-30.csv"))
        self.by = self.snap.by_isin()

    def test_header_fields(self):
        self.assertEqual(self.snap.as_of, date(2026, 6, 30))
        self.assertEqual(self.snap.custodian, MERIDIAN)
        self.assertEqual(self.snap.account, "PWM-4471")
        self.assertEqual(self.snap.base_ccy, "USD")

    def test_us_locale_figures(self):
        aapl = self.by[AAPL]
        self.assertEqual(aapl.quantity, Decimal("1300"))
        self.assertEqual(aapl.price, Decimal("238.90"))
        self.assertEqual(aapl.market_value, Decimal("310570.00"))
        self.assertEqual(aapl.cost_basis, Decimal("217416.00"))

    def test_a_stale_ticker_still_resolves_to_the_right_instrument(self):
        """
        The statement prints FB. Refusing to resolve it would turn cosmetic
        staleness into a phantom break; resolving it silently would hide a real
        finding. It resolves, and the printed ticker is kept verbatim so the
        identifier rule can report on it.
        """
        self.assertIn(META, self.by)
        self.assertEqual(self.by[META].reported_symbol, "FB")

    def test_no_base_value_column_on_a_usd_statement(self):
        self.assertIsNone(self.by[AAPL].reported_base_value)


class TestMeridianTransactions(StatementsFixture):
    def setUp(self):
        self.txns = parse_meridian_transactions(
            self.path("meridian_transactions_2026Q2.csv"))

    def _one(self, isin, kind):
        matches = [t for t in self.txns if t.isin == isin and t.kind == kind]
        self.assertEqual(len(matches), 1, "%s %s" % (isin, kind))
        return matches[0]

    def test_a_buy_is_a_positive_delta_and_a_cash_outflow(self):
        buy = self._one(AAPL, "BUY")
        self.assertEqual(buy.quantity, Decimal("300"))
        self.assertEqual(buy.amount, Decimal("-69420.00"))

    def test_a_sale_is_negated_into_a_signed_delta(self):
        """Both statements print a sale as a positive 200. Both are negated."""
        sell = self._one(AAPL, "SELL")
        self.assertEqual(sell.quantity, Decimal("-200"))
        self.assertEqual(sell.amount, Decimal("47860.00"))

    def test_a_split_books_the_delta(self):
        split = self._one(NVDA, "SPLIT")
        self.assertEqual(split.quantity, Decimal("2400.000"))

    def test_a_merger_leg_keeps_the_sign_the_statement_printed(self):
        """
        Accounting parentheses on the outgoing leg. No rule keyed on the activity
        code can tell the two legs of a merger apart, so the parser trusts the
        sign in the source rather than guessing which leg it is looking at.
        """
        leg = self._one(SPCX, "MERGER")
        self.assertEqual(leg.quantity, Decimal("-1000.000"))

    def test_dividends_and_fees_move_no_shares(self):
        for t in self.txns:
            if t.kind in ("DIV", "FEE"):
                self.assertEqual(t.quantity, Decimal("0"))

    def test_an_account_level_line_has_no_instrument(self):
        fees = [t for t in self.txns if t.kind == "FEE"]
        for f in fees:
            self.assertEqual(f.isin, "")

    def test_the_quarter_with_no_fee_really_has_none(self):
        q2 = [t for t in self.txns if t.kind == "FEE"]
        q1 = [t for t in parse_meridian_transactions(
            self.path("meridian_transactions_2026Q1.csv")) if t.kind == "FEE"]
        self.assertEqual(q2, [])
        self.assertEqual(len(q1), 1)


class TestBhpPositions(StatementsFixture):
    def setUp(self):
        self.snap = parse_bhp_positions(
            self.path("bhp_vermoegensausweis_2026-06-30.txt"))
        self.by = self.snap.by_isin()

    def test_header_fields(self):
        self.assertEqual(self.snap.as_of, date(2026, 6, 30))
        self.assertEqual(self.snap.custodian, BHP)
        self.assertEqual(self.snap.base_ccy, "EUR")

    def test_eu_locale_figures(self):
        aapl = self.by[AAPL]
        self.assertEqual(aapl.quantity, Decimal("1300"))
        self.assertEqual(aapl.price, Decimal("238.90"))
        self.assertEqual(aapl.market_value, Decimal("310570.00"))

    def test_unreported_cost_basis_is_none_not_zero(self):
        for p in self.snap.positions:
            self.assertIsNone(p.cost_basis)

    def test_base_currency_column_is_captured(self):
        self.assertEqual(self.by[AAPL].reported_base_value, Decimal("277649.58"))

    def test_fx_rates_are_read_off_the_document(self):
        rates = dict((r.pair, r.rate) for r in self.snap.fx_rates)
        self.assertEqual(rates["USD/EUR"], Decimal("0.9170"))

    def test_the_same_holding_parses_identically_at_both_custodians(self):
        meridian = parse_meridian_positions(
            self.path("meridian_positions_2026-06-30.csv")).by_isin()
        self.assertEqual(self.by[AAPL].quantity, meridian[AAPL].quantity)
        self.assertEqual(self.by[AAPL].price, meridian[AAPL].price)


class TestBhpTransactions(StatementsFixture):
    def setUp(self):
        self.txns = parse_bhp_transactions(self.path("bhp_bewegungen_2026Q2.txt"))

    def test_german_vocabulary_maps_to_canonical_kinds(self):
        got = set(t.kind for t in self.txns)
        self.assertTrue({"BUY", "SELL", "SPLIT", "MERGER", "DIV", "FEE"} <= got)

    def test_verkauf_is_negated(self):
        sell = [t for t in self.txns if t.kind == "SELL"][0]
        self.assertEqual(sell.quantity, Decimal("-200"))

    def test_both_merger_legs_carry_their_own_sign(self):
        legs = dict((t.isin, t.quantity) for t in self.txns if t.kind == "MERGER")
        self.assertEqual(legs[SPCX], Decimal("-1000"))
        self.assertEqual(legs[TSLA], Decimal("2000"))

    def test_eu_dates(self):
        self.assertIn(date(2026, 6, 12), [t.trade_date for t in self.txns])

    def test_this_custodian_never_booked_the_nvidia_split(self):
        self.assertEqual([t for t in self.txns if t.isin == NVDA], [])


class TestCorporateActionFeed(StatementsFixture):
    def setUp(self):
        self.actions = parse_corporate_actions(
            self.path("corporate_actions_2026Q2.csv"))
        self.by = dict((a.isin, a) for a in self.actions)

    def test_iso_dates_a_third_format(self):
        self.assertEqual(self.by[NVDA].ex_date, date(2026, 5, 18))

    def test_split_ratios(self):
        self.assertEqual((self.by[NVDA].ratio_num, self.by[NVDA].ratio_den), (4, 1))
        self.assertEqual((self.by[MSFT].ratio_num, self.by[MSFT].ratio_den), (3, 2))

    def test_a_name_change_carries_both_tickers_and_moves_no_shares(self):
        act = self.by[META]
        self.assertEqual(act.kind, "NAME_CHANGE")
        self.assertEqual((act.old_symbol, act.new_symbol), ("FB", "META"))
        self.assertFalse(act.affects_quantity())

    def test_a_merger_names_both_instruments(self):
        act = self.by[SPCX]
        self.assertEqual(act.kind, "MERGER")
        self.assertEqual(act.related_isin, TSLA)
        self.assertTrue(act.is_two_legged())
        self.assertEqual(sorted(act.touches()), sorted([SPCX, TSLA]))

    def test_comment_lines_are_skipped(self):
        self.assertEqual(len(self.actions), 4)


class TestParsersRefuseBadInput(unittest.TestCase):
    """
    An unidentifiable holding cannot be reconciled, and a report that quietly
    omits a position is worse than no report — the reviewer has no way to know
    it is incomplete. Every one of these raises rather than dropping a line.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="break-desk-bad-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_unmapped_symbol_in_a_position_file(self):
        path = self.write("p.csv", "\n".join([
            "As Of: 06/30/2026",
            "Account: PWM-4471",
            "Symbol,CUSIP,Name,Quantity,Price,MarketValue,CostBasis,Currency",
            "WIDGET,999999999,Widget Co,100.000,10.00,1000.00,900.00,USD",
        ]) + "\n")
        with self.assertRaises(ParseError) as ctx:
            parse_meridian_positions(path)
        self.assertIn("unmapped instrument", str(ctx.exception))

    def test_short_row_in_a_position_file(self):
        path = self.write("p.csv", "\n".join([
            "As Of: 06/30/2026",
            "Account: PWM-4471",
            "Symbol,CUSIP,Name,Quantity,Price,MarketValue,CostBasis,Currency",
            "AAPL,037833100,Apple Inc.,100.000",
        ]) + "\n")
        with self.assertRaises(ParseError):
            parse_meridian_positions(path)

    def test_missing_header_value(self):
        path = self.write("p.csv", "\n".join([
            "As Of: 06/30/2026",
            "Symbol,CUSIP,Name,Quantity,Price,MarketValue,CostBasis,Currency",
        ]) + "\n")
        with self.assertRaises(ParseError) as ctx:
            parse_meridian_positions(path)
        self.assertIn("Account", str(ctx.exception))

    def test_unknown_activity_code(self):
        path = self.write("t.csv", "\n".join([
            "Account: PWM-4471",
            "Trade Date,Settle Date,Symbol,CUSIP,Activity,Quantity,Price,Amount,Currency,Description",
            "05/12/2026,05/14/2026,AAPL,037833100,TELEPORT,300.000,231.40,(69420.00),USD,x",
        ]) + "\n")
        with self.assertRaises(ParseError) as ctx:
            parse_meridian_transactions(path)
        self.assertIn("unknown activity", str(ctx.exception))

    def test_wrong_column_count_in_the_space_delimited_file(self):
        """
        Columns held apart by runs of spaces are exactly as fragile as they
        sound, so the field count is verified on every line and the file is
        refused rather than a column quietly mis-assigned.
        """
        path = self.write("b.txt", "\n".join([
            "Stichtag / Date: 30.06.2026",
            "Konto / Compte: PWM-4471",
            "ISIN           Bezeichnung      Anzahl    Kurs   Whg   Marktwert   EUR",
            "US0378331005   APPLE INC        1.300,000    238,90   USD",
        ]) + "\n")
        with self.assertRaises(ParseError) as ctx:
            parse_bhp_positions(path)
        self.assertIn("7 space-delimited columns", str(ctx.exception))

    def test_a_merger_with_one_leg_is_not_interpretable(self):
        path = self.write("ca.csv", "\n".join([
            "ISIN,Symbol,ExDate,Type,RatioNew,RatioOld,RelatedISIN,OldSymbol,NewSymbol,Description",
            "US00SPACEX19,SPCX,2026-06-12,MERGER,2,1,,,,acquired",
        ]) + "\n")
        with self.assertRaises(ParseError) as ctx:
            parse_corporate_actions(path)
        self.assertIn("two-legged action with one leg", str(ctx.exception))

    def test_a_sub_unity_split_is_reclassified_as_a_reverse_split(self):
        path = self.write("ca.csv", "\n".join([
            "ISIN,Symbol,ExDate,Type,RatioNew,RatioOld,RelatedISIN,OldSymbol,NewSymbol,Description",
            "US92857W3088,VOD,2026-05-01,SPLIT,1,10,,,,one for ten",
        ]) + "\n")
        act = parse_corporate_actions(path)[0]
        self.assertEqual(act.kind, "REVERSE_SPLIT")
        self.assertEqual(act.multiplier(), Decimal("0.1"))

    def test_a_non_positive_ratio_is_refused(self):
        path = self.write("ca.csv", "\n".join([
            "ISIN,Symbol,ExDate,Type,RatioNew,RatioOld,RelatedISIN,OldSymbol,NewSymbol,Description",
            "US67066G1040,NVDA,2026-05-18,SPLIT,4,0,,,,divide by zero",
        ]) + "\n")
        with self.assertRaises(ParseError):
            parse_corporate_actions(path)


class TestLoadPeriod(StatementsFixture):
    def setUp(self):
        self.period = load_period(self.dir)

    def test_shape(self):
        self.assertEqual(
            sorted(self.period),
            ["actions", "current", "prior", "txns_current", "txns_prior"],
        )

    def test_both_custodians_on_both_dates(self):
        for side in ("prior", "current"):
            self.assertEqual(sorted(self.period[side]), sorted([BHP, MERIDIAN]))

    def test_downstream_never_needs_to_know_the_source_format(self):
        """The contract of this module: two locales in, one model out."""
        for snapshot in list(self.period["prior"].values()) + list(self.period["current"].values()):
            for p in snapshot.positions:
                self.assertIsInstance(p.quantity, Decimal)
                self.assertIsInstance(p.price, Decimal)
                self.assertTrue(normalize.ISIN_RE.match(p.isin))


if __name__ == "__main__":
    unittest.main()
