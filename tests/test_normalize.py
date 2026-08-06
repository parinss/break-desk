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
    NORTHGATE,
    load_period,
    parse_bhp_positions,
    parse_bhp_transactions,
    parse_corporate_actions,
    parse_meridian_positions,
    parse_meridian_transactions,
    parse_mt535,
    parse_mt536,
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

    def test_every_custodian_on_both_dates(self):
        for side in ("prior", "current"):
            self.assertEqual(
                sorted(self.period[side]), sorted([BHP, MERIDIAN, NORTHGATE])
            )

    def test_downstream_never_needs_to_know_the_source_format(self):
        """The contract of this module: three locales in, one model out."""
        for snapshot in list(self.period["prior"].values()) + list(self.period["current"].values()):
            for p in snapshot.positions:
                self.assertIsInstance(p.quantity, Decimal)
                self.assertIsInstance(p.price, Decimal)
                self.assertTrue(normalize.ISIN_RE.match(p.isin))


# --- ISO 15022 ---------------------------------------------------------------


MT535_MINIMAL = "\n".join([
    "{1:F01NRTGZZ00XXXX0000000000}",
    "{2:O5350915260630NRTGZZ00XXXX2606300915N}",
    "{4:",
    ":16R:GENL",
    ":20C::SEME//TEST535",
    ":23G:NEWM",
    ":98A::STAT//20260630",
    ":22F::STBA//TRAD",
    ":97A::SAFE//PWM-4471",
    ":17B::ACTI//Y",
    ":16S:GENL",
    ":16R:FIN",
    ":35B:ISIN US0378331005",
    "APPLE INC.",
    ":93B::AGGR//UNIT/1800,",
    ":16R:PRIC",
    ":90B::MRKT//ACTU/USD238,90",
    ":16S:PRIC",
    ":19A::HOLD//USD430020,",
    ":16S:FIN",
    "-}",
    "",
])


class SwiftFixture(unittest.TestCase):
    """Writes a message to a temp file so the parser is exercised through its
    real entry point — the one that opens a file and reports a line number."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="swift-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, text, name="mt535.txt"):
        # type: (str, str) -> str
        path = os.path.join(self.dir, name)
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def mutate(self, old, new, name="mt535.txt", source=MT535_MINIMAL):
        # type: (str, str, str, str) -> str
        self.assertIn(old, source, "fixture does not contain %r" % old)
        return self.write(source.replace(old, new, 1), name)


class TestMT535(SwiftFixture):
    def snapshot(self):
        return parse_mt535(self.write(MT535_MINIMAL))

    def test_header_fields(self):
        snap = self.snapshot()
        self.assertEqual(snap.custodian, NORTHGATE)
        self.assertEqual(snap.account, "PWM-4471")
        self.assertEqual(snap.as_of, date(2026, 6, 30))

    def test_the_statement_basis_is_read_not_assumed(self):
        self.assertEqual(self.snapshot().basis, "TRAD")

    def test_a_message_with_no_basis_is_settled(self):
        """
        The convention a statement is read under when it says nothing. Assuming
        trade date instead would invent an in-flight adjustment against every
        feed that simply omits the field.
        """
        path = self.mutate(":22F::STBA//TRAD\n", "")
        self.assertEqual(parse_mt535(path).basis, "SETT")

    def test_an_unknown_basis_is_refused(self):
        path = self.mutate(":22F::STBA//TRAD", ":22F::STBA//XXXX")
        self.assertRaises(ParseError, parse_mt535, path)

    def test_position_figures(self):
        p = self.snapshot().positions[0]
        self.assertEqual(p.isin, AAPL)
        self.assertEqual(p.quantity, Decimal("1800"))
        self.assertEqual(p.price, Decimal("238.90"))
        self.assertEqual(p.market_value, Decimal("430020"))
        self.assertEqual(p.ccy, "USD")

    def test_no_cost_basis_is_none_and_not_zero(self):
        """Same rule as BHP: a custodian that does not report basis must not be
        read as reporting a basis of zero."""
        self.assertIsNone(self.snapshot().positions[0].cost_basis)

    def test_there_is_no_ticker_to_be_stale(self):
        """Identification is by ISIN throughout, so the identifier rule has
        nothing to report — and must not invent something from the description
        line, which is free text and often carries a former name."""
        self.assertEqual(self.snapshot().positions[0].reported_symbol, "")

    def test_provenance_points_at_the_quantity_line(self):
        p = self.snapshot().positions[0]
        with io.open(os.path.join(self.dir, "mt535.txt"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines[p.source.line - 1], p.source.excerpt)
        self.assertIn("93B", p.source.excerpt)

    # --- the block grammar ---

    def test_an_unclosed_block_is_refused(self):
        """A dropped :16S: re-parents every block after it, which silently moves
        a holding into another instrument rather than failing."""
        path = self.mutate(":16S:PRIC\n", "")
        self.assertRaises(ParseError, parse_mt535, path)

    def test_a_mismatched_close_is_refused(self):
        path = self.mutate(":16S:PRIC", ":16S:SUBBAL")
        self.assertRaises(ParseError, parse_mt535, path)

    def test_a_close_with_nothing_open_is_refused(self):
        path = self.mutate(":16R:GENL\n", "")
        self.assertRaises(ParseError, parse_mt535, path)

    def test_a_continuation_line_with_no_field_is_refused(self):
        path = self.mutate(":16R:FIN\n", ":16R:FIN\nORPHAN TEXT\n")
        self.assertRaises(ParseError, parse_mt535, path)

    def test_a_missing_body_block_is_refused(self):
        path = self.write(MT535_MINIMAL.replace("{4:", "{5:"))
        self.assertRaises(ParseError, parse_mt535, path)

    def test_nested_blocks_do_not_leak_fields_to_the_parent(self):
        """
        :90B: lives inside PRIC. If block scoping were flat, a lookup on the FIN
        block would find it anyway and the nesting would be decorative.
        """
        root = normalize._swift_blocks(MT535_MINIMAL.splitlines(), "mt535.txt")
        fin = root.blocks("FIN")[0]
        self.assertIsNone(normalize._swift_field(fin, "90B", "MRKT", "x", required=False))
        self.assertIsNotNone(
            normalize._swift_field(fin.blocks("PRIC")[0], "90B", "MRKT", "x")
        )

    # --- fields that must not be guessed at ---

    def test_a_face_amount_is_not_a_share_count(self):
        """
        FAMT is a bond's notional. Reading it as units overstates the holding by
        orders of magnitude, and the denomination needed to convert it is not in
        this message — so it is refused rather than approximated.
        """
        path = self.mutate("UNIT/1800,", "FAMT/1800,")
        self.assertRaises(ParseError, parse_mt535, path)

    def test_a_percentage_price_is_not_an_amount(self):
        path = self.mutate("ACTU/USD238,90", "PRCT/USD238,90")
        self.assertRaises(ParseError, parse_mt535, path)

    def test_pricing_and_valuation_currencies_must_agree(self):
        path = self.mutate(":19A::HOLD//USD430020,", ":19A::HOLD//EUR430020,")
        self.assertRaises(ParseError, parse_mt535, path)

    def test_an_unmapped_isin_is_refused(self):
        path = self.mutate("US0378331005", "US9999999999")
        self.assertRaises(ParseError, parse_mt535, path)

    def test_a_malformed_isin_is_refused(self):
        path = self.mutate(":35B:ISIN US0378331005", ":35B:ISIN NOTANISIN")
        self.assertRaises(ParseError, parse_mt535, path)

    def test_a_35b_without_an_isin_is_refused(self):
        path = self.mutate(":35B:ISIN US0378331005", ":35B:/US/037833100")
        self.assertRaises(ParseError, parse_mt535, path)

    def test_a_missing_required_field_names_the_field(self):
        path = self.mutate(":98A::STAT//20260630\n", "")
        with self.assertRaises(ParseError) as caught:
            parse_mt535(path)
        self.assertIn("98A", str(caught.exception))
        self.assertIn("STAT", str(caught.exception))


MT536_HEAD = "\n".join([
    "{1:F01NRTGZZ00XXXX0000000000}",
    "{2:O5360915260630NRTGZZ00XXXX2606300915N}",
    "{4:",
    ":16R:GENL",
    ":20C::SEME//TEST536",
    ":23G:NEWM",
    ":98A::STAT//20260630",
    ":97A::SAFE//PWM-4471",
    ":17B::ACTI//%s",
    ":16S:GENL",
])

MT536_TRAN = "\n".join([
    ":16R:STAT",
    ":16R:FIN",
    ":35B:ISIN US0378331005",
    "APPLE INC.",
    ":16R:TRAN",
    ":22H::REDE//%s",
    ":22F::TRAN//%s",
    ":98A::TRAD//20260629",
    ":98A::SETT//20260702",
    ":36B::PSTA//UNIT/500,",
    ":19A::PSTA//USD119450,",
    ":16S:TRAN",
    ":16S:FIN",
    ":16S:STAT",
])


def mt536(rede="RECE", tran="TRAD", activity="Y", body=None):
    # type: (str, str, str, str) -> str
    parts = [MT536_HEAD % activity]
    if body is None and activity == "Y":
        body = MT536_TRAN % (rede, tran)
    if body:
        parts.append(body)
    parts.append("-}")
    return "\n".join(parts) + "\n"


class TestMT536(SwiftFixture):
    def parse(self, **kw):
        return parse_mt536(self.write(mt536(**kw), name="mt536.txt"))

    def test_a_receive_is_a_buy_and_the_cash_goes_the_other_way(self):
        """
        :36B: is always a magnitude — direction lives in :22H::REDE//. The
        canonical model wants a signed delta *and* a signed cash flow, and they
        point opposite ways: shares received cost money.
        """
        t = self.parse(rede="RECE")[0]
        self.assertEqual(t.kind, "BUY")
        self.assertEqual(t.quantity, Decimal("500"))
        self.assertEqual(t.amount, Decimal("-119450"))

    def test_a_deliver_is_a_sell(self):
        t = self.parse(rede="DELI")[0]
        self.assertEqual(t.kind, "SELL")
        self.assertEqual(t.quantity, Decimal("-500"))
        self.assertEqual(t.amount, Decimal("119450"))

    def test_settlement_date_is_carried_through(self):
        t = self.parse()[0]
        self.assertEqual(t.trade_date, date(2026, 6, 29))
        self.assertEqual(t.settle_date, date(2026, 7, 2))

    def test_in_flight_is_bounded_at_both_ends(self):
        t = self.parse()[0]
        self.assertTrue(t.in_flight_at(date(2026, 6, 30)))
        self.assertTrue(t.in_flight_at(date(2026, 6, 29)))   # traded, not settled
        self.assertFalse(t.in_flight_at(date(2026, 6, 28)))  # not yet traded
        self.assertFalse(t.in_flight_at(date(2026, 7, 2)))   # settled that day

    def test_a_corporate_action_movement_reads_its_event_code(self):
        path = self.write(
            mt536(body=(MT536_TRAN % ("RECE", "CORP")).replace(
                ":22F::TRAN//CORP", ":22F::TRAN//CORP\n:22F::CAEV//SPLF")),
            name="mt536.txt",
        )
        self.assertEqual(parse_mt536(path)[0].kind, "SPLIT")

    def test_a_corporate_action_without_an_event_code_is_refused(self):
        """CORP alone does not say whether shares arrived from a split or a
        merger allocation, and the two reconcile against different things."""
        self.assertRaises(ParseError, self.parse, tran="CORP")

    def test_an_unknown_direction_is_refused(self):
        self.assertRaises(ParseError, self.parse, rede="XXXX")

    # --- declared absence is not the same as absence ---

    def test_a_quarter_with_no_movements_says_so(self):
        self.assertEqual(self.parse(activity="N"), [])

    def test_declaring_no_activity_while_carrying_some_is_refused(self):
        """A truncated download must not read as a quiet quarter."""
        path = self.write(
            mt536(activity="N", body=MT536_TRAN % ("RECE", "TRAD")), name="mt536.txt"
        )
        self.assertRaises(ParseError, parse_mt536, path)

    def test_declaring_activity_while_carrying_none_is_refused(self):
        path = self.write(mt536(activity="Y", body=""), name="mt536.txt")
        self.assertRaises(ParseError, parse_mt536, path)


class TestThreeFormatsOneModel(StatementsFixture):
    """
    The claim normalize.py makes in its own docstring: adding a custodian means
    adding a parser and touching nothing else. Northgate is the third format and
    the second number convention change, so this is where that claim is tested
    rather than asserted.
    """

    def setUp(self):
        super(TestThreeFormatsOneModel, self).setUp()
        self.period = load_period(self.dir)

    def test_the_swift_custodian_lands_in_the_same_model(self):
        snap = self.period["current"][NORTHGATE]
        self.assertEqual(snap.basis, "TRAD")
        self.assertEqual(
            sorted(p.isin for p in snap.positions), sorted([AAPL, MSFT])
        )

    def test_only_the_swift_custodian_reports_a_trade_date_basis(self):
        bases = dict(
            (name, s.basis) for name, s in self.period["current"].items()
        )
        self.assertEqual(bases[NORTHGATE], "TRAD")
        self.assertEqual(bases[MERIDIAN], "SETT")
        self.assertEqual(bases[BHP], "SETT")

    def test_exactly_one_movement_is_in_flight_at_the_period_end(self):
        """The 500 AAPL traded on 29 June and settling on 2 July. This single
        row is why Northgate reports 1,800 and the others report 1,300."""
        as_of = self.period["current"][NORTHGATE].as_of
        flight = [
            t for t in self.period["txns_current"][NORTHGATE] if t.in_flight_at(as_of)
        ]
        self.assertEqual(len(flight), 1)
        self.assertEqual(flight[0].isin, AAPL)
        self.assertEqual(flight[0].quantity, Decimal("500"))

    def test_a_corporate_action_has_no_settlement_date(self):
        """
        A split moves shares without settling. Recording the trade date in that
        column would put a same-day settled movement in the file rather than
        nothing — harmless until a rule asks what is in flight.
        """
        splits = [
            t for t in self.period["txns_current"][MERIDIAN] if t.kind == "SPLIT"
        ]
        self.assertTrue(splits)
        for t in splits:
            self.assertIsNone(t.settle_date)


if __name__ == "__main__":
    unittest.main()
