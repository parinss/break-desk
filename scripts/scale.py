#!/usr/bin/env python3
"""
scale.py — the desk under load.

The demo book is nine instruments. A real mandate is thousands, and the question
anyone asks about a reconciliation engine is not "does it work" — it is "does it
still work on the whole book, on a Tuesday, an hour before the client call".

This builds a synthetic book of arbitrary size across the three custodians,
seeds a fixed and known set of breaks into it, runs the pipeline, and reports
where the time went and where the seeded findings landed in the queue.

    python3 scripts/scale.py                    # ~5,000 position lines
    python3 scripts/scale.py --positions 15000

## Two questions, not one

**Does it finish?** Throughput is the obvious question and the less interesting
one. The detectors compare holdings pairwise across custodians and roll
transactions against positions, and both of those have a quadratic sitting one
careless line away — a filter over the whole transaction list inside a loop over
every position is the natural way to write the code and is O(P x T).

**Does the queue still mean anything?** This is the question that matters. Ten
findings sort themselves. Two hundred do not, and an operations team that works
a queue top-down will work whatever the sort key put at the top. If severity
ranking does not float the largest exposures up through a book this size, the
report is a list rather than a queue.

## What this measures, and what it does not

Measured: detection, the prose layer, report assembly, serialisation.

Not measured: parsing. The book is built in memory rather than written out and
read back. The parsers are single-pass over lines and linear in file length, but
the honest reason they are excluded is more mundane: they refuse an ISIN that is
not in the security master, and growing a nine-row master to five thousand rows
to time a linear loop would be measuring the harness rather than the system.

Because the book is never written to disk, its provenance carries a
`synthetic://` scheme rather than a path — nothing here can be mistaken for a
file, and nothing will go looking for one. Provenance that points at a real file
and a real line is proved on the demo book instead, by the integration test that
opens every citation in the finished report and checks the line says what the
report claims it says.

## The manifest

`build_book` returns the book *and* the list of breaks that are in it, the same
discipline `generate.py` follows for the demo: a missed break and an invented
one are both failures. At this size that matters more, not less — nobody
eyeballs five thousand positions to check the engine's homework, so the expected
answer has to be written down before the run rather than read off after it.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from datetime import date
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import breaks  # noqa: E402
import build as build_mod  # noqa: E402
import explain  # noqa: E402
from model import (  # noqa: E402
    BASIS_SETTLED,
    BASIS_TRADE,
    CorporateAction,
    FxRate,
    Position,
    Provenance,
    Snapshot,
    Transaction,
)
from money import fmt_money, q_money  # noqa: E402
from normalize import BHP, MERIDIAN, NORTHGATE  # noqa: E402

ACCOUNT = "SCALE-0001"
PRIOR = date(2026, 3, 31)
CURRENT = date(2026, 6, 30)

# Custodian, statement basis, base currency, reports cost basis.
# The bases differ on purpose: two settled, one trade-date, which is what puts
# the settlement-netting path — the expensive one — on the critical path.
CUSTODIANS = [
    (MERIDIAN, BASIS_SETTLED, "USD", True),
    (BHP, BASIS_SETTLED, "EUR", False),
    (NORTHGATE, BASIS_TRADE, "USD", False),
]

SLUG = {
    MERIDIAN: "meridian",
    BHP: "bhp",
    NORTHGATE: "northgate",
}

USD_EUR = Decimal("0.92")

# Every instrument is bought once in the window, at every custodian.
BUY_QTY = Decimal("20")
BUY_DATE = date(2026, 5, 15)
BUY_SETTLE = date(2026, 5, 19)

# Every tenth instrument also has a Northgate trade dated inside the quarter and
# settling outside it. Northgate reports on a trade-date basis, so it carries
# these and the other two do not — a real difference between correct statements,
# and the silence the quantity rule has to hold on to at scale.
IN_FLIGHT_EVERY = 10
IN_FLIGHT_QTY = Decimal("40")
IN_FLIGHT_TRADE = date(2026, 6, 29)
IN_FLIGHT_SETTLE = date(2026, 7, 2)

FEE = Decimal("6250.00")

# Instrument indices carrying a seeded error. Small, fixed, and independent of
# book size, so the same ten findings must surface out of five thousand
# positions or fifty thousand — which is the ranking claim, stated as data.
SEEDS = {
    "basis_drift": 3,
    "price_divergence": 5,
    "rollforward": 7,
    "disappeared": 11,
    "cross_custodian": 13,
    "split": 17,
}
MIN_INSTRUMENTS = 40

# Prices for the seeded instruments, chosen so the exposures land in different
# severity bands and in a known order.
SEED_PRICE = {
    SEEDS["basis_drift"]: Decimal("150.00"),
    SEEDS["price_divergence"]: Decimal("250.00"),
    SEEDS["rollforward"]: Decimal("900.00"),
    SEEDS["disappeared"]: Decimal("700.00"),
    SEEDS["cross_custodian"]: Decimal("410.00"),
    SEEDS["split"]: Decimal("120.00"),
}

SHORTFALL = Decimal("500")       # shares Meridian never booked
UNILATERAL_BUY = Decimal("250")  # shares only Northgate bought
BASIS_DRIFT = Decimal("12000.00")
PRICE_GAP = Decimal("6.00")
SPLIT_DATE = date(2026, 5, 20)


def isin_for(i):
    # type: (int) -> str
    """
    `USSCALE00033`. Twelve characters, ISIN-shaped, and unmistakably not one.

    Same reasoning as the SpaceX ISIN in the security master: an invented
    identifier that could be confused with a real instrument is a liability in a
    repository anyone can clone.
    """
    if not 1 <= i <= 9999:
        raise ValueError("instrument index out of range: %d" % i)
    return "USSCALE%04d%d" % (i, i % 10)


def _prov(custodian, doc, line, excerpt):
    # type: (str, str, int, str) -> Provenance
    return Provenance(
        file="synthetic://%s/%s" % (SLUG[custodian], doc),
        line=line,
        excerpt=excerpt,
    )


def _price(i):
    # type: (int) -> Decimal
    return SEED_PRICE.get(i, Decimal(10 + (i % 300)))


def _opening_qty(i):
    # type: (int) -> Decimal
    return Decimal(100 + (i % 40) * 10)


def _unit_cost(i):
    # type: (int) -> Decimal
    return _price(i) * Decimal("0.8")


def _position(custodian, base_ccy, reports_basis, as_of, doc, i, qty, price, cost_basis):
    # type: (str, str, bool, date, str, int, Decimal, Decimal, object) -> Position
    isin = isin_for(i)
    market_value = q_money(qty * price)
    return Position(
        as_of=as_of,
        custodian=custodian,
        account=ACCOUNT,
        isin=isin,
        quantity=qty,
        price=price,
        ccy="USD",
        market_value=market_value,
        source=_prov(
            custodian, doc, i,
            "%s,%s,%s,%s" % (isin, qty, price, market_value),
        ),
        cost_basis=q_money(cost_basis) if reports_basis else None,
        # Only meaningful when the holding is denominated in something other than
        # the statement's base currency, which for this book is BHP and only BHP.
        reported_base_value=(
            q_money(market_value * USD_EUR) if base_ccy != "USD" else None
        ),
    )


def _txn(custodian, doc, line, isin, kind, qty, amount, trade, settle):
    # type: (str, str, int, str, str, Decimal, Decimal, date, object) -> Transaction
    return Transaction(
        trade_date=trade,
        custodian=custodian,
        account=ACCOUNT,
        isin=isin,
        kind=kind,
        quantity=qty,
        amount=amount,
        ccy="USD",
        source=_prov(custodian, doc, line, "%s,%s,%s,%s" % (trade, isin, kind, qty)),
        settle_date=settle,
    )


def build_book(positions=5000):
    # type: (int) -> tuple
    """
    Build a book of roughly `positions` position lines across three custodians.

    Returns `(period, manifest)`, where `period` is exactly the structure
    `normalize.load_period` produces — so the detectors cannot tell the
    difference, which is the point — and `manifest` is the sorted list of
    `(kind, isin)` pairs that must come out the other end.
    """
    instruments = max(MIN_INSTRUMENTS, positions // len(CUSTODIANS))
    if instruments > 9999:
        raise ValueError(
            "book of %d positions needs %d instruments; the identifier scheme "
            "holds 9,999" % (positions, instruments)
        )

    prior = {}
    current = {}
    txns_prior = {}
    txns_current = {}

    split_isin = isin_for(SEEDS["split"])
    actions = [
        CorporateAction(
            isin=split_isin,
            ex_date=SPLIT_DATE,
            kind="SPLIT",
            ratio_num=2,
            ratio_den=1,
            description="2-for-1 common stock split",
            source=Provenance(
                file="synthetic://reference/corporate_actions",
                line=1,
                excerpt="%s,2026-05-20,SPLIT,2,1" % split_isin,
            ),
        )
    ]

    for custodian, basis, base_ccy, reports_basis in CUSTODIANS:
        prior_positions = []
        current_positions = []
        movements = []

        for i in range(1, instruments + 1):
            isin = isin_for(i)
            price = _price(i)
            open_qty = _opening_qty(i)
            unit = _unit_cost(i)
            open_basis = open_qty * unit

            # --- the prior statement, clean everywhere -----------------------
            prior_positions.append(
                _position(
                    custodian, base_ccy, reports_basis, PRIOR,
                    "positions_2026-03-31", i, open_qty, price, open_basis,
                )
            )

            # --- the window's activity ---------------------------------------
            bought = BUY_QTY
            buy_cost = BUY_QTY * price
            skip_position = False

            if i == SEEDS["disappeared"] and custodian == BHP:
                # The holding leaves the statement and the movement file says
                # nothing about it. That is the break; a buy sitting in the
                # activity file for a position that vanished would be a second,
                # unrelated oddity muddying a seeded one.
                skip_position = True
                bought = None
            else:
                movements.append(
                    _txn(
                        custodian, "movements_2026Q2", i, isin, "BUY",
                        BUY_QTY, -buy_cost, BUY_DATE, BUY_SETTLE,
                    )
                )

            if skip_position:
                continue

            qty = open_qty + bought
            basis = open_basis + buy_cost

            if i == SEEDS["split"]:
                movements.append(
                    _txn(
                        custodian, "movements_2026Q2", i, isin, "SPLIT",
                        qty, Decimal("0"), SPLIT_DATE, None,
                    )
                )
                qty = qty * 2  # basis is untouched by a split, by definition

            if i % IN_FLIGHT_EVERY == 0 and custodian == NORTHGATE:
                movements.append(
                    _txn(
                        custodian, "movements_2026Q2", i, isin, "BUY",
                        IN_FLIGHT_QTY, -(IN_FLIGHT_QTY * price),
                        IN_FLIGHT_TRADE, IN_FLIGHT_SETTLE,
                    )
                )
                qty += IN_FLIGHT_QTY
                basis += IN_FLIGHT_QTY * price

            if i == SEEDS["rollforward"] and custodian == MERIDIAN:
                qty -= SHORTFALL  # booked nowhere, explained by nothing

            if i == SEEDS["cross_custodian"] and custodian == NORTHGATE:
                # Booked properly here and nowhere else: the rollforward is
                # clean and only a cross-custodian comparison can see it.
                movements.append(
                    _txn(
                        custodian, "movements_2026Q2", i, isin, "BUY",
                        UNILATERAL_BUY, -(UNILATERAL_BUY * price),
                        BUY_DATE, BUY_SETTLE,
                    )
                )
                qty += UNILATERAL_BUY
                basis += UNILATERAL_BUY * price

            if i == SEEDS["basis_drift"] and custodian == MERIDIAN:
                basis += BASIS_DRIFT

            if i == SEEDS["price_divergence"] and custodian == BHP:
                price = price + PRICE_GAP

            current_positions.append(
                _position(
                    custodian, base_ccy, reports_basis, CURRENT,
                    "positions_2026-06-30", i, qty, price, basis,
                )
            )

        fx = []
        if base_ccy != "USD":
            fx = [
                FxRate(
                    as_of=CURRENT,
                    custodian=custodian,
                    pair="USD/%s" % base_ccy,
                    rate=USD_EUR,
                    source=_prov(custodian, "positions_2026-06-30", 0, "USD/EUR 0,92"),
                )
            ]

        prior[custodian] = Snapshot(
            as_of=PRIOR, custodian=custodian, account=ACCOUNT,
            base_ccy=base_ccy, basis=basis_for(custodian),
            positions=prior_positions, fx_rates=fx,
        )
        current[custodian] = Snapshot(
            as_of=CURRENT, custodian=custodian, account=ACCOUNT,
            base_ccy=base_ccy, basis=basis_for(custodian),
            positions=current_positions, fx_rates=fx,
        )

        # A management fee last quarter at every custodian, and this quarter at
        # every custodian but one. An absence is invisible to every rule that
        # compares numbers; it has to be looked for on purpose.
        txns_prior[custodian] = [
            _txn(custodian, "movements_2026Q1", 1, "", "FEE",
                 Decimal("0"), -FEE, date(2026, 3, 31), None)
        ]
        if custodian != BHP:
            movements.append(
                _txn(custodian, "movements_2026Q2", 0, "", "FEE",
                     Decimal("0"), -FEE, CURRENT, None)
            )
        txns_current[custodian] = movements

    period = {
        "prior": prior,
        "current": current,
        "txns_prior": txns_prior,
        "txns_current": txns_current,
        "actions": actions,
    }
    return period, manifest()


def basis_for(custodian):
    # type: (str) -> str
    for name, basis, _ccy, _cb in CUSTODIANS:
        if name == custodian:
            return basis
    raise KeyError(custodian)


def manifest():
    # type: () -> list
    """
    Every finding the seeded book must produce, and nothing else.

    Two of the seeds surface more than once, and that is correct rather than
    untidy. A quantity Meridian never booked is a rollforward failure *and* a
    disagreement with each of the other two custodians; the desk is looking at
    one error through three windows, and suppressing two of them would hide the
    fact that the windows agree.
    """
    return sorted([
        ("COST_BASIS_DRIFT", isin_for(SEEDS["basis_drift"])),
        # Twice, and correctly. The custodian carrying the odd price disagrees
        # with each of the other two, and the desk has no basis for nominating
        # one of the three as the right one — reporting a single finding would
        # mean silently electing a reference price.
        ("PRICE_DIVERGENCE", isin_for(SEEDS["price_divergence"])),
        ("PRICE_DIVERGENCE", isin_for(SEEDS["price_divergence"])),
        ("QTY_ROLLFORWARD", isin_for(SEEDS["rollforward"])),
        ("CROSS_CUSTODIAN_QTY", isin_for(SEEDS["rollforward"])),
        ("CROSS_CUSTODIAN_QTY", isin_for(SEEDS["rollforward"])),
        ("POSITION_DISAPPEARED", isin_for(SEEDS["disappeared"])),
        ("CROSS_CUSTODIAN_QTY", isin_for(SEEDS["cross_custodian"])),
        ("CROSS_CUSTODIAN_QTY", isin_for(SEEDS["cross_custodian"])),
        ("MISSING_FEE_ACCRUAL", ""),
        ("STATEMENT_BASIS_MISMATCH", ""),
    ])


def found_manifest(found):
    # type: (list) -> list
    return sorted((b.kind, b.isin) for b in found)


def run(positions=5000, out_path=None):
    # type: (int, str) -> dict
    """Build, reconcile, and time it. Returns a dict of results."""
    started = time.perf_counter()
    period, expected = build_book(positions)
    built = time.perf_counter()

    found = breaks.detect_all(period)
    detected = time.perf_counter()

    tmp = None
    if out_path is None:
        handle, out_path = tempfile.mkstemp(prefix="scale-", suffix=".json")
        os.close(handle)
        tmp = out_path
    try:
        report = build_mod.build(
            period=period,
            out_path=out_path,
            writer=explain.deterministic_writer,
            quiet=True,
        )
        piped = time.perf_counter()
        size = os.path.getsize(out_path)
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)

    return {
        "instruments": len(period["current"][MERIDIAN].positions),
        "positions": sum(len(s.positions) for s in period["current"].values()),
        "transactions": sum(len(t) for t in period["txns_current"].values()),
        "build_s": built - started,
        "detect_s": detected - built,
        "pipeline_s": piped - detected,
        "report_bytes": size,
        "found": found,
        "expected": expected,
        "report": report,
    }


def main(argv=None):
    # type: (list) -> int
    parser = argparse.ArgumentParser(description="Run the desk over a large book.")
    parser.add_argument("--positions", type=int, default=5000)
    parser.add_argument("--top", type=int, default=10, help="queue rows to print")
    args = parser.parse_args(argv)

    result = run(args.positions)
    found = result["found"]

    out = sys.stdout.write
    out("book        %d instruments, %d position lines, %d movements\n"
        % (result["instruments"], result["positions"], result["transactions"]))
    out("generate    %6.2fs\n" % result["build_s"])
    out("detect      %6.2fs   (measured on its own pass)\n" % result["detect_s"])
    out("pipeline    %6.2fs   (detect + prose + report + %s)\n"
        % (result["pipeline_s"], _bytes(result["report_bytes"])))
    out("\n")

    ok = found_manifest(found) == result["expected"]
    out("findings    %d, manifest %s\n" % (len(found), "matched" if ok else "MISMATCHED"))
    if not ok:
        out("  expected  %r\n" % (result["expected"],))
        out("  found     %r\n" % (found_manifest(found),))

    out("\nqueue, top %d of %d:\n" % (min(args.top, len(found)), len(found)))
    for brk in found[: args.top]:
        # The Break's own figure, not `detail["value_at_risk"]` — account-level
        # findings name their exposure something else, and printing the sort key
        # is the only way this column can be checked against the order it claims
        # to be in.
        out("  %-9s %-24s %14s  %s\n"
            % (brk.severity,
               brk.kind,
               fmt_money(brk.value_at_risk, brk.value_ccy)
               if brk.value_at_risk is not None else "-",
               brk.isin or "(account-level)"))
    return 0 if ok else 1


def _bytes(n):
    # type: (int) -> str
    return "%.1f MB" % (n / 1048576.0) if n >= 1048576 else "%.0f kB" % (n / 1024.0)


if __name__ == "__main__":
    sys.exit(main())
