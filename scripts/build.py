#!/usr/bin/env python3
"""
build.py — the pipeline. Statements in, one JSON file out.

    load -> detect -> explain -> write public/data/breaks.json

The clock, the input directory, the output path and the prose writer are all
parameters with defaults, so the integration test drives the whole pipeline into
a temp directory with a frozen timestamp and asserts on the bytes. A pipeline
that can only be run one way can only be tested one way.

The report carries a coverage block as well as a findings list. A reviewer's
first question is never "what did you find" — it is "what did you look for, and
what did you look at and clear". A findings list on its own cannot be falsified;
with the rule list and the clean-instrument list beside it, it can.

Run:  python3 scripts/build.py
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal

import breaks
import explain
import normalize
import securities
from model import SEVERITY_ORDER, to_json
from money import ZERO, fmt_money

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATEMENTS = os.path.join(ROOT, "data", "statements")
OUT = os.path.join(ROOT, "public", "data", "breaks.json")


def _exposure_by_ccy(found):
    # type: (list) -> OrderedDict
    """
    Total exposure, kept separate per currency.

    Never summed across currencies. A single figure spanning USD and EUR would
    be a made-up number sitting at the top of a report whose entire argument is
    that its numbers are not made up.
    """
    totals = {}
    for brk in found:
        if brk.value_at_risk is None or not brk.value_ccy:
            continue
        totals.setdefault(brk.value_ccy, ZERO)
        totals[brk.value_ccy] += abs(brk.value_at_risk)
    return OrderedDict(
        (ccy, fmt_money(totals[ccy], ccy)) for ccy in sorted(totals)
    )


def build(statements_dir=None, out_path=None, now=None, writer=None, quiet=False):
    # type: (str, str, datetime, object, bool) -> dict
    """Run the pipeline and write the report. Returns the report as a dict."""
    statements_dir = statements_dir or STATEMENTS
    out_path = out_path or OUT
    now = now or datetime.now(timezone.utc)

    period = normalize.load_period(statements_dir)
    found = breaks.detect_all(period)

    fallbacks = []
    writer = writer if writer is not None else explain.writer_from_env()
    found = explain.explain_all(
        found, writer=writer, on_reject=lambda b, e: fallbacks.append((b.key(), str(e)))
    )

    current = period["current"]
    prior = period["prior"]
    txns_current = period["txns_current"]
    actions = period["actions"]

    custodians = []
    all_isins = set()
    positions_examined = 0
    for name in sorted(current):
        snap = current[name]
        positions_examined += len(snap.positions)
        all_isins.update(p.isin for p in snap.positions)
        custodians.append(
            OrderedDict([
                ("name", name),
                ("account", snap.account),
                ("base_currency", snap.base_ccy),
                ("positions", len(snap.positions)),
                ("transactions", len(txns_current.get(name, []))),
                ("reports_cost_basis", any(p.cost_basis is not None for p in snap.positions)),
                ("source_file", snap.positions[0].source.file if snap.positions else ""),
            ])
        )

    # Instruments held at the start of the period were examined too — the
    # disappearance and merger rules are about precisely those. Counting only
    # the closing statements would leave a holding that was correctly merged out
    # of existence invisible: not a finding, and absent from the cleared list as
    # well, which is the one place a reviewer looks to confirm somebody checked.
    for name in sorted(prior):
        all_isins.update(p.isin for p in prior[name].positions)

    flagged = set(b.isin for b in found if b.isin)
    clean = sorted(all_isins - flagged)

    by_severity = OrderedDict(
        (s, sum(1 for b in found if b.severity == s)) for s in SEVERITY_ORDER
    )

    report = OrderedDict([
        ("generated_at", now.replace(microsecond=0).isoformat()),
        ("account", custodians[0]["account"] if custodians else ""),
        ("period", OrderedDict([
            ("prior", min(s.as_of for s in prior.values()).isoformat()),
            ("current", min(s.as_of for s in current.values()).isoformat()),
        ])),
        ("custodians", custodians),
        ("summary", OrderedDict([
            ("total_breaks", len(found)),
            ("by_severity", by_severity),
            ("exposure_by_currency", _exposure_by_ccy(found)),
            ("narrative_source",
             "template" if writer is explain.deterministic_writer else "model"),
            ("narrative_fallbacks", len(fallbacks)),
        ])),
        ("coverage", OrderedDict([
            ("instruments_examined", len(all_isins)),
            ("positions_examined", positions_examined),
            ("transactions_examined", sum(len(v) for v in txns_current.values())),
            ("corporate_actions_examined", len(actions)),
            ("rules_run", [
                OrderedDict([("kind", k), ("checks", d)]) for k, d in breaks.RULES
            ]),
            ("instruments_clean", [
                OrderedDict([("isin", i), ("name", securities.name_for(i))]) for i in clean
            ]),
        ])),
        ("breaks", found),
    ])

    payload = to_json(report)
    out_dir = os.path.dirname(out_path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    with io.open(out_path, "w", encoding="utf-8") as fh:
        fh.write(payload + "\n")

    if not quiet:
        sys.stdout.write(
            "%d breaks (%s) -> %s\n"
            % (len(found),
               ", ".join("%d %s" % (n, s) for s, n in by_severity.items() if n),
               os.path.relpath(out_path, ROOT))
        )
        for ccy, total in report["summary"]["exposure_by_currency"].items():
            sys.stdout.write("  exposure %s\n" % total)
        if fallbacks:
            sys.stdout.write(
                "  %d narrative(s) fell back to templates:\n" % len(fallbacks)
            )
            for key, reason in fallbacks:
                sys.stdout.write("    %s: %s\n" % (key, reason))

    return report


def main(argv=None):
    # type: (list) -> int
    parser = argparse.ArgumentParser(description="Build the break report.")
    parser.add_argument("--statements", default=STATEMENTS)
    parser.add_argument("--out", default=OUT)
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="force deterministic templates even when an API key is present",
    )
    args = parser.parse_args(argv)

    build(
        statements_dir=args.statements,
        out_path=args.out,
        writer=explain.deterministic_writer if args.no_llm else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
