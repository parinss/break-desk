"""
test_breaks.py — one class per rule, and every rule tested twice.

Once that it fires on the condition it names. Once that it stays quiet on the
neighbouring case that merely resembles it. The second half is the half that
decides whether a reconciliation desk survives contact with an operations team:
a rule that flags the corporate action it was supposed to explain away gets the
whole system switched off inside a fortnight, and the real break sails through
in week three.
"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

import _util
from _util import (
    AAPL,
    ACCOUNT,
    ASML,
    BASIS_SETTLED,
    BASIS_TRADE,
    CURRENT,
    CUST_A,
    CUST_B,
    META,
    MID,
    MSFT,
    NVDA,
    PRIOR,
    SPCX,
    TSLA,
    VOD,
    action,
    fx,
    has_break,
    pos,
    prov,
    snap,
    txn,
)

import breaks
from breaks import (
    RULES,
    detect_all,
    detect_basis_mismatch,
    detect_corp_actions,
    detect_cost_basis_drift,
    detect_cross_custodian_qty,
    detect_fx_inconsistency,
    detect_identifier_stale,
    detect_mergers,
    detect_missing_fee_accrual,
    detect_position_disappeared,
    detect_price_divergence,
    detect_qty_rollforward,
    in_flight_qty,
    roll_cost_basis,
    severity_for,
)


class TestSeverity(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(severity_for(Decimal("100000")), "critical")
        self.assertEqual(severity_for(Decimal("99999.99")), "high")
        self.assertEqual(severity_for(Decimal("5000")), "high")
        self.assertEqual(severity_for(Decimal("4999.99")), "medium")
        self.assertEqual(severity_for(Decimal("500")), "medium")
        self.assertEqual(severity_for(Decimal("499.99")), "low")
        self.assertEqual(severity_for(Decimal("0")), "low")

    def test_direction_does_not_change_materiality(self):
        self.assertEqual(severity_for(Decimal("-250000")), "critical")

    def test_cap_lowers_but_never_raises(self):
        self.assertEqual(breaks._capped_severity(Decimal("900000"), "medium"), "medium")
        self.assertEqual(breaks._capped_severity(Decimal("100"), "medium"), "low")

    def test_severity_comes_from_exposure_not_from_the_rule(self):
        """The same rule on a large and a small position must not rank alike."""
        big = detect_qty_rollforward(
            snap([pos(VOD, "5000", price="11.42")], as_of=PRIOR),
            snap([pos(VOD, "0", price="11.42")]),
            [],
        )
        small = detect_qty_rollforward(
            snap([pos(VOD, "10", price="11.42")], as_of=PRIOR),
            snap([pos(VOD, "0", price="11.42")]),
            [],
        )
        self.assertEqual(big[0].kind, small[0].kind)
        self.assertNotEqual(big[0].severity, small[0].severity)


class TestQtyRollforward(unittest.TestCase):
    def test_fires_on_an_unexplained_movement(self):
        found = detect_qty_rollforward(
            snap([pos(VOD, "5000", price="11.42")], as_of=PRIOR),
            snap([pos(VOD, "4400", price="12.05")]),
            [],
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "QTY_ROLLFORWARD")
        self.assertEqual(found[0].detail["unexplained_quantity"], "-600")
        self.assertEqual(found[0].value_at_risk, Decimal("7230.00"))

    def test_silent_when_the_activity_file_explains_it(self):
        found = detect_qty_rollforward(
            snap([pos(AAPL, "1200")], as_of=PRIOR),
            snap([pos(AAPL, "1300")]),
            [txn(AAPL, "BUY", qty="300", amount="-69420"),
             txn(AAPL, "SELL", qty="-200", amount="47860")],
        )
        self.assertEqual(found, [])

    def test_silent_when_a_split_accounts_for_it(self):
        found = detect_qty_rollforward(
            snap([pos(NVDA, "800")], as_of=PRIOR),
            snap([pos(NVDA, "3200")]),
            [txn(NVDA, "SPLIT", qty="2400")],
        )
        self.assertEqual(found, [])

    def test_positions_opened_in_the_window_are_not_breaks(self):
        """No prior line means nothing to roll forward from. Treating an absent
        prior as zero would report every new purchase as a finding."""
        found = detect_qty_rollforward(
            snap([], as_of=PRIOR),
            snap([pos(ASML, "300", ccy="EUR")]),
            [txn(ASML, "BUY", qty="300", amount="-274680", ccy="EUR")],
        )
        self.assertEqual(found, [])

    def test_carries_provenance_for_every_figure_it_used(self):
        movement = txn(AAPL, "BUY", qty="300", amount="-69420",
                       source=prov(file="activity.csv", line=7))
        found = detect_qty_rollforward(
            snap([pos(AAPL, "1200", source=prov(file="prior.csv", line=3))], as_of=PRIOR),
            snap([pos(AAPL, "1600", source=prov(file="current.csv", line=3))]),
            [movement],
        )
        cites = set(c.cite() for c in found[0].citations)
        self.assertEqual(cites, {"prior.csv:3", "current.csv:3", "activity.csv:7"})


class TestPositionDisappeared(unittest.TestCase):
    def test_fires_when_nothing_accounts_for_the_holding(self):
        found = detect_position_disappeared(
            snap([pos(VOD, "5000", price="11.42")], as_of=PRIOR),
            snap([]),
            [],
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "POSITION_DISAPPEARED")

    def test_silent_when_a_merger_removed_it(self):
        """
        The demo's merged-out target is exactly this shape at both custodians
        and is entirely correct. A rule that said "a position vanished" rather
        than "a position vanished and the activity file does not account for it"
        turns every corporate action into a false critical.
        """
        found = detect_position_disappeared(
            snap([pos(SPCX, "1000", price="145.80")], as_of=PRIOR),
            snap([]),
            [txn(SPCX, "MERGER", qty="-1000")],
        )
        self.assertEqual(found, [])

    def test_silent_when_it_was_simply_sold(self):
        found = detect_position_disappeared(
            snap([pos(AAPL, "200")], as_of=PRIOR),
            snap([]),
            [txn(AAPL, "SELL", qty="-200", amount="47860")],
        )
        self.assertEqual(found, [])

    def test_a_partial_disposal_leaves_a_residual(self):
        found = detect_position_disappeared(
            snap([pos(AAPL, "200", price="238.90")], as_of=PRIOR),
            snap([]),
            [txn(AAPL, "SELL", qty="-150", amount="35835")],
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].detail["unexplained_quantity"], "50")


class TestCorporateActions(unittest.TestCase):
    def test_fires_on_an_unapplied_split(self):
        found = detect_corp_actions(
            snap([pos(NVDA, "800", price="452.60")], as_of=PRIOR),
            snap([pos(NVDA, "800", price="118.90")]),
            [],
            [action(NVDA, num=4, den=1)],
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "CORP_ACTION_UNAPPLIED")
        self.assertEqual(found[0].detail["unapplied_quantity"], "2,400")
        self.assertEqual(found[0].value_at_risk, Decimal("285360.00"))
        self.assertEqual(found[0].severity, "critical")

    def test_silent_on_an_action_applied_correctly(self):
        """
        A 3-for-2 handled properly, with a trade in the same window. This is the
        negative case a "share count moved near a corporate action" heuristic
        cannot pass, and the reason detection is driven off a reference feed.
        """
        found = detect_corp_actions(
            snap([pos(MSFT, "450")], as_of=PRIOR),
            snap([pos(MSFT, "750")]),
            [txn(MSFT, "BUY", qty="50", amount="-25265"),
             txn(MSFT, "SPLIT", qty="250", trade_date=date(2026, 6, 2))],
            [action(MSFT, num=3, den=2, ex_date=date(2026, 6, 2))],
        )
        self.assertEqual(found, [])

    def test_fires_on_a_wrong_ratio(self):
        found = detect_corp_actions(
            snap([pos(NVDA, "100", price="100")], as_of=PRIOR),
            snap([pos(NVDA, "250", price="100")]),
            [],
            [action(NVDA, num=2, den=1)],
        )
        self.assertEqual(found[0].kind, "CORP_ACTION_WRONG_RATIO")
        self.assertEqual(found[0].detail["implied_ratio"], "5-for-2")

    def test_fires_on_a_corrupted_basis(self):
        found = detect_corp_actions(
            snap([pos(NVDA, "100", cost_basis="1000")], as_of=PRIOR),
            snap([pos(NVDA, "200", cost_basis="2000")]),
            [],
            [action(NVDA, num=2, den=1)],
        )
        self.assertEqual(found[0].kind, "CORP_ACTION_BASIS_CORRUPTED")
        self.assertEqual(found[0].value_at_risk, Decimal("1000.00"))

    def test_ignores_actions_outside_the_window(self):
        found = detect_corp_actions(
            snap([pos(NVDA, "800")], as_of=PRIOR),
            snap([pos(NVDA, "800")]),
            [],
            [action(NVDA, num=4, den=1, ex_date=date(2026, 8, 1))],
        )
        self.assertEqual(found, [])

    def test_leaves_two_legged_and_rename_actions_to_their_own_detectors(self):
        merger = action(SPCX, kind="MERGER", num=2, den=1, related=TSLA)
        rename = action(META, kind="NAME_CHANGE", num=1, den=1,
                        old_symbol="FB", new_symbol="META")
        found = detect_corp_actions(
            snap([pos(SPCX, "1000"), pos(META, "600", reported_symbol="FB")], as_of=PRIOR),
            snap([pos(SPCX, "1000"), pos(META, "600", reported_symbol="FB")]),
            [],
            [merger, rename],
        )
        self.assertEqual(found, [])

    def test_a_security_not_held_here_is_not_a_break(self):
        found = detect_corp_actions(
            snap([pos(AAPL, "100")], as_of=PRIOR),
            snap([pos(AAPL, "100")]),
            [],
            [action(NVDA, num=4, den=1)],
        )
        self.assertEqual(found, [])


class TestMergers(unittest.TestCase):
    def setUp(self):
        self.act = action(SPCX, kind="MERGER", num=2, den=1, related=TSLA,
                          ex_date=date(2026, 6, 12))
        self.prior = snap([pos(SPCX, "1000", price="145.80"),
                           pos(TSLA, "400", price="268.50")], as_of=PRIOR)

    def test_fires_when_the_acquirer_leg_is_never_credited(self):
        found = detect_mergers(
            self.prior,
            snap([pos(TSLA, "400", price="271.30")]),
            [txn(SPCX, "MERGER", qty="-1000", trade_date=date(2026, 6, 12))],
            [self.act],
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "MERGER_UNPROCESSED")
        self.assertEqual(found[0].isin, TSLA, "the finding belongs to the acquirer")
        self.assertEqual(found[0].detail["shares_missing"], "2,000")
        self.assertEqual(found[0].value_at_risk, Decimal("542600.00"))
        self.assertEqual(found[0].severity, "critical")

    def test_silent_when_both_legs_are_processed(self):
        found = detect_mergers(
            self.prior,
            snap([pos(TSLA, "2400", price="271.30")]),
            [txn(SPCX, "MERGER", qty="-1000", trade_date=date(2026, 6, 12)),
             txn(TSLA, "MERGER", qty="2000", trade_date=date(2026, 6, 12))],
            [self.act],
        )
        self.assertEqual(found, [])

    def test_fires_when_the_target_is_never_removed(self):
        found = detect_mergers(
            self.prior,
            snap([pos(SPCX, "1000", price="145.80"), pos(TSLA, "2400", price="271.30")]),
            [],
            [self.act],
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].detail["leg_failed"], "target position never removed")

    def test_rollforward_cannot_see_the_case_this_rule_exists_for(self):
        """
        The point of the whole corporate-action path, asserted directly: with the
        target removed and no credit booked, the position file and the activity
        file agree with each other perfectly. Self-consistency checks pass. Only
        the external record supplies the missing expectation.
        """
        current = snap([pos(TSLA, "400", price="271.30")])
        txns = [txn(SPCX, "MERGER", qty="-1000", trade_date=date(2026, 6, 12))]
        self.assertEqual(detect_qty_rollforward(self.prior, current, txns), [])
        self.assertEqual(detect_position_disappeared(self.prior, current, txns), [])
        self.assertEqual(len(detect_mergers(self.prior, current, txns, [self.act])), 1)


class TestIdentifierStale(unittest.TestCase):
    def setUp(self):
        self.act = action(META, kind="NAME_CHANGE", num=1, den=1,
                          ex_date=date(2026, 5, 5), old_symbol="FB", new_symbol="META")

    def test_fires_on_a_superseded_ticker(self):
        found = detect_identifier_stale(
            snap([pos(META, "600", price="548.70", reported_symbol="FB")]), [self.act]
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "IDENTIFIER_STALE")
        self.assertEqual(found[0].detail["reported_symbol"], "FB")

    def test_silent_when_the_ticker_is_current(self):
        found = detect_identifier_stale(
            snap([pos(META, "600", reported_symbol="META")]), [self.act]
        )
        self.assertEqual(found, [])

    def test_severity_is_capped_however_large_the_position(self):
        """
        The exposure is the whole position, because a mis-keyed holding can drop
        out of a consolidated report. But nothing is misstated, and ranking a
        stale ticker above a genuinely missing half-million of stock is the
        fastest way to teach an operations team to ignore the queue.
        """
        found = detect_identifier_stale(
            snap([pos(META, "600", price="548.70", reported_symbol="FB")]), [self.act]
        )
        self.assertEqual(found[0].value_at_risk, Decimal("329220.00"))
        self.assertEqual(found[0].severity, "medium")

    def test_silent_before_the_effective_date(self):
        future = action(META, kind="NAME_CHANGE", num=1, den=1,
                        ex_date=date(2026, 9, 1), old_symbol="FB", new_symbol="META")
        found = detect_identifier_stale(
            snap([pos(META, "600", reported_symbol="FB")]), [future]
        )
        self.assertEqual(found, [])

    def test_the_position_itself_still_reconciles(self):
        """A stale ticker must not also produce a phantom quantity break — the
        line was matched on CUSIP, so the holding lines up either way."""
        stale = snap([pos(META, "600", reported_symbol="FB")], custodian=CUST_A)
        fresh = snap([pos(META, "600", reported_symbol="META")], custodian=CUST_B)
        self.assertEqual(detect_cross_custodian_qty([stale, fresh]), [])


class TestFxConsistency(unittest.TestCase):
    def test_fires_when_the_statement_disagrees_with_its_own_rate(self):
        found = detect_fx_inconsistency(snap(
            [pos(AAPL, "1300", price="238.90", ccy="USD",
                 market_value="310570.00", reported_base_value="277649.58")],
            base_ccy="EUR",
            fx_rates=[fx("USD/EUR", "0.9170")],
        ))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "FX_INCONSISTENT")
        self.assertEqual(found[0].value_ccy, "EUR")
        self.assertEqual(found[0].detail["implied_rate"], "0.894000")

    def test_silent_when_the_conversion_is_right(self):
        found = detect_fx_inconsistency(snap(
            [pos(AAPL, "1300", price="238.90", ccy="USD",
                 market_value="310570.00", reported_base_value="284792.69")],
            base_ccy="EUR",
            fx_rates=[fx("USD/EUR", "0.9170")],
        ))
        self.assertEqual(found, [])

    def test_silent_on_a_base_currency_holding(self):
        """A EUR line in a EUR statement has no conversion to be wrong about.
        Inventing a 1.0000 rate for it would be inventing a finding."""
        found = detect_fx_inconsistency(snap(
            [pos(ASML, "300", price="915.60", ccy="EUR", market_value="274680.00")],
            base_ccy="EUR",
            fx_rates=[fx("USD/EUR", "0.9170")],
        ))
        self.assertEqual(found, [])

    def test_silent_within_rounding_tolerance(self):
        found = detect_fx_inconsistency(snap(
            [pos(AAPL, "1000", price="100", ccy="USD",
                 market_value="100000.00", reported_base_value="91700.50")],
            base_ccy="EUR",
            fx_rates=[fx("USD/EUR", "0.9170")],
        ))
        self.assertEqual(found, [])

    def test_silent_when_no_base_value_is_reported(self):
        found = detect_fx_inconsistency(snap(
            [pos(AAPL, "1000", price="100", ccy="USD", market_value="100000.00")],
            base_ccy="EUR",
            fx_rates=[fx("USD/EUR", "0.9170")],
        ))
        self.assertEqual(found, [])

    def test_silent_when_the_statement_quotes_no_rate_for_the_pair(self):
        found = detect_fx_inconsistency(snap(
            [pos(AAPL, "1000", price="100", ccy="USD",
                 market_value="100000.00", reported_base_value="1.00")],
            base_ccy="EUR",
            fx_rates=[],
        ))
        self.assertEqual(found, [])


class TestCostBasisRollforward(unittest.TestCase):
    def test_a_buy_adds_its_consideration(self):
        got = roll_cost_basis(Decimal("1000"), Decimal("100000"),
                              [txn(AAPL, "BUY", qty="300", amount="-69420")])
        self.assertEqual(got, Decimal("169420.00"))

    def test_a_sale_relieves_at_weighted_average(self):
        got = roll_cost_basis(Decimal("1500"), Decimal("258660"),
                              [txn(AAPL, "SELL", qty="-200", amount="47860")])
        self.assertEqual(got, Decimal("224172.00"))

    def test_a_split_moves_shares_and_leaves_basis_alone(self):
        """Not a special case bolted on — it is the definition of a split."""
        got = roll_cost_basis(Decimal("800"), Decimal("210400"),
                              [txn(NVDA, "SPLIT", qty="2400")])
        self.assertEqual(got, Decimal("210400.00"))

    def test_dividends_and_fees_do_not_touch_basis(self):
        got = roll_cost_basis(Decimal("100"), Decimal("10000"),
                              [txn(AAPL, "DIV", amount="325"),
                               txn(AAPL, "FEE", amount="-100")])
        self.assertEqual(got, Decimal("10000.00"))

    def test_the_full_demo_sequence(self):
        got = roll_cost_basis(
            Decimal("1200"), Decimal("189240"),
            [txn(AAPL, "BUY", qty="300", amount="-69420", trade_date=date(2026, 5, 12)),
             txn(AAPL, "SELL", qty="-200", amount="47860", trade_date=date(2026, 6, 9)),
             txn(AAPL, "DIV", amount="325", trade_date=date(2026, 6, 15))],
        )
        self.assertEqual(got, Decimal("224172.00"))


class TestCostBasisDrift(unittest.TestCase):
    def test_fires_on_a_misstated_basis(self):
        found = detect_cost_basis_drift(
            snap([pos(AAPL, "1200", cost_basis="189240")], as_of=PRIOR),
            snap([pos(AAPL, "1300", cost_basis="217416")]),
            [txn(AAPL, "BUY", qty="300", amount="-69420", trade_date=date(2026, 5, 12)),
             txn(AAPL, "SELL", qty="-200", amount="47860", trade_date=date(2026, 6, 9))],
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "COST_BASIS_DRIFT")
        self.assertEqual(found[0].value_at_risk, Decimal("-6756.00"))
        self.assertEqual(found[0].severity, "high")

    def test_silent_when_the_reported_basis_is_right(self):
        found = detect_cost_basis_drift(
            snap([pos(AAPL, "1200", cost_basis="189240")], as_of=PRIOR),
            snap([pos(AAPL, "1300", cost_basis="224172")]),
            [txn(AAPL, "BUY", qty="300", amount="-69420", trade_date=date(2026, 5, 12)),
             txn(AAPL, "SELL", qty="-200", amount="47860", trade_date=date(2026, 6, 9))],
        )
        self.assertEqual(found, [])

    def test_silent_when_the_custodian_reports_no_basis_at_all(self):
        """
        The Swiss custodian reports none. Reading None as zero would produce a
        critical finding on every one of its holdings on the first run in front
        of a client, all of them fictional.
        """
        found = detect_cost_basis_drift(
            snap([pos(AAPL, "1200", cost_basis=None)], as_of=PRIOR),
            snap([pos(AAPL, "1300", cost_basis=None)]),
            [],
        )
        self.assertEqual(found, [])

    def test_a_reported_zero_is_not_the_same_as_unreported(self):
        found = detect_cost_basis_drift(
            snap([pos(AAPL, "1200", cost_basis="189240")], as_of=PRIOR),
            snap([pos(AAPL, "1200", cost_basis="0")]),
            [],
        )
        self.assertEqual(len(found), 1)


class TestCrossCustodianQty(unittest.TestCase):
    def test_fires_when_two_custodians_disagree(self):
        found = detect_cross_custodian_qty([
            snap([pos(NVDA, "3200", price="118.90")], custodian=CUST_A),
            snap([pos(NVDA, "800", price="118.90")], custodian=CUST_B),
        ])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "CROSS_CUSTODIAN_QTY")
        self.assertEqual(found[0].value_at_risk, Decimal("285360.00"))

    def test_records_a_clean_ratio_as_context_never_as_the_finding(self):
        found = detect_cross_custodian_qty([
            snap([pos(NVDA, "3200")], custodian=CUST_A),
            snap([pos(NVDA, "800")], custodian=CUST_B),
        ])
        self.assertEqual(found[0].detail["ratio_between_custodians"], "4:1")
        self.assertEqual(found[0].kind, "CROSS_CUSTODIAN_QTY",
                         "the ratio is a hint; the reference-backed rule makes the claim")

    def test_no_ratio_note_when_the_gap_is_not_a_split(self):
        found = detect_cross_custodian_qty([
            snap([pos(VOD, "4400")], custodian=CUST_A),
            snap([pos(VOD, "5000")], custodian=CUST_B),
        ])
        self.assertNotIn("ratio_between_custodians", found[0].detail)

    def test_silent_when_they_agree(self):
        found = detect_cross_custodian_qty([
            snap([pos(AAPL, "1300")], custodian=CUST_A),
            snap([pos(AAPL, "1300")], custodian=CUST_B),
        ])
        self.assertEqual(found, [])

    def test_a_single_custodian_holding_is_not_a_disagreement(self):
        found = detect_cross_custodian_qty([
            snap([pos(AAPL, "1300")], custodian=CUST_A),
            snap([pos(ASML, "300", ccy="EUR")], custodian=CUST_B),
        ])
        self.assertEqual(found, [])


class TestStatementBasis(unittest.TestCase):
    """
    Two custodians, one mandate, different statement bases.

    The expensive case in the whole suite. A trade-date statement counts a trade
    when it is executed; a settled-date one counts it when the shares move.
    Between them sits every trade in flight over the period end — and the naive
    difference is real, large, and not a break.
    """

    def setUp(self):
        # 500 shares bought on 29 June, settling 2 July. Inside the trade-date
        # book at 30 June, outside the settled one.
        self.in_flight = txn(
            AAPL, "BUY", qty="500", amount="-119450",
            trade_date=date(2026, 6, 29), settle_date=date(2026, 7, 2),
            custodian=CUST_B,
        )
        self.settled = snap(
            [pos(AAPL, "1300", price="238.90")], custodian=CUST_A, basis=BASIS_SETTLED
        )
        self.traded = snap(
            [pos(AAPL, "1800", price="238.90")], custodian=CUST_B, basis=BASIS_TRADE
        )
        self.txns = {CUST_B: [self.in_flight]}

    # --- the silence this rule exists for ---

    def test_a_difference_the_bases_fully_explain_is_not_a_break(self):
        """
        USD 119,450 apart on the largest holding in the book, and correctly
        silent. This is the false positive that teaches an operations team to
        stop reading the queue, so it is a required silence, not a nicety.
        """
        found = detect_cross_custodian_qty([self.settled, self.traded], self.txns)
        self.assertEqual(found, [])

    def test_the_residual_is_reported_when_the_bases_explain_only_part(self):
        """
        Netting the explained part is not the same as going quiet. A rule that
        skipped the comparison whenever bases differed would fall silent on
        exactly the custodian pair that most needs checking.
        """
        traded = snap(
            [pos(AAPL, "1900", price="238.90")], custodian=CUST_B, basis=BASIS_TRADE
        )
        found = detect_cross_custodian_qty([self.settled, traded], self.txns)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].detail["difference"], "-100")
        self.assertEqual(found[0].detail["reported_difference"], "-600")
        self.assertEqual(found[0].detail["explained_by_settlement"], "-500")
        self.assertEqual(found[0].value_at_risk, Decimal("23890.00"))

    def test_direction_is_not_assumed(self):
        """The same pair with the bases the other way round nets to zero too —
        the sign of the adjustment follows which custodian reports trade date,
        not which one was listed first."""
        settled = snap(
            [pos(AAPL, "1300", price="238.90")], custodian=CUST_B, basis=BASIS_SETTLED
        )
        traded = snap(
            [pos(AAPL, "1800", price="238.90")], custodian=CUST_A, basis=BASIS_TRADE
        )
        flight = txn(
            AAPL, "BUY", qty="500", trade_date=date(2026, 6, 29),
            settle_date=date(2026, 7, 2), custodian=CUST_A,
        )
        self.assertEqual(
            detect_cross_custodian_qty([settled, traded], {CUST_A: [flight]}), []
        )

    def test_a_settled_trade_explains_nothing(self):
        """
        Netting is driven by what is genuinely in flight, not by any trade near
        the boundary. A trade that settled before the period end is in both
        books, so the difference stands as a break.
        """
        settled_trade = txn(
            AAPL, "BUY", qty="500", trade_date=date(2026, 6, 20),
            settle_date=date(2026, 6, 24), custodian=CUST_B,
        )
        found = detect_cross_custodian_qty(
            [self.settled, self.traded], {CUST_B: [settled_trade]}
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].detail["difference"], "-500")

    def test_matching_bases_stay_exactly_as_strict_as_before(self):
        """The regression that matters: teaching this rule about settlement must
        not have loosened it for the ordinary same-basis case."""
        both_settled = snap(
            [pos(AAPL, "1800", price="238.90")], custodian=CUST_B, basis=BASIS_SETTLED
        )
        found = detect_cross_custodian_qty([self.settled, both_settled], self.txns)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].detail["difference"], "-500")
        self.assertNotIn("explained_by_settlement", found[0].detail)

    def test_no_transactions_supplied_means_nothing_is_explained_away(self):
        """
        Called without activity, the rule must not assume a difference is
        settlement. Silence bought by an absent argument is the worst kind.
        """
        found = detect_cross_custodian_qty([self.settled, self.traded])
        self.assertEqual(len(found), 1)

    # --- the control finding ---

    def test_the_mismatch_itself_is_reported(self):
        found = detect_basis_mismatch([self.settled, self.traded], self.txns)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "STATEMENT_BASIS_MISMATCH")
        self.assertEqual(found[0].isin, "", "this is an account-level control finding")
        self.assertEqual(found[0].value_at_risk, Decimal("119450.00"))
        self.assertEqual(found[0].detail["movements_in_flight"], "1")

    def test_severity_is_capped_because_nothing_is_missing(self):
        """USD 119,450 would band as critical on exposure alone. Nothing is
        missing here — it is in flight — so it must not outrank a genuinely
        absent half-million of stock."""
        found = detect_basis_mismatch([self.settled, self.traded], self.txns)
        self.assertEqual(found[0].severity, "medium")

    def test_silent_when_every_custodian_reports_the_same_basis(self):
        other = snap([pos(AAPL, "1300")], custodian=CUST_B, basis=BASIS_SETTLED)
        self.assertEqual(detect_basis_mismatch([self.settled, other], self.txns), [])

    def test_silent_for_a_single_custodian(self):
        self.assertEqual(detect_basis_mismatch([self.traded], self.txns), [])

    def test_it_cites_the_movements_it_counted(self):
        """The reviewer must be able to get from the finding to the trade that
        causes it without going looking."""
        found = detect_basis_mismatch([self.settled, self.traded], self.txns)
        self.assertEqual(
            [c.cite() for c in found[0].citations], [self.in_flight.source.cite()]
        )


class TestInFlightQty(unittest.TestCase):
    def test_nets_movements_in_both_directions(self):
        txns = [
            txn(AAPL, "BUY", qty="500", trade_date=date(2026, 6, 29),
                settle_date=date(2026, 7, 2)),
            txn(AAPL, "SELL", qty="-200", trade_date=date(2026, 6, 30),
                settle_date=date(2026, 7, 2)),
        ]
        self.assertEqual(in_flight_qty(txns, AAPL, date(2026, 6, 30)), Decimal("300"))

    def test_ignores_other_instruments(self):
        txns = [txn(NVDA, "BUY", qty="500", trade_date=date(2026, 6, 29),
                    settle_date=date(2026, 7, 2))]
        self.assertEqual(in_flight_qty(txns, AAPL, date(2026, 6, 30)), Decimal("0"))

    def test_a_movement_with_no_settlement_date_is_never_in_flight(self):
        """
        A split has no settlement date because it does not settle. Treating a
        missing date as "settles later" would put a fictional corporate action
        in flight across every period boundary it touched.
        """
        txns = [txn(AAPL, "SPLIT", qty="2400", trade_date=date(2026, 6, 29))]
        self.assertEqual(in_flight_qty(txns, AAPL, date(2026, 6, 30)), Decimal("0"))


class TestPriceDivergence(unittest.TestCase):
    """
    Silent on the demo data — both custodians price every shared holding
    identically — and proven here instead. That is the correct relationship
    between a rule and its evidence: the demo shows what the desk found, the
    tests show what it can find.
    """

    def test_fires_on_a_material_gap(self):
        found = detect_price_divergence([
            snap([pos(AAPL, "1000", price="238.90")], custodian=CUST_A),
            snap([pos(AAPL, "1000", price="231.40")], custodian=CUST_B),
        ])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "PRICE_DIVERGENCE")
        self.assertEqual(found[0].value_at_risk, Decimal("7500.00"))

    def test_silent_inside_tolerance(self):
        found = detect_price_divergence([
            snap([pos(AAPL, "1000", price="238.90")], custodian=CUST_A),
            snap([pos(AAPL, "1000", price="238.60")], custodian=CUST_B),
        ])
        self.assertEqual(found, [])

    def test_silent_across_currencies(self):
        """Two prices in different currencies are not a divergence; comparing
        them would be comparing a number to a different number."""
        found = detect_price_divergence([
            snap([pos(AAPL, "1000", price="238.90", ccy="USD")], custodian=CUST_A),
            snap([pos(AAPL, "1000", price="219.10", ccy="EUR")], custodian=CUST_B),
        ])
        self.assertEqual(found, [])


class TestMissingFeeAccrual(unittest.TestCase):
    def test_fires_when_a_recurring_fee_stops(self):
        found = detect_missing_fee_accrual(
            [txn("", "FEE", amount="-4287.50", trade_date=PRIOR)],
            [txn(AAPL, "BUY", qty="300", amount="-69420")],
            CUST_A, ACCOUNT, CURRENT,
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "MISSING_FEE_ACCRUAL")
        self.assertEqual(found[0].value_at_risk, Decimal("4287.50"))
        self.assertEqual(found[0].isin, "", "this is an account-level finding")

    def test_silent_when_the_fee_recurs(self):
        found = detect_missing_fee_accrual(
            [txn("", "FEE", amount="-4287.50", trade_date=PRIOR)],
            [txn("", "FEE", amount="-3912.40", trade_date=CURRENT)],
            CUST_A, ACCOUNT, CURRENT,
        )
        self.assertEqual(found, [])

    def test_silent_when_there_was_no_prior_fee_to_recur(self):
        found = detect_missing_fee_accrual([], [], CUST_A, ACCOUNT, CURRENT)
        self.assertEqual(found, [])


class TestDetectAll(unittest.TestCase):
    def _period(self):
        prior = snap([pos(NVDA, "800", price="452.60"), pos(AAPL, "1200", cost_basis="189240")],
                     as_of=PRIOR, custodian=CUST_A)
        current = snap([pos(NVDA, "800", price="118.90"), pos(AAPL, "1300", cost_basis="217416")],
                       custodian=CUST_A)
        return _util.period(
            prior={CUST_A: prior},
            current={CUST_A: current},
            txns_prior={CUST_A: [txn("", "FEE", amount="-4287.50", trade_date=PRIOR)]},
            txns_current={CUST_A: [
                txn(AAPL, "BUY", qty="300", amount="-69420", trade_date=date(2026, 5, 12)),
                txn(AAPL, "SELL", qty="-200", amount="47860", trade_date=date(2026, 6, 9)),
            ]},
            actions=[action(NVDA, num=4, den=1)],
        )

    def test_runs_every_applicable_rule(self):
        found = detect_all(self._period())
        self.assertTrue(has_break(found, "CORP_ACTION_UNAPPLIED", NVDA))
        self.assertTrue(has_break(found, "COST_BASIS_DRIFT", AAPL))
        self.assertTrue(has_break(found, "MISSING_FEE_ACCRUAL"))

    def test_worst_first(self):
        from model import SEVERITY_ORDER

        found = detect_all(self._period())
        ranks = [SEVERITY_ORDER.index(b.severity) for b in found]
        self.assertEqual(ranks, sorted(ranks))

    def test_output_is_deterministic(self):
        first = [b.key() for b in detect_all(self._period())]
        second = [b.key() for b in detect_all(self._period())]
        self.assertEqual(first, second)

    def test_a_period_with_one_custodian_and_no_prior_does_not_crash(self):
        found = detect_all(_util.period(current={CUST_A: snap([pos(AAPL, "100")])}))
        self.assertEqual(found, [])

    def test_every_break_carries_at_least_one_citation(self):
        for b in detect_all(self._period()):
            self.assertTrue(b.citations, "%s has no provenance" % b.key())

    def test_no_figure_is_ever_a_float(self):
        for b in detect_all(self._period()):
            self.assertNotIsInstance(b.value_at_risk, float)
            if b.value_at_risk is not None:
                self.assertIsInstance(b.value_at_risk, Decimal)


class TestPublishedRuleList(unittest.TestCase):
    def test_every_rule_that_can_fire_is_published(self):
        """
        The report publishes the rule list beside the findings. If a detector
        can emit a kind that is not in RULES, the coverage claim is false.
        """
        published = set(k for k, _ in RULES)
        emitted = {
            "QTY_ROLLFORWARD", "POSITION_DISAPPEARED", "CORP_ACTION_UNAPPLIED",
            "CORP_ACTION_WRONG_RATIO", "CORP_ACTION_BASIS_CORRUPTED",
            "MERGER_UNPROCESSED", "IDENTIFIER_STALE", "FX_INCONSISTENT",
            "COST_BASIS_DRIFT", "CROSS_CUSTODIAN_QTY", "PRICE_DIVERGENCE",
            "MISSING_FEE_ACCRUAL", "STATEMENT_BASIS_MISMATCH",
        }
        self.assertEqual(published, emitted)

    def test_each_rule_is_described_in_plain_english(self):
        for kind, description in RULES:
            self.assertTrue(description.endswith("."))
            self.assertNotIn("_", description)


if __name__ == "__main__":
    unittest.main()
