"""
test_diagram.py — the architecture diagram has to still be true.

A diagram in a README is documentation with a shorter half-life than the rest of
it. Prose that goes stale reads oddly and somebody fixes it; a picture that goes
stale keeps looking authoritative. This one names modules, functions, rules and
figures, so every one of those is checked against the code and against the
report the pipeline actually produces.

The figures are the part worth having. `docs/architecture.svg` shows a queue
with USD 542,600.00 at the top, and that number is only there because the desk
computed it — if the demo book changes and the diagram does not, this fails
rather than quietly misleading the next person who reads it.
"""

from __future__ import annotations

import io
import os
import re
import unittest
from xml.etree import ElementTree

import _util  # noqa: F401  (path bootstrap)

import breaks
import model
import normalize

SVG = os.path.join(_util.ROOT, "docs", "architecture.svg")
REPORT = os.path.join(_util.ROOT, "public", "data", "breaks.json")
NS = "{http://www.w3.org/2000/svg}"

WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
}


def _tree():
    return ElementTree.parse(SVG)


def _texts():
    return [
        "".join(el.itertext()).strip()
        for el in _tree().iter(NS + "text")
    ]


def _texts_classed(*classes):
    """Text belonging to specific style classes.

    Selecting by class rather than by shape: an uppercase run matches column
    headings and severity chips as readily as a rule name, and a test that
    cannot tell those apart is testing its own regex."""
    wanted = set(classes)
    return [
        "".join(el.itertext()).strip()
        for el in _tree().iter(NS + "text")
        if (el.get("class") or "").strip() in wanted
    ]


class TestDiagramIsWellFormed(unittest.TestCase):
    def test_the_file_parses(self):
        self.assertEqual(_tree().getroot().tag, NS + "svg")

    def test_it_carries_a_title_and_a_description(self):
        root = _tree().getroot()
        self.assertTrue(root.find(NS + "title").text.strip())
        self.assertGreater(len(root.find(NS + "desc").text.strip()), 200)
        self.assertEqual(root.get("role"), "img")

    def _raw(self):
        with io.open(SVG, encoding="utf-8") as fh:
            return fh.read()

    def test_it_is_self_contained(self):
        """
        No scripts, no external images, no remote fonts. A diagram that fetches
        something is a diagram that can be different for different readers, and
        one committed to a public repository is a request a stranger's browser
        makes on their behalf.
        """
        raw = self._raw()
        self.assertNotIn("<script", raw)
        self.assertNotIn("<image", raw)
        self.assertNotIn("http://", raw.replace("http://www.w3.org/2000/svg", ""))
        self.assertNotIn("https://", raw)
        self.assertNotIn("@import", raw)

    def test_it_is_drawn_for_both_themes(self):
        raw = self._raw()
        self.assertIn("prefers-color-scheme: dark", raw)
        # An opaque ground, so the light fallback still reads as a deliberate
        # figure on a dark page rather than as black text on nothing.
        self.assertIn('class="bg"', raw)

    def test_every_marker_reference_resolves_inside_the_document(self):
        raw = self._raw()
        defined = set(re.findall(r'<marker id="([^"]+)"', raw))
        used = set(re.findall(r'marker-end="url\(#([^)]+)\)"', raw))
        self.assertTrue(used)
        self.assertEqual(used - defined, set())


class TestDiagramMatchesTheCode(unittest.TestCase):
    def setUp(self):
        self.texts = _texts()
        self.blob = " ".join(self.texts)

    def test_the_detector_count_is_the_real_one(self):
        stated = [t for t in self.texts if t.endswith("pure detectors")]
        self.assertEqual(len(stated), 1)
        word = stated[0].split()[0].lower()
        self.assertEqual(WORDS[word], len(breaks.RULES))

    def test_the_rules_it_names_are_real_rules(self):
        published = set(kind for kind, _checks in breaks.RULES)
        named = [t for t in _texts_classed("mono") if re.match(r"^[A-Z][A-Z_]+$", t)]
        self.assertGreaterEqual(len(named), 4)
        for kind in named:
            self.assertIn(kind, published)

    def test_the_rules_it_does_not_name_are_counted(self):
        """`+ nine more` has to stay honest as rules are added."""
        named = [t for t in _texts_classed("mono") if re.match(r"^[A-Z][A-Z_]+$", t)]
        more = [t for t in _texts_classed("monos") if t.endswith("more")]
        self.assertEqual(len(more), 1)
        word = more[0].replace("+", "").replace("more", "").strip().lower()
        self.assertEqual(WORDS[word] + len(named), len(breaks.RULES))

    def test_the_parsers_it_names_exist(self):
        for name in ("parse_meridian_positions", "parse_bhp_positions",
                     "parse_mt535", "parse_mt536"):
            self.assertIn(name, self.blob, "diagram no longer names %s" % name)
            self.assertTrue(hasattr(normalize, name), "%s is gone" % name)

    def test_the_model_classes_it_names_exist(self):
        for name in ("Position", "Transaction", "CorporateAction", "Provenance"):
            self.assertIn(name, self.blob)
            self.assertTrue(hasattr(model, name))

    def test_the_prose_fields_it_names_are_the_only_two_the_model_may_write(self):
        self.assertIn("narrative", self.blob)
        self.assertIn("proposed_fix", self.blob)
        import explain

        self.assertEqual(sorted(explain.PROSE_FIELDS), ["narrative", "proposed_fix"])
        for field in explain.PROSE_FIELDS:
            self.assertIn(field, self.blob)

    def test_the_custodians_it_names_are_the_ones_the_pipeline_loads(self):
        for name in (normalize.MERIDIAN, normalize.BHP, normalize.NORTHGATE):
            # The diagram drops the legal-entity suffix on one and keeps it on
            # another, so match on the distinctive word rather than the whole
            # string — the claim is that the diagram shows these three
            # custodians, not that it formats their names a particular way.
            head = name.split()[0]
            self.assertIn(head, self.blob, "diagram no longer shows %s" % name)


class TestDiagramFiguresAreTheComputedOnes(unittest.TestCase):
    """
    The numbers in the picture are the numbers in the report, or this fails.

    This is the test the diagram exists to earn. Anything else in it is a claim
    about structure that a reader can check by opening the code; the figures are
    a claim about output, and a stale one would be indistinguishable from a
    fabricated one.
    """

    def setUp(self):
        import json

        with io.open(REPORT, encoding="utf-8") as fh:
            self.report = json.load(fh)
        self.shown = [
            t for t in _texts_classed("amt")
            if re.match(r"^[A-Z]{3}\s+[\d,]+\.\d\d$", t)
        ]

    def test_the_queue_shows_four_figures(self):
        self.assertEqual(len(self.shown), 4)

    def test_every_figure_shown_is_one_the_desk_computed(self):
        from decimal import Decimal

        from money import fmt_money

        computed = set(
            fmt_money(Decimal(b["value_at_risk"]), b["value_ccy"])
            for b in self.report["breaks"]
            if b["value_at_risk"] is not None
        )
        for figure in self.shown:
            self.assertIn(
                re.sub(r"\s+", " ", figure), computed,
                "%r is in the diagram and nowhere in the report" % figure,
            )

    def test_the_two_figures_shown_as_critical_are_critical(self):
        import json  # noqa: F401
        from decimal import Decimal

        from money import fmt_money

        by_figure = {}
        for b in self.report["breaks"]:
            if b["value_at_risk"] is None:
                continue
            key = fmt_money(Decimal(b["value_at_risk"]), b["value_ccy"])
            by_figure.setdefault(key, set()).add(b["severity"])

        chips = [t.lower() for t in _texts_classed("chipTC", "chipTH", "chipTM")]
        self.assertEqual(chips, ["critical", "critical", "high", "medium"])
        for chip, figure in zip(chips, self.shown):
            self.assertIn(
                chip, by_figure[re.sub(r"\s+", " ", figure)],
                "diagram calls %s %s; the report does not" % (figure, chip),
            )

    def test_the_provenance_example_is_a_real_line_in_a_real_statement(self):
        """
        The rail along the bottom shows a citation and the line it points at.
        Both come out of a statement on disk, and this opens it and checks.
        """
        rail = [t for t in _texts_classed("monos") if ".txt:" in t]
        self.assertEqual(len(rail), 1)
        name, lineno = rail[0].rsplit(":", 1)
        path = os.path.join(_util.ROOT, "data", "statements", name)
        self.assertTrue(os.path.exists(path), "%s does not exist" % path)

        with io.open(path, encoding="utf-8") as fh:
            line = fh.readlines()[int(lineno) - 1]


        excerpt = [t for t in _texts_classed("monos") if t.startswith("US67066G1040")]
        self.assertEqual(len(excerpt), 1)
        for token in excerpt[0].split():
            self.assertIn(token, line, "%r is not on %s:%s" % (token, name, lineno))


if __name__ == "__main__":
    unittest.main()
