"""
test_corpactions.py — the arithmetic the engine is actually built around.

No files, no pipeline, no statements. Corporate actions are where a naive
reconciliation engine produces its most confident wrong answers, so this is the
module that gets tested hardest and in isolation.
"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

import _util
from _util import AAPL, CURRENT, MID, MSFT, NVDA, PRIOR, SPCX, TSLA, VOD, action, pos, txn

import corpactions
from corpactions import (
    ALLOWED_SPLIT_RATIOS,
    application_status,
    actions_in_window,
    basis_is_preserved,
    booked_by,
    expected_quantity,
    infer_ratio,
    merger_status,
    name_change_status,
    quantity_matches,
    split_delta,
)


class TestInferRatio(unittest.TestCase):
    def test_reads_a_real_split(self):
        self.assertEqual(infer_ratio(Decimal("800"), Decimal("3200")), (4, 1))
        self.assertEqual(infer_ratio(Decimal("500"), Decimal("750")), (3, 2))
        self.assertEqual(infer_ratio(Decimal("450"), Decimal("675")), (3, 2))

    def test_reads_a_reverse_split(self):
        self.assertEqual(infer_ratio(Decimal("1000"), Decimal("100")), (1, 10))

    def test_the_case_a_bounds_check_gets_wrong(self):
        """
        5,000 -> 4,400 reduces to a perfectly tidy 22:25. A "both terms under
        thirty" check calls that a split. No issuer has declared a 22-for-25
        split, and the demo's Vodafone break is exactly this shape: reading it
        as a corporate action would explain away a real 600-share hole.
        """
        self.assertIsNone(infer_ratio(Decimal("5000"), Decimal("4400")))
        self.assertNotIn((22, 25), ALLOWED_SPLIT_RATIOS)

    def test_equal_counts_are_not_a_ratio(self):
        self.assertIsNone(infer_ratio(Decimal("100"), Decimal("100")))

    def test_noise_is_not_a_ratio(self):
        self.assertIsNone(infer_ratio(Decimal("1301"), Decimal("1300")))
        self.assertIsNone(infer_ratio(Decimal("777"), Decimal("1013")))

    def test_non_positive_and_missing_inputs(self):
        self.assertIsNone(infer_ratio(Decimal("0"), Decimal("100")))
        self.assertIsNone(infer_ratio(Decimal("100"), Decimal("0")))
        self.assertIsNone(infer_ratio(Decimal("-100"), Decimal("100")))
        self.assertIsNone(infer_ratio(None, Decimal("100")))

    def test_allowlist_has_no_absurd_entries(self):
        for num, den in ALLOWED_SPLIT_RATIOS:
            self.assertTrue(num > 0 and den > 0)
            self.assertTrue(num <= 20 and den <= 20)


class TestExpectedQuantity(unittest.TestCase):
    def test_forward_split(self):
        act = action(NVDA, num=4, den=1)
        self.assertEqual(expected_quantity(Decimal("800"), act), Decimal("3200"))

    def test_fractional_ratio(self):
        act = action(MSFT, num=3, den=2)
        self.assertEqual(expected_quantity(Decimal("500"), act), Decimal("750"))

    def test_reverse_split(self):
        act = action(VOD, kind="REVERSE_SPLIT", num=1, den=10)
        self.assertEqual(expected_quantity(Decimal("5000"), act), Decimal("500"))

    def test_an_action_that_moves_no_shares_leaves_the_count_alone(self):
        act = action(SPCX, kind="MERGER", num=2, den=1, related=TSLA)
        self.assertEqual(expected_quantity(Decimal("1000"), act), Decimal("1000"))


class TestSplitDelta(unittest.TestCase):
    def test_a_split_books_the_delta_not_the_target(self):
        """800 shares through a 4-for-1 is a +2,400 line, not a 3,200 line."""
        act = action(NVDA, num=4, den=1)
        self.assertEqual(split_delta(Decimal("800"), act), Decimal("2400"))

    def test_reverse_split_delta_is_negative(self):
        act = action(VOD, kind="REVERSE_SPLIT", num=1, den=10)
        self.assertEqual(split_delta(Decimal("5000"), act), Decimal("-4500"))


class TestQuantityMatches(unittest.TestCase):
    def test_within_a_thousandth(self):
        self.assertTrue(quantity_matches(Decimal("100.0005"), Decimal("100")))

    def test_outside_it(self):
        self.assertFalse(quantity_matches(Decimal("100.01"), Decimal("100")))


class TestBasisPreserved(unittest.TestCase):
    def test_unchanged_total_is_preserved(self):
        self.assertTrue(basis_is_preserved(Decimal("210400"), Decimal("210400")))

    def test_scaled_total_is_not(self):
        self.assertFalse(basis_is_preserved(Decimal("210400"), Decimal("841600")))

    def test_unreported_basis_is_none_not_false(self):
        """
        "We cannot tell" and "it is wrong" are different answers, and BHP
        reports no cost basis at all. Collapsing them would flag every Swiss
        holding as having lost its entire basis.
        """
        self.assertIsNone(basis_is_preserved(None, Decimal("1")))
        self.assertIsNone(basis_is_preserved(Decimal("1"), None))
        self.assertIsNone(basis_is_preserved(None, None))


class TestActionsInWindow(unittest.TestCase):
    def setUp(self):
        self.acts = [
            action(NVDA, ex_date=PRIOR),                       # on the boundary
            action(MSFT, ex_date=date(2026, 6, 2), num=3, den=2),
            action(AAPL, ex_date=CURRENT),                     # on the boundary
            action(VOD, ex_date=date(2026, 7, 15)),            # after
        ]

    def test_lower_bound_is_open(self):
        """An action on the prior statement date is already in that statement."""
        got = actions_in_window(self.acts, PRIOR, CURRENT)
        self.assertNotIn(NVDA, [a.isin for a in got])

    def test_upper_bound_is_closed(self):
        got = actions_in_window(self.acts, PRIOR, CURRENT)
        self.assertIn(AAPL, [a.isin for a in got])

    def test_later_actions_excluded(self):
        got = actions_in_window(self.acts, PRIOR, CURRENT)
        self.assertNotIn(VOD, [a.isin for a in got])

    def test_filter_matches_both_legs_of_a_two_legged_action(self):
        merger = action(SPCX, kind="MERGER", num=2, den=1, related=TSLA,
                        ex_date=date(2026, 6, 12))
        self.assertEqual(actions_in_window([merger], PRIOR, CURRENT, isin=SPCX), [merger])
        self.assertEqual(actions_in_window([merger], PRIOR, CURRENT, isin=TSLA), [merger])
        self.assertEqual(actions_in_window([merger], PRIOR, CURRENT, isin=AAPL), [])

    def test_result_is_ordered(self):
        got = actions_in_window(self.acts, PRIOR, CURRENT)
        self.assertEqual(got, sorted(got, key=lambda a: (a.ex_date, a.isin)))


class TestBookedBy(unittest.TestCase):
    def test_matches_on_instrument_kind_and_date(self):
        act = action(NVDA, num=4, den=1, ex_date=MID)
        good = txn(NVDA, "SPLIT", qty="2400", trade_date=MID)
        wrong_date = txn(NVDA, "SPLIT", qty="2400", trade_date=CURRENT)
        wrong_isin = txn(AAPL, "SPLIT", qty="2400", trade_date=MID)
        wrong_kind = txn(NVDA, "BUY", qty="2400", trade_date=MID)
        self.assertEqual(booked_by([good, wrong_date, wrong_isin, wrong_kind], act), [good])

    def test_merger_accepts_either_activity_code(self):
        act = action(SPCX, kind="MERGER", num=2, den=1, related=TSLA, ex_date=MID)
        as_merger = txn(SPCX, "MERGER", qty="-1000", trade_date=MID)
        self.assertEqual(booked_by([as_merger], act), [as_merger])

    def test_can_be_asked_about_the_other_leg(self):
        act = action(SPCX, kind="MERGER", num=2, den=1, related=TSLA, ex_date=MID)
        credit = txn(TSLA, "MERGER", qty="2000", trade_date=MID)
        self.assertEqual(booked_by([credit], act, isin=TSLA), [credit])
        self.assertEqual(booked_by([credit], act), [])


class TestApplicationStatus(unittest.TestCase):
    def test_routing_errors_are_loud(self):
        """
        A merger does not scale a position and a ticker change moves no shares.
        Asking this function about either is a programming error, not a data
        condition, so it raises rather than returning a plausible-looking dict.
        """
        merger = action(SPCX, kind="MERGER", num=2, den=1, related=TSLA)
        rename = action(_util.META, kind="NAME_CHANGE", num=1, den=1,
                        old_symbol="FB", new_symbol="META")
        for act in (merger, rename):
            with self.assertRaises(ValueError):
                application_status(act, pos(act.isin, "100"), pos(act.isin, "100"), [])

    def test_not_held(self):
        st = application_status(action(NVDA), None, None, [])
        self.assertEqual(st["status"], "not_held")

    def test_no_prior_position_is_unknown_not_a_break(self):
        st = application_status(action(NVDA), None, pos(NVDA, "3200"), [])
        self.assertEqual(st["status"], "unknown")

    def test_applied(self):
        act = action(NVDA, num=4, den=1)
        st = application_status(act, pos(NVDA, "800"), pos(NVDA, "3200"), [])
        self.assertEqual(st["status"], "applied")

    def test_not_applied(self):
        act = action(NVDA, num=4, den=1)
        st = application_status(act, pos(NVDA, "800"), pos(NVDA, "800"), [])
        self.assertEqual(st["status"], "not_applied")
        self.assertEqual(st["expected_qty"], Decimal("3200"))

    def test_wrong_ratio(self):
        act = action(NVDA, num=2, den=1)
        st = application_status(act, pos(NVDA, "100"), pos(NVDA, "250"), [])
        self.assertEqual(st["status"], "wrong_ratio")
        self.assertEqual(st["implied_ratio"], (5, 2))

    def test_trade_then_action_ordering(self):
        act = action(MSFT, num=3, den=2)
        trade = txn(MSFT, "BUY", qty="50", amount="-25265")
        st = application_status(act, pos(MSFT, "450"), pos(MSFT, "750"), [trade])
        self.assertEqual(st["status"], "applied")

    def test_action_then_trade_ordering(self):
        """(450 x 3/2) + 50 = 725. The other ordering gives 750. Both are correct
        applications; the statements do not say which happened first."""
        act = action(MSFT, num=3, den=2)
        trade = txn(MSFT, "BUY", qty="50", amount="-25265")
        st = application_status(act, pos(MSFT, "450"), pos(MSFT, "725"), [trade])
        self.assertEqual(st["status"], "applied")

    def test_basis_corrupted(self):
        act = action(NVDA, num=2, den=1)
        st = application_status(
            act,
            pos(NVDA, "100", cost_basis="1000"),
            pos(NVDA, "200", cost_basis="2000"),
            [],
        )
        self.assertEqual(st["status"], "basis_corrupted")

    def test_basis_check_is_skipped_when_trading_muddies_it(self):
        """
        A buy in the same window moves basis for a legitimate reason, and two
        statements alone cannot attribute the change between the two causes.
        Guessing here would manufacture a false positive on every split that
        happened to share a quarter with a trade — the cost-basis rollforward in
        breaks.py is the detector that owns this.
        """
        act = action(MSFT, num=3, den=2)
        trade = txn(MSFT, "BUY", qty="50", amount="-25265")
        st = application_status(
            act,
            pos(MSFT, "450", cost_basis="168750"),
            pos(MSFT, "750", cost_basis="194015"),
            [trade],
        )
        self.assertEqual(st["status"], "applied")
        self.assertIsNone(st["basis_preserved"])
        self.assertIn("not isolable", st["basis_note"])

    def test_booked_is_tracked_separately_from_the_quantity_check(self):
        """Right share count with no transaction behind it reconciles today and
        is unauditable tomorrow, so the two facts are reported separately."""
        act = action(NVDA, num=4, den=1)
        st = application_status(act, pos(NVDA, "800"), pos(NVDA, "3200"), [])
        self.assertEqual(st["status"], "applied")
        self.assertEqual(st["booked"], [])


class TestMergerStatus(unittest.TestCase):
    """
    SPCX is acquired by TSLA at two-for-one. 1,000 target shares become 2,000
    acquirer shares on top of the 400 already held.
    """

    def setUp(self):
        self.act = action(SPCX, kind="MERGER", num=2, den=1, related=TSLA, ex_date=MID)
        self.prior = {SPCX: pos(SPCX, "1000"), TSLA: pos(TSLA, "400")}

    def test_both_legs_processed(self):
        current = {TSLA: pos(TSLA, "2400")}
        st = merger_status(self.act, self.prior, current, [])
        self.assertEqual(st["status"], "both_legs_ok")
        self.assertEqual(st["entitlement"], Decimal("2000"))

    def test_the_case_worth_building_the_engine_for(self):
        """
        Target removed, acquirer never credited. Every self-consistency check
        passes — the position file and the activity file agree with each other —
        and 2,000 shares have vanished from the client's holdings.
        """
        current = {TSLA: pos(TSLA, "400")}
        st = merger_status(self.act, self.prior, current, [])
        self.assertEqual(st["status"], "acquirer_not_credited")
        self.assertEqual(st["acquirer_shortfall"], Decimal("2000"))

    def test_target_not_removed(self):
        current = {SPCX: pos(SPCX, "1000"), TSLA: pos(TSLA, "2400")}
        st = merger_status(self.act, self.prior, current, [])
        self.assertEqual(st["status"], "target_not_removed")

    def test_neither_leg_processed(self):
        current = {SPCX: pos(SPCX, "1000"), TSLA: pos(TSLA, "400")}
        st = merger_status(self.act, self.prior, current, [])
        self.assertEqual(st["status"], "not_processed")

    def test_not_held_is_silent(self):
        st = merger_status(self.act, {TSLA: pos(TSLA, "400")}, {TSLA: pos(TSLA, "400")}, [])
        self.assertEqual(st["status"], "not_held")

    def test_a_zeroed_target_counts_as_removed(self):
        current = {SPCX: pos(SPCX, "0"), TSLA: pos(TSLA, "2400")}
        st = merger_status(self.act, self.prior, current, [])
        self.assertEqual(st["status"], "both_legs_ok")

    def test_trading_in_the_acquirer_is_netted_out(self):
        """Buying 100 more Tesla during the window is not a merger failure."""
        current = {TSLA: pos(TSLA, "2500")}
        trade = txn(TSLA, "BUY", qty="100", amount="-27130")
        st = merger_status(self.act, self.prior, current, [trade])
        self.assertEqual(st["status"], "both_legs_ok")

    def test_acquirer_not_previously_held(self):
        prior = {SPCX: pos(SPCX, "1000")}
        st = merger_status(self.act, prior, {TSLA: pos(TSLA, "2000")}, [])
        self.assertEqual(st["status"], "both_legs_ok")


class TestNameChangeStatus(unittest.TestCase):
    def setUp(self):
        self.act = action(_util.META, kind="NAME_CHANGE", num=1, den=1,
                          old_symbol="FB", new_symbol="META")

    def test_stale_ticker(self):
        st = name_change_status(self.act, pos(_util.META, "600", reported_symbol="FB"))
        self.assertEqual(st["status"], "stale")
        self.assertEqual(st["reported_symbol"], "FB")

    def test_current_ticker(self):
        st = name_change_status(self.act, pos(_util.META, "600", reported_symbol="META"))
        self.assertEqual(st["status"], "current")

    def test_case_is_not_a_difference(self):
        st = name_change_status(self.act, pos(_util.META, "600", reported_symbol="meta"))
        self.assertEqual(st["status"], "current")

    def test_not_held(self):
        self.assertEqual(name_change_status(self.act, None)["status"], "not_held")

    def test_no_symbol_reported(self):
        st = name_change_status(self.act, pos(_util.META, "600", reported_symbol=""))
        self.assertEqual(st["status"], "no_symbol_reported")

    def test_a_third_ticker_is_flagged_as_unrecognised(self):
        st = name_change_status(self.act, pos(_util.META, "600", reported_symbol="FBK"))
        self.assertEqual(st["status"], "unrecognised")


class TestPurity(unittest.TestCase):
    def test_module_reads_no_files_and_holds_no_clock(self):
        with open(corpactions.__file__, encoding="utf-8") as fh:
            source = fh.read()
        for forbidden in ("open(", "datetime.now", "date.today", "requests", "os.environ"):
            self.assertNotIn(forbidden, source,
                             "%s must stay free of %r to remain testable in isolation"
                             % (corpactions.__file__, forbidden))


if __name__ == "__main__":
    unittest.main()
