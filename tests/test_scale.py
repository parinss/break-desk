"""
test_scale.py — the desk on a book nobody can eyeball.

Two claims are under test here, and they are different in kind.

**The manifest still holds.** Ten seeded findings must come out of five thousand
positions, and only those ten. At nine instruments a human can check the engine's
homework; at five thousand nobody does, which is exactly when an engine starts
quietly inventing findings and quietly losing them.

**The queue still means something.** An operations team works a queue from the
top. If severity ranking does not float the largest exposures up through a book
this size, the report is a list, and a list of two hundred criticals is a way of
saying nothing at all.

## Why there are no stopwatches in this file

Performance is asserted structurally, not by wall clock. `scripts/scale.py`
reports seconds — that is its job, on a machine somebody chose. A test that
fails when CI is busy teaches people to re-run it, and a test people re-run
until it passes is not a test.

So the complexity claims are made by counting scans of the movement list with a
list subclass that records its own iteration. A quadratic detector scans once
per position; a linear one scans a fixed number of times no matter how large the
book gets. That count is a property of the algorithm, identical on every machine
and in every mood CI is in, and it fails loudly the moment somebody puts a filter
back inside the loop.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from _util import CURRENT, CUST_A, PRIOR, prov, snap, txn

import breaks
import scale
import securities
from explain import InventedFigureError, check_prose, deterministic_writer
from model import SEVERITY_ORDER, Transaction
from money import ZERO


class _CountingList(list):
    """
    A list that records how many times it was iterated.

    This is the whole complexity harness. `[t for t in txns if ...]` calls
    `__iter__`; so does `for t in txns`. A detector that groups the movements
    once and then serves every position from the index iterates once. A detector
    that filters inside the loop iterates once per position, and the count says
    so in a number rather than in a duration.
    """

    def __init__(self, items):
        list.__init__(self, items)
        self.scans = 0

    def __iter__(self):
        self.scans += 1
        return list.__iter__(self)


def _scan_counts(positions):
    # type: (int) -> dict
    """Run each detector over a book of `positions` lines, counting list scans."""
    period, _ = scale.build_book(positions)
    prior = period["prior"][scale.MERIDIAN]
    current = period["current"][scale.MERIDIAN]

    counted = {}
    for name in period["txns_current"]:
        counted[name] = _CountingList(period["txns_current"][name])

    breaks.detect_qty_rollforward(prior, current, counted[scale.MERIDIAN])
    rollforward = counted[scale.MERIDIAN].scans

    counted[scale.MERIDIAN].scans = 0
    breaks.detect_position_disappeared(prior, current, counted[scale.MERIDIAN])
    disappeared = counted[scale.MERIDIAN].scans

    counted[scale.MERIDIAN].scans = 0
    breaks.detect_cost_basis_drift(prior, current, counted[scale.MERIDIAN])
    cost_basis = counted[scale.MERIDIAN].scans

    for name in counted:
        counted[name].scans = 0
    snapshots = [period["current"][c] for c in sorted(period["current"])]
    breaks.detect_cross_custodian_qty(snapshots, counted)
    cross = sum(c.scans for c in counted.values())

    return {
        "instruments": len(current.positions),
        "qty_rollforward": rollforward,
        "position_disappeared": disappeared,
        "cost_basis_drift": cost_basis,
        "cross_custodian_qty": cross,
    }


class TestBookShape(unittest.TestCase):
    def test_book_has_roughly_the_requested_number_of_positions(self):
        period, _ = scale.build_book(3000)
        total = sum(len(s.positions) for s in period["current"].values())
        # One short: a seeded holding has left one custodian's statement, which
        # is one of the ten things the book is for.
        self.assertEqual(total, 2999)

    def test_book_is_the_shape_load_period_returns(self):
        period, _ = scale.build_book(300)
        self.assertEqual(
            sorted(period),
            ["actions", "current", "prior", "txns_current", "txns_prior"],
        )

    def test_identifiers_are_isin_shaped(self):
        self.assertTrue(securities.ISIN_RE.match(scale.isin_for(1)))
        self.assertTrue(securities.ISIN_RE.match(scale.isin_for(9999)))

    def test_identifiers_are_not_in_the_real_security_master(self):
        """
        A synthetic identifier that could be mistaken for a real instrument is a
        liability in a repository anyone can clone. `USSCALE...` cannot be.
        """
        for i in (1, 500, 9999):
            self.assertNotIn(scale.isin_for(i), securities.BY_ISIN)

    def test_a_book_too_large_for_the_identifier_scheme_is_refused(self):
        with self.assertRaises(ValueError):
            scale.build_book(40000)

    def test_index_out_of_range_is_refused_rather_than_wrapped(self):
        with self.assertRaises(ValueError):
            scale.isin_for(10000)
        with self.assertRaises(ValueError):
            scale.isin_for(0)


class TestManifestAtScale(unittest.TestCase):
    """A missed break and an invented one are both failures. At this size the
    invented one is the likelier of the two, and the harder to notice."""

    def test_manifest_matches_exactly(self):
        period, expected = scale.build_book(5000)
        found = breaks.detect_all(period)
        self.assertEqual(scale.found_manifest(found), expected)

    def test_the_same_ten_findings_come_out_of_twice_the_book(self):
        """
        The seeds are a fixed set, so doubling the clean positions around them
        must change nothing. Anything that grows with book size is a false
        positive with a plausible excuse.
        """
        small, expected = scale.build_book(2500)
        large, _ = scale.build_book(5000)
        self.assertEqual(scale.found_manifest(breaks.detect_all(small)), expected)
        self.assertEqual(scale.found_manifest(breaks.detect_all(large)), expected)

    def test_every_finding_carries_provenance(self):
        period, _ = scale.build_book(1500)
        for brk in breaks.detect_all(period):
            self.assertTrue(brk.citations, "%s has no citations" % brk.kind)
            for cite in brk.citations:
                self.assertTrue(cite.file)
                self.assertTrue(cite.excerpt)


class TestQueueOrder(unittest.TestCase):
    """Severity bands are coarse by design. Inside a band, money decides."""

    def setUp(self):
        period, _ = scale.build_book(5000)
        self.found = breaks.detect_all(period)

    def test_severity_bands_are_in_order(self):
        rank = dict((s, i) for i, s in enumerate(SEVERITY_ORDER))
        seen = [rank[b.severity] for b in self.found]
        self.assertEqual(seen, sorted(seen))

    def test_exposure_falls_monotonically_inside_each_band(self):
        by_band = {}
        for brk in self.found:
            by_band.setdefault(brk.severity, []).append(
                abs(brk.value_at_risk) if brk.value_at_risk is not None else ZERO
            )
        for severity, values in by_band.items():
            self.assertEqual(
                values, sorted(values, reverse=True),
                "%s findings are not in descending exposure order: %r"
                % (severity, values),
            )

    def test_the_first_row_is_the_largest_exposure_in_the_worst_band(self):
        """
        Note "in the worst band" rather than "in the book". The largest figure
        in the book belongs to the capped control finding below, and it is not
        at the top on purpose — see the cap test further down. Severity leads and
        exposure orders; that precedence is the design, not a limitation of it.
        """
        top = self.found[0].severity
        band = [
            b.value_at_risk for b in self.found
            if b.severity == top and b.value_at_risk is not None
        ]
        self.assertEqual(self.found[0].value_at_risk, max(band))

    def test_one_error_seen_through_three_rules_lands_together(self):
        """
        The quantity Meridian never booked is a rollforward failure and a
        disagreement with each of the other two custodians. Three findings, one
        underlying error, identical exposure — so they sort adjacently and a
        reviewer sees them as one piece of work rather than three.
        """
        isin = scale.isin_for(scale.SEEDS["rollforward"])
        rows = [i for i, b in enumerate(self.found) if b.isin == isin]
        self.assertEqual(rows, [0, 1, 2])

    def test_a_capped_control_finding_does_not_outrank_a_real_misstatement(self):
        """
        The in-flight notional across a book this size is large — larger than
        several of the genuine breaks. It is capped at medium because nothing is
        missing, and the cap is what keeps the top of the queue meaning
        "somebody's money is not where it should be".
        """
        basis = [b for b in self.found if b.kind == "STATEMENT_BASIS_MISMATCH"][0]
        rollforward = [b for b in self.found if b.kind == "QTY_ROLLFORWARD"][0]
        self.assertGreater(basis.value_at_risk, rollforward.value_at_risk)
        self.assertLess(
            self.found.index(rollforward), self.found.index(basis)
        )

    def test_unpriced_findings_sort_last_within_their_band(self):
        from _util import brk

        priced = brk(severity="high", value_at_risk="1000")
        unpriced = brk(severity="high", value_at_risk=None, isin="AAA")
        ordered = breaks._sorted([unpriced, priced])
        self.assertEqual(ordered, [priced, unpriced])


class TestRequiredSilenceAtScale(unittest.TestCase):
    """
    Every tenth instrument has a Northgate trade dated inside the quarter and
    settling outside it. Northgate reports on a trade-date basis and the other
    two do not, so all three statements are correct and all three disagree.

    The silence is a netting, not an absence — which is why the first test here
    proves the difference is really there before the second proves it is not
    reported.
    """

    def setUp(self):
        self.period, _ = scale.build_book(1500)
        self.found = breaks.detect_all(self.period)
        self.in_flight = [
            scale.isin_for(i)
            for i in range(1, len(self.period["current"][scale.MERIDIAN].positions) + 1)
            if i % scale.IN_FLIGHT_EVERY == 0
        ]

    def test_the_difference_is_real_and_large(self):
        self.assertTrue(self.in_flight)
        meridian = self.period["current"][scale.MERIDIAN].by_isin()
        northgate = self.period["current"][scale.NORTHGATE].by_isin()
        for isin in self.in_flight:
            self.assertEqual(
                northgate[isin].quantity - meridian[isin].quantity,
                scale.IN_FLIGHT_QTY,
            )

    def test_and_it_is_not_reported_as_a_break(self):
        flagged = set(
            b.isin for b in self.found if b.kind == "CROSS_CUSTODIAN_QTY"
        )
        for isin in self.in_flight:
            self.assertNotIn(isin, flagged)

    def test_the_comparison_itself_is_reported_once(self):
        control = [b for b in self.found if b.kind == "STATEMENT_BASIS_MISMATCH"]
        self.assertEqual(len(control), 1)
        self.assertEqual(
            control[0].detail["movements_in_flight"], str(len(self.in_flight))
        )

    def test_a_correctly_applied_split_stays_silent_at_every_custodian(self):
        isin = scale.isin_for(scale.SEEDS["split"])
        self.assertEqual([b for b in self.found if b.isin == isin], [])


class TestInFlightIndex(unittest.TestCase):
    """
    The fast path and the obvious path must agree everywhere, not on average.
    A faster answer that is quietly different is worse than the slow one.
    """

    def setUp(self):
        self.period, _ = scale.build_book(900)
        self.txns = self.period["txns_current"]

    def test_index_agrees_with_the_per_instrument_scan(self):
        index = breaks.in_flight_index(self.txns, [CURRENT])
        checked = 0
        for custodian, movements in self.txns.items():
            for isin in sorted(set(t.isin for t in movements)):
                scanned = breaks.in_flight_qty(movements, isin, CURRENT)
                indexed = index.get((custodian, CURRENT, isin), ZERO)
                self.assertEqual(indexed, scanned, "%s %s" % (custodian, isin))
                checked += 1
        self.assertGreater(checked, 100)

    def test_a_movement_with_no_settlement_date_is_never_in_flight(self):
        movements = [txn("X", "BUY", qty="10", settle_date=None)]
        self.assertEqual(
            breaks.in_flight_index({CUST_A: movements}, [CURRENT]), {}
        )

    def test_a_movement_settling_inside_the_period_is_not_in_flight(self):
        from datetime import date

        movements = [
            txn("X", "BUY", qty="10", trade_date=date(2026, 6, 1),
                settle_date=date(2026, 6, 3))
        ]
        self.assertEqual(
            breaks.in_flight_index({CUST_A: movements}, [CURRENT]), {}
        )

    def test_movements_in_the_same_instrument_net(self):
        from datetime import date

        movements = [
            txn("X", "BUY", qty="10", trade_date=date(2026, 6, 29),
                settle_date=date(2026, 7, 2)),
            txn("X", "SELL", qty="-4", trade_date=date(2026, 6, 30),
                settle_date=date(2026, 7, 2)),
        ]
        index = breaks.in_flight_index({CUST_A: movements}, [CURRENT])
        self.assertEqual(index[(CUST_A, CURRENT, "X")], Decimal("6"))

    def test_each_statement_date_is_indexed_separately(self):
        from datetime import date

        movements = [
            txn("X", "BUY", qty="10", trade_date=date(2026, 6, 29),
                settle_date=date(2026, 7, 2))
        ]
        index = breaks.in_flight_index({CUST_A: movements}, [PRIOR, CURRENT])
        self.assertNotIn((CUST_A, PRIOR, "X"), index)  # not yet traded in March
        self.assertEqual(index[(CUST_A, CURRENT, "X")], Decimal("10"))

    def test_empty_input_is_an_empty_index(self):
        self.assertEqual(breaks.in_flight_index({}, [CURRENT]), {})
        self.assertEqual(breaks.in_flight_index({CUST_A: []}, [CURRENT]), {})


class TestNotQuadratic(unittest.TestCase):
    """
    Doubling the book must not double the number of times a detector walks the
    movement list. It did, in all four of these, and a nine-instrument demo
    could never have shown it.
    """

    @classmethod
    def setUpClass(cls):
        cls.small = _scan_counts(1500)
        cls.large = _scan_counts(3000)

    def test_the_two_books_really_are_different_sizes(self):
        self.assertEqual(self.large["instruments"], self.small["instruments"] * 2)

    def test_quantity_rollforward_scans_a_constant_number_of_times(self):
        self.assertEqual(
            self.large["qty_rollforward"], self.small["qty_rollforward"]
        )

    def test_position_disappeared_scans_a_constant_number_of_times(self):
        self.assertEqual(
            self.large["position_disappeared"], self.small["position_disappeared"]
        )

    def test_cost_basis_drift_scans_a_constant_number_of_times(self):
        self.assertEqual(
            self.large["cost_basis_drift"], self.small["cost_basis_drift"]
        )

    def test_cross_custodian_scans_a_constant_number_of_times(self):
        """
        The worst of the four. Two of the three custodian pairs are on different
        statement bases, so before the index this asked "what is in flight here?"
        once per instrument per pair, and answered it by walking every movement
        the custodian reported.
        """
        self.assertEqual(
            self.large["cross_custodian_qty"], self.small["cross_custodian_qty"]
        )

    def test_the_counts_are_small_in_absolute_terms(self):
        """
        Constant is necessary but not sufficient — a constant of two hundred
        would also be constant. These are single-digit.
        """
        for key in ("qty_rollforward", "position_disappeared",
                    "cost_basis_drift", "cross_custodian_qty"):
            self.assertLessEqual(self.small[key], 8, key)

    def test_the_harness_would_notice_a_quadratic(self):
        """
        The counter proves nothing unless it can count. A deliberately quadratic
        scan over the same list must produce a count that grows with the book.
        """
        def quadratic(prior, current, txns):
            for pos in current.positions:
                [t for t in txns if t.isin == pos.isin]

        counts = []
        for size in (300, 600):
            period, _ = scale.build_book(size)
            counted = _CountingList(period["txns_current"][scale.MERIDIAN])
            quadratic(
                period["prior"][scale.MERIDIAN],
                period["current"][scale.MERIDIAN],
                counted,
            )
            counts.append(counted.scans)
        self.assertEqual(counts[1], counts[0] * 2)


class TestManyCitations(unittest.TestCase):
    """
    A finding with more sources than the prose prints.

    The templates summarise citations, and the summary used to end `(+27 more)`
    — a figure the prose layer worked out by subtraction, which the
    invented-figure guard is right to reject and did. On the demo book no
    finding ever has more than three distinct citations, so this crashed nothing
    until a book arrived that did. It is the failure a scale test exists to find:
    not slow, just fatal, and unreachable from the data everybody looks at.
    """

    def _break_with(self, n):
        from _util import brk

        return brk(
            citations=[
                prov(file="meridian_transactions_2026Q2.csv", line=i, excerpt="row %d" % i)
                for i in range(1, n + 1)
            ],
            detail={
                "opening_quantity": "1,000",
                "reported_quantity": "1,200",
                "transaction_delta": "150",
                "unexplained_quantity": "50",
                "value_at_risk": "USD 12,500.00",
            },
        )

    def test_a_finding_with_thirty_sources_renders_prose_that_passes_the_guard(self):
        brk = self._break_with(30)
        narrative, fix = deterministic_writer(brk)
        check_prose(brk, narrative, fix)  # must not raise

    def test_the_prose_names_the_total_not_the_remainder(self):
        brk = self._break_with(30)
        narrative, _fix = deterministic_writer(brk)
        self.assertIn("30 sources in all", narrative)
        self.assertNotIn("27", narrative)

    def test_a_finding_with_few_sources_still_lists_them_all_untruncated(self):
        brk = self._break_with(2)
        narrative, _fix = deterministic_writer(brk)
        self.assertIn("meridian_transactions_2026Q2.csv:1", narrative)
        self.assertIn("meridian_transactions_2026Q2.csv:2", narrative)
        self.assertNotIn("sources in all", narrative)

    def test_the_guard_still_rejects_a_figure_the_desk_did_not_compute(self):
        """
        Widening the allowlist to the source count must not have widened it to
        anything else. This is the same hostile figure the explain tests use.
        """
        brk = self._break_with(30)
        with self.assertRaises(InventedFigureError):
            check_prose(brk, "Exposure is EUR 9,999,999.00 on this position.")


class TestPipelineOverAnInMemoryBook(unittest.TestCase):
    """
    `build(period=...)` exists so the harness can drive the real pipeline over a
    book that was never written to disk. If it were a different code path the
    measurements would be of the harness rather than the system.
    """

    def test_the_report_is_assembled_from_a_supplied_period(self):
        import os
        import tempfile

        import build as build_mod
        from explain import deterministic_writer as writer

        period, expected = scale.build_book(600)
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        try:
            report = build_mod.build(
                period=period, out_path=path, writer=writer, quiet=True
            )
        finally:
            os.remove(path)

        self.assertEqual(report["summary"]["total_breaks"], len(expected))
        self.assertEqual(report["coverage"]["positions_examined"], 599)
        self.assertEqual(report["summary"]["narrative_fallbacks"], 0)

    def test_the_report_publishes_the_clean_instruments_it_examined(self):
        import os
        import tempfile

        import build as build_mod
        from explain import deterministic_writer as writer

        period, _ = scale.build_book(600)
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        try:
            report = build_mod.build(
                period=period, out_path=path, writer=writer, quiet=True
            )
        finally:
            os.remove(path)

        clean = report["coverage"]["instruments_clean"]
        flagged = set(b.isin for b in report["breaks"] if b.isin)
        self.assertEqual(len(clean) + len(flagged), 200)
        for row in clean:
            self.assertNotIn(row["isin"], flagged)


if __name__ == "__main__":
    unittest.main()
