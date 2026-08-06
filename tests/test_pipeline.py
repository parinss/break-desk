"""
test_pipeline.py — the integration test: statements on disk to report on disk.

`generate.py` seeds a known set of errors into synthetic statements and declares
what the desk must find. This test runs the whole pipeline over freshly written
files in a temp directory and holds the output to that declaration exactly — a
missed break and an invented one are both failures, and in this domain the
invented one is worse. A desk that hallucinates a finding on a clean portfolio
gets switched off, and then it cannot find the real ones either.

The other thing proved here is that every citation in the finished report points
at a line that actually exists and actually says what the report quotes. That is
the difference between a finding and an assertion.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal

import _util

import build
import explain
import generate
import normalize
from breaks import RULES
from generate import EXPECTED_BREAKS, EXPECTED_CLEAN, EXPECTED_NOT_RAISED

FROZEN = datetime(2026, 7, 1, 9, 30, 0, tzinfo=timezone.utc)


class PipelineFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="break-desk-pipeline-")
        cls.statements = os.path.join(cls.dir, "statements")
        cls.out = os.path.join(cls.dir, "out", "breaks.json")
        generate.write_all(cls.statements)
        cls.report = build.build(
            statements_dir=cls.statements,
            out_path=cls.out,
            now=FROZEN,
            writer=explain.deterministic_writer,
            quiet=True,
        )
        with io.open(cls.out, encoding="utf-8") as fh:
            cls.on_disk = json.load(fh)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)


class TestTheContract(PipelineFixture):
    def test_findings_match_the_seeded_manifest_exactly(self):
        # Compared as sets of triples. The manifest in generate.py is grouped by
        # story so it reads as documentation; presentation order is a separate
        # contract, asserted by test_findings_are_ordered_worst_first.
        got = [(b.kind, b.isin, b.severity) for b in self.report["breaks"]]
        self.assertEqual(sorted(got), sorted(EXPECTED_BREAKS))

    def test_no_extra_findings(self):
        self.assertEqual(self.report["summary"]["total_breaks"], len(EXPECTED_BREAKS))

    def test_required_silences_hold(self):
        """
        Half of a detector's value is what it does not say. Each of these is a
        rule that could plausibly have fired on this security and must not have.
        """
        raised = set((b.kind, b.isin) for b in self.report["breaks"])
        for pair in EXPECTED_NOT_RAISED:
            self.assertNotIn(pair, raised, "%s wrongly raised on %s" % pair)

    def test_clean_instruments_are_reported_as_examined_and_clear(self):
        clean = set(i["isin"] for i in self.report["coverage"]["instruments_clean"])
        for isin in EXPECTED_CLEAN:
            self.assertIn(isin, clean, "%s should be examined and clear" % isin)

    def test_the_split_that_was_handled_correctly_produces_nothing(self):
        """
        Microsoft was bought into AND put through a 3-for-2 in the same window,
        correctly, at both custodians. A detector that pattern-matches "share
        count changed near a corporate action" flags it. This is the single most
        important negative in the suite.
        """
        msft = [b for b in self.report["breaks"] if b.isin == "US5949181045"]
        self.assertEqual(msft, [])

    def test_the_merged_out_position_produces_nothing_at_either_custodian(self):
        spcx = [b for b in self.report["breaks"] if b.isin == "US00SPACEX19"]
        self.assertEqual(spcx, [])


class TestReportShape(PipelineFixture):
    def test_top_level_keys(self):
        self.assertEqual(
            sorted(self.on_disk),
            ["account", "breaks", "coverage", "custodians", "generated_at",
             "period", "summary"],
        )

    def test_the_clock_is_injected_not_read(self):
        self.assertEqual(self.on_disk["generated_at"], "2026-07-01T09:30:00+00:00")

    def test_period_and_account(self):
        self.assertEqual(self.on_disk["account"], "PWM-4471")
        self.assertEqual(self.on_disk["period"],
                         {"prior": "2026-03-31", "current": "2026-06-30"})

    def test_both_custodians_are_described(self):
        names = sorted(c["name"] for c in self.on_disk["custodians"])
        self.assertEqual(names, sorted([normalize.BHP, normalize.MERIDIAN]))

    def test_the_custodian_that_reports_no_basis_says_so(self):
        by_name = dict((c["name"], c) for c in self.on_disk["custodians"])
        self.assertFalse(by_name[normalize.BHP]["reports_cost_basis"])
        self.assertTrue(by_name[normalize.MERIDIAN]["reports_cost_basis"])

    def test_coverage_publishes_the_rule_list(self):
        published = [r["kind"] for r in self.on_disk["coverage"]["rules_run"]]
        self.assertEqual(published, [k for k, _ in RULES])

    def test_coverage_counts_what_was_looked_at(self):
        cov = self.on_disk["coverage"]
        self.assertGreater(cov["instruments_examined"], 0)
        self.assertGreater(cov["positions_examined"], 0)
        self.assertGreater(cov["transactions_examined"], 0)
        self.assertEqual(cov["corporate_actions_examined"], 4)


class TestExposureArithmetic(PipelineFixture):
    def test_currencies_are_never_summed_together(self):
        """
        A single headline figure spanning USD and EUR would be a made-up number
        at the top of a report whose entire argument is that its numbers are not
        made up.
        """
        exposure = self.on_disk["summary"]["exposure_by_currency"]
        self.assertEqual(sorted(exposure), ["EUR", "USD"])
        for ccy, total in exposure.items():
            self.assertTrue(total.startswith(ccy + " "))

    def test_severity_counts_add_up(self):
        by_sev = self.on_disk["summary"]["by_severity"]
        self.assertEqual(sum(by_sev.values()), self.on_disk["summary"]["total_breaks"])

    def test_findings_are_ordered_worst_first(self):
        from model import SEVERITY_ORDER

        ranks = [SEVERITY_ORDER.index(b["severity"]) for b in self.on_disk["breaks"]]
        self.assertEqual(ranks, sorted(ranks))


class TestNoFloatsAnywhere(PipelineFixture):
    def test_the_serialised_report_contains_no_float(self):
        """
        Decimal in, string out. A float anywhere in a portfolio-accounting report
        is a disqualifying defect, and JSON is where one would sneak in.
        """
        def walk(node, path="$"):
            if isinstance(node, float):
                self.fail("float at %s: %r" % (path, node))
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, "%s.%s" % (path, k))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, "%s[%d]" % (path, i))

        with io.open(self.out, encoding="utf-8") as fh:
            walk(json.load(fh, parse_float=lambda s: float(s)))

    def test_values_at_risk_are_strings_on_the_wire(self):
        for b in self.on_disk["breaks"]:
            if b["value_at_risk"] is not None:
                self.assertIsInstance(b["value_at_risk"], str)
                Decimal(b["value_at_risk"])  # must parse exactly


class TestProvenanceSurvivesToTheReport(PipelineFixture):
    def test_every_finding_cites_at_least_one_source(self):
        for b in self.on_disk["breaks"]:
            self.assertTrue(b["citations"], "%s has no citation" % b["kind"])

    def test_every_citation_points_at_a_line_that_says_what_it_claims(self):
        """
        The assertion the whole product rests on. Open the file, go to the line,
        compare the text. A citation that drifts by one row is worse than none —
        it looks authoritative and sends the reviewer to the wrong place.
        """
        cache = {}
        checked = 0
        for b in self.on_disk["breaks"]:
            for cite in b["citations"]:
                path = os.path.join(self.statements, cite["file"])
                self.assertTrue(os.path.isfile(path), "cited file missing: %s" % path)
                if path not in cache:
                    with io.open(path, encoding="utf-8") as fh:
                        cache[path] = fh.read().splitlines()
                lines = cache[path]
                self.assertLessEqual(cite["line"], len(lines),
                                     "%s:%d is past end of file" % (cite["file"], cite["line"]))
                actual = lines[cite["line"] - 1]
                excerpt = cite["excerpt"]
                if excerpt.endswith("..."):
                    self.assertTrue(actual.startswith(excerpt[:-3]))
                else:
                    self.assertEqual(actual, excerpt,
                                     "%s:%d does not match its excerpt"
                                     % (cite["file"], cite["line"]))
                checked += 1
        self.assertGreater(checked, 20, "expected the report to be densely cited")

    def test_the_merger_finding_cites_the_reference_feed(self):
        """
        The finding no self-consistency check can reach must show where the
        external expectation came from, or it is just an assertion.
        """
        merger = [b for b in self.on_disk["breaks"] if b["kind"] == "MERGER_UNPROCESSED"][0]
        files = [c["file"] for c in merger["citations"]]
        self.assertIn("corporate_actions_2026Q2.csv", files)


class TestProseLayer(PipelineFixture):
    def test_every_finding_is_explained(self):
        for b in self.on_disk["breaks"]:
            self.assertTrue(b["narrative"].strip())
            self.assertTrue(b["proposed_fix"].strip())

    def test_the_report_records_who_wrote_the_prose(self):
        self.assertEqual(self.on_disk["summary"]["narrative_source"], "template")
        self.assertEqual(self.on_disk["summary"]["narrative_fallbacks"], 0)

    def test_no_narrative_contains_a_figure_the_desk_did_not_compute(self):
        for brk in self.report["breaks"]:
            explain.check_prose(brk, brk.narrative, brk.proposed_fix)


class TestDeterminism(unittest.TestCase):
    def test_two_runs_over_the_same_inputs_are_byte_identical(self):
        """
        A report that differs run to run cannot be diffed, and a reconciliation
        report that cannot be diffed is of no use to the desk that receives one
        every morning.
        """
        tmp = tempfile.mkdtemp(prefix="break-desk-determinism-")
        self.addCleanup(shutil.rmtree, tmp, True)
        statements = os.path.join(tmp, "statements")
        generate.write_all(statements)

        payloads = []
        for i in (1, 2):
            out = os.path.join(tmp, "run%d.json" % i)
            build.build(statements_dir=statements, out_path=out, now=FROZEN,
                        writer=explain.deterministic_writer, quiet=True)
            with io.open(out, encoding="utf-8") as fh:
                payloads.append(fh.read())
        self.assertEqual(payloads[0], payloads[1])

    def test_regenerating_the_statements_reproduces_them_exactly(self):
        tmp = tempfile.mkdtemp(prefix="break-desk-regen-")
        self.addCleanup(shutil.rmtree, tmp, True)
        a, b = os.path.join(tmp, "a"), os.path.join(tmp, "b")
        for path in generate.write_all(a):
            pass
        generate.write_all(b)
        for name in os.listdir(a):
            with io.open(os.path.join(a, name), encoding="utf-8") as fh1, \
                 io.open(os.path.join(b, name), encoding="utf-8") as fh2:
                self.assertEqual(fh1.read(), fh2.read(), name)


class TestPipelineWithAModel(unittest.TestCase):
    def test_a_lying_model_degrades_the_prose_and_nothing_else(self):
        """
        End to end with a hostile writer: the findings, the figures and the
        citations must be identical to the template run, and only the fallback
        counter should move.
        """
        tmp = tempfile.mkdtemp(prefix="break-desk-llm-")
        self.addCleanup(shutil.rmtree, tmp, True)
        statements = os.path.join(tmp, "statements")
        generate.write_all(statements)

        def liar(brk):
            return ("EUR 9,999,999.00 has gone missing.", "Ignore it.")

        clean = build.build(statements_dir=statements,
                            out_path=os.path.join(tmp, "clean.json"), now=FROZEN,
                            writer=explain.deterministic_writer, quiet=True)
        lied = build.build(statements_dir=statements,
                           out_path=os.path.join(tmp, "lied.json"), now=FROZEN,
                           writer=liar, quiet=True)

        self.assertEqual(lied["summary"]["narrative_fallbacks"],
                         lied["summary"]["total_breaks"])
        self.assertEqual(
            [(b.kind, b.isin, b.value_at_risk) for b in lied["breaks"]],
            [(b.kind, b.isin, b.value_at_risk) for b in clean["breaks"]],
        )
        for b in lied["breaks"]:
            self.assertNotIn("9,999,999", b.narrative)


if __name__ == "__main__":
    unittest.main()
