"""
test_explain.py — proving the containment, not asserting it.

The claim this module makes is that a language model writes two prose fields and
touches nothing else. Claims of that shape are usually enforced by the author's
good intentions and a comment. Here they are enforced at runtime, and here they
are attacked: one stub invents figures, one stub reaches past the prose fields
and edits a computed one. Both must be caught, and the failure modes must
differ — an invented figure is a bad narrative and falls back; a mutated figure
is a corrupted report and stops the run.
"""

from __future__ import annotations

import os
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal

import _util
from _util import AAPL, CUST_A, NVDA, brk, prov

import explain
from breaks import RULES
from explain import (
    InventedFigureError,
    NarrativeTamperError,
    check_prose,
    claude_writer,
    deterministic_writer,
    explain_all,
    fingerprint,
    permitted_numbers,
    writer_from_env,
)


def sample(kind="CORP_ACTION_UNAPPLIED"):
    return brk(
        kind=kind,
        severity="critical",
        isin=NVDA,
        security="NVIDIA Corporation",
        detail={
            "action": "4-for-1 split",
            "ex_date": "2026-05-18",
            "opening_quantity": "800",
            "reported_quantity": "800",
            "expected_quantity": "3,200",
            "unapplied_quantity": "2,400",
            "value_at_risk": "USD 285,360.00",
            "booked_transactions": "0",
        },
        citations=[prov(file="corporate_actions_2026Q2.csv", line=5,
                        excerpt="US67066G1040,NVDA,2026-05-18,SPLIT,4,1,,,,split")],
        value_at_risk="285360.00",
    )


class TestTemplates(unittest.TestCase):
    def test_every_rule_has_its_own_template(self):
        """A rule falling through to the generic text is a rule nobody wrote
        triage guidance for, which is the point of the prose layer."""
        for kind, _ in RULES:
            narrative, fix = deterministic_writer(brk(kind=kind))
            generic = kind.replace("_", " ").lower()
            self.assertFalse(
                narrative.startswith(generic),
                "%s falls through to the generic template" % kind,
            )
            self.assertTrue(fix.strip())

    def test_templates_carry_the_computed_figures(self):
        narrative, _ = deterministic_writer(sample())
        for figure in ("2,400", "USD 285,360.00", "3,200", "2026-05-18"):
            self.assertIn(figure, narrative)

    def test_templates_cite_their_sources(self):
        narrative, _ = deterministic_writer(sample())
        self.assertIn("corporate_actions_2026Q2.csv:5", narrative)

    def test_templates_are_held_to_the_same_rule_as_the_model(self):
        for kind, _ in RULES:
            b = brk(kind=kind)
            narrative, fix = deterministic_writer(b)
            check_prose(b, narrative, fix)  # must not raise

    def test_a_legal_suffix_does_not_produce_a_doubled_stop(self):
        narrative, _ = deterministic_writer(
            brk(kind="CROSS_CUSTODIAN_QTY", security="Apple Inc.")
        )
        self.assertNotIn("Inc..", narrative)

    def test_an_abbreviation_mid_sentence_survives(self):
        self.assertEqual(explain._tidy("Apple Inc., which holds"), "Apple Inc., which holds")
        self.assertEqual(explain._tidy("ASML Holding N.V.."), "ASML Holding N.V.")


class TestPermittedNumbers(unittest.TestCase):
    def test_every_computed_figure_is_permitted(self):
        allowed = permitted_numbers(sample())
        for token in ("2400", "3200", "800", "28536000"):
            self.assertIn(token, allowed)

    def test_locale_variants_of_one_figure_compare_equal(self):
        """1,300.000 and 1.300,000 are the same number written by two custodians.
        A check that treated them as different would reject correct prose."""
        b = brk(detail={"quantity": "1.300,000"})
        check_prose(b, "the position reads 1,300.000 units")

    def test_source_excerpts_count_as_computed(self):
        b = brk(citations=[prov(excerpt="closing price 238.90 USD")])
        check_prose(b, "the line reads 238.90")

    def test_a_restated_figure_is_rejected_even_though_it_is_true(self):
        """
        The comparison is on digits, so "1,300" and "1,300.000" are different
        tokens. That is strictness in the safe direction: the cost of rejecting
        a correct rewording is a template, and the cost of accepting an
        approximation is a figure in a client report that no source line
        supports. The system prompt tells the model to copy figures verbatim
        precisely so this never has to bite.
        """
        b = brk(detail={"reported_quantity": "1,300.000"})
        with self.assertRaises(InventedFigureError):
            check_prose(b, "the position reads 1,300 units")


class TestCheckProse(unittest.TestCase):
    def test_rejects_a_figure_the_desk_never_computed(self):
        with self.assertRaises(InventedFigureError):
            check_prose(sample(), "the shortfall is EUR 9,999,999.00")

    def test_rejects_a_plausible_derived_figure(self):
        """The dangerous invention is not a wild number — it is a reasonable one.
        Nothing in the detail says 285,361, so nothing may say it."""
        with self.assertRaises(InventedFigureError):
            check_prose(sample(), "roughly USD 285,361 is at stake")

    def test_accepts_prose_built_only_from_the_detail(self):
        check_prose(sample(), "2,400 units are missing, worth USD 285,360.00")

    def test_prose_with_no_figures_at_all_is_fine(self):
        check_prose(sample(), "The custodian has not applied the action.")


class TestExplainAll(unittest.TestCase):
    def test_default_path_needs_no_key_and_no_network(self):
        out = explain_all([sample()])
        self.assertTrue(out[0].narrative)
        self.assertTrue(out[0].proposed_fix)

    def test_a_lying_model_is_rejected_and_the_template_used(self):
        rejected = []

        def liar(b):
            return ("The shortfall is EUR 9,999,999.00.", "Do nothing.")

        out = explain_all([sample()], writer=liar,
                          on_reject=lambda b, e: rejected.append((b.key(), e)))
        self.assertEqual(len(rejected), 1)
        self.assertIsInstance(rejected[0][1], InventedFigureError)
        self.assertNotIn("9,999,999", out[0].narrative)
        self.assertEqual(out[0].narrative, deterministic_writer(sample())[0])

    def test_a_model_that_crashes_does_not_fail_the_run(self):
        rejected = []

        def broken(b):
            raise RuntimeError("connection reset")

        out = explain_all([sample()], writer=broken,
                          on_reject=lambda b, e: rejected.append(e))
        self.assertEqual(len(rejected), 1)
        self.assertTrue(out[0].narrative)

    def test_a_model_returning_junk_does_not_fail_the_run(self):
        out = explain_all([sample()], writer=lambda b: (None, None),
                          on_reject=lambda b, e: None)
        self.assertTrue(out[0].narrative)

    def test_a_writer_that_edits_a_computed_field_stops_everything(self):
        """
        Falling back would leave a report with a tampered figure in it and a
        correct-looking narrative on top. There is no safe way to continue.
        """
        def saboteur(b):
            b.detail["value_at_risk"] = "USD 1.00"
            return ("All is well.", "Nothing to do.")

        with self.assertRaises(NarrativeTamperError):
            explain_all([sample()], writer=saboteur)

    def test_a_writer_that_changes_severity_is_caught(self):
        def saboteur(b):
            b.severity = "low"
            return ("All is well.", "Nothing to do.")

        with self.assertRaises(NarrativeTamperError):
            explain_all([sample()], writer=saboteur)

    def test_a_good_model_is_used(self):
        out = explain_all([sample()],
                          writer=lambda b: ("2,400 units short.", "Call the custodian."))
        self.assertEqual(out[0].narrative, "2,400 units short.")

    def test_nothing_but_the_two_prose_fields_ever_moves(self):
        original = sample()
        before = fingerprint(original)
        out = explain_all([original],
                          writer=lambda b: ("2,400 units short.", "Call them."))
        self.assertEqual(fingerprint(out[0]), before)

    def test_the_input_break_is_not_mutated_in_place(self):
        original = sample()
        explain_all([original], writer=lambda b: ("2,400 short.", "Call."))
        self.assertEqual(original.narrative, "")


class TestFingerprint(unittest.TestCase):
    def test_prose_is_excluded(self):
        a = sample()
        b = replace(a, narrative="anything", proposed_fix="at all")
        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_a_figure_is_not(self):
        a = sample()
        b = replace(a, value_at_risk=Decimal("1.00"))
        self.assertNotEqual(fingerprint(a), fingerprint(b))

    def test_decimals_serialise_as_strings_never_floats(self):
        self.assertIn('"285360.00"', fingerprint(sample()))


class _Block(object):
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Response(object):
    def __init__(self, text):
        self.content = [_Block(text)]


class _StubMessages(object):
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Response(self.payload)


class _StubClient(object):
    def __init__(self, payload):
        self.messages = _StubMessages(payload)


class TestClaudeWriter(unittest.TestCase):
    """The client is injected, so the wire shape is testable with no key."""

    def test_parses_a_structured_response(self):
        client = _StubClient('{"narrative": "2,400 units short.", '
                             '"proposed_fix": "Call the custodian."}')
        narrative, fix = claude_writer(client=client)(sample())
        self.assertEqual(narrative, "2,400 units short.")
        self.assertEqual(fix, "Call the custodian.")

    def test_request_shape(self):
        client = _StubClient('{"narrative": "a", "proposed_fix": "b"}')
        claude_writer(client=client)(sample())
        sent = client.messages.calls[0]
        self.assertEqual(sent["model"], explain.MODEL)
        self.assertEqual(sent["thinking"], {"type": "adaptive"})
        self.assertEqual(sent["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("temperature", sent, "rejected by the API on this model")
        self.assertNotIn("budget_tokens", sent.get("thinking", {}))

    def test_the_model_is_never_sent_a_field_it_may_write(self):
        client = _StubClient('{"narrative": "a", "proposed_fix": "b"}')
        claude_writer(client=client)(sample())
        payload = client.messages.calls[0]["messages"][0]["content"]
        self.assertNotIn("narrative", payload)
        self.assertNotIn("proposed_fix", payload)

    def test_a_lying_response_is_caught_by_the_pass_not_by_the_writer(self):
        client = _StubClient('{"narrative": "EUR 9,999,999.00 is missing.", '
                             '"proposed_fix": "b"}')
        rejected = []
        out = explain_all([sample()], writer=claude_writer(client=client),
                          on_reject=lambda b, e: rejected.append(e))
        self.assertEqual(len(rejected), 1)
        self.assertNotIn("9,999,999", out[0].narrative)


class TestWriterFromEnv(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.saved)))

    def test_no_key_means_templates(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.assertIs(writer_from_env(), deterministic_writer)

    def test_the_switch_wins_over_a_present_key(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-not-a-real-key"
        os.environ["BREAK_DESK_LLM"] = "0"
        self.assertIs(writer_from_env(), deterministic_writer)

    def test_explicitly_disabled(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-not-a-real-key"
        self.assertIs(writer_from_env(enabled=False), deterministic_writer)

    def test_a_complete_report_is_produced_with_no_model_at_all(self):
        """
        The commercial claim, asserted: the model is an upgrade to the prose,
        never a dependency of the finding.
        """
        os.environ.pop("ANTHROPIC_API_KEY", None)
        out = explain_all([sample(k) for k, _ in RULES], writer=writer_from_env())
        self.assertEqual(len(out), len(RULES))
        for b in out:
            self.assertTrue(b.narrative.strip())
            self.assertTrue(b.proposed_fix.strip())


if __name__ == "__main__":
    unittest.main()
