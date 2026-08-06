#!/usr/bin/env python3
"""
breaks.py — the detectors.

Every function here is pure: canonical objects in, `Break` objects out. No file
access, no clock, no network, no logging, no global state. That is not
architectural taste — it is what makes the rules testable one at a time, which is
the only way to have any confidence in the half of a detector's behaviour that
matters most: its silence.

A reconciliation desk that flags everything is worse than none at all. It gets
switched off in a fortnight and the real break sails through in week three. So
each rule is tested twice — once that it fires on the condition it names, once
that it stays quiet on the neighbouring case that merely resembles it.

## Severity

Severity is computed from value at risk, never assigned per rule. A twelve-share
mismatch and a twenty-four-hundred-share mismatch are not the same finding just
because the same rule produced them, and an operations team triaging by rule name
rather than by exposure is triaging by the wrong key.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional, Sequence

import corpactions
import securities
from model import (
    BASIS_TRADE,
    Break,
    CorporateAction,
    Position,
    Snapshot,
    Transaction,
)
from money import ZERO, fmt_money, fmt_qty, pct_diff, q_money, q_qty, q_rate

# Value-at-risk bands, in the account's base currency. Exposed as module state so
# a client with a different materiality policy changes one table, not six rules.
SEVERITY_BANDS = [
    (Decimal("100000"), "critical"),
    (Decimal("5000"), "high"),
    (Decimal("500"), "medium"),
]

# A single line's currency conversion can round by a cent. It cannot round by a
# unit of currency, so anything at or below this is arithmetic, not a break.
FX_TOLERANCE = Decimal("1.00")
QTY_TOLERANCE = Decimal("0.001")
BASIS_TOLERANCE = Decimal("0.01")
PRICE_TOLERANCE_PCT = Decimal("0.50")

# What the desk checks, in plain English. Published in the report because a
# reviewer's first question is never "what did you find" — it is "what did you
# look for", and a findings list with no rule list is unfalsifiable.
RULES = [
    ("QTY_ROLLFORWARD",
     "Opening quantity plus every booked movement must equal closing quantity."),
    ("POSITION_DISAPPEARED",
     "A holding that left the statement must be accounted for by the activity file."),
    ("CORP_ACTION_UNAPPLIED",
     "Every corporate action in the reference feed must be applied by every custodian."),
    ("CORP_ACTION_WRONG_RATIO",
     "An applied action must move the share count by the declared ratio."),
    ("CORP_ACTION_BASIS_CORRUPTED",
     "A split divides basis per share and leaves the total unchanged."),
    ("MERGER_UNPROCESSED",
     "Both legs of a merger must be processed: target removed, acquirer credited."),
    ("IDENTIFIER_STALE",
     "A custodian must not report a security under a superseded ticker."),
    ("FX_INCONSISTENT",
     "A statement's converted values must agree with the rates it prints."),
    ("COST_BASIS_DRIFT",
     "Cost basis rolled through the period's activity must match what is reported."),
    ("CROSS_CUSTODIAN_QTY",
     "Two custodians reporting one mandate must report the same share count, "
     "once both are put on the same statement basis."),
    ("STATEMENT_BASIS_MISMATCH",
     "Custodians reporting one mandate must be compared on one statement basis."),
    ("PRICE_DIVERGENCE",
     "Two custodians must price the same instrument alike on the same date."),
    ("MISSING_FEE_ACCRUAL",
     "A fee charged in the prior period must recur or be explained."),
]


def severity_for(value_at_risk):
    # type: (Decimal) -> str
    v = abs(Decimal(value_at_risk))
    for threshold, label in SEVERITY_BANDS:
        if v >= threshold:
            return label
    return "low"


def _capped_severity(value_at_risk, ceiling):
    # type: (Decimal, str) -> str
    """
    Severity by exposure, but never worse than `ceiling`.

    For findings where the whole position is notionally at risk but the failure
    is a control weakness rather than a misstatement. Without the cap a stale
    ticker on a large holding outranks a genuinely missing half-million of stock,
    and the queue stops meaning anything.
    """
    from model import SEVERITY_ORDER

    computed = severity_for(value_at_risk)
    if SEVERITY_ORDER.index(computed) < SEVERITY_ORDER.index(ceiling):
        return ceiling
    return computed


def _name(isin):
    # type: (str) -> str
    return securities.name_for(isin) if isin else "(account-level)"


def _sorted(brks):
    # type: (List[Break]) -> List[Break]
    """
    Queue order: worst band first, largest exposure first inside it, then stable
    by kind/isin/custodian.

    The exposure term is not cosmetic. Severity is a band, and a band is coarse
    on purpose — everything above a hundred thousand is critical, whether it is
    a hundred and one thousand or four hundred and fifty. On the demo's eleven
    findings the band is the whole answer. On a real book the critical band alone
    runs to dozens of rows, and an operations team works a queue from the top;
    without this term the largest exposure in the book lands wherever its rule's
    name falls in the alphabet. `scripts/scale.py` is where that stopped being a
    hypothetical.

    Findings the desk could not price sort last within their band rather than
    first: an unpriced finding is not a small one, but it is one a reviewer
    cannot triage by size, and putting it above figures they can would be
    guessing on their behalf.

    Magnitudes are compared without conversion, across currencies. That is worth
    naming rather than hiding: it is exactly as currency-blind as `severity_for`,
    which bands a EUR figure against the same thresholds, and this ordering only
    refines those bands. A deployment holding materially mixed currencies should
    convert to the account's base before both — one rate, applied in one place.
    """
    from model import SEVERITY_ORDER

    rank = dict((s, i) for i, s in enumerate(SEVERITY_ORDER))
    return sorted(
        brks,
        key=lambda b: (
            rank.get(b.severity, 99),
            -abs(b.value_at_risk) if b.value_at_risk is not None else ZERO,
            b.kind,
            b.isin,
            b.custodian,
        ),
    )


def _txns_by_isin(txns):
    # type: (Sequence[Transaction]) -> Dict[str, List[Transaction]]
    """
    Group a custodian's movements by instrument, once.

    Every rule below wants "the movements in this security", and the natural way
    to write that is a filter over the whole list inside the loop over positions
    — which is O(positions x movements) and invisible on a nine-instrument book.
    At twenty thousand position lines it was the difference between eight seconds
    and a fifth of one.

    Built inside each detector from that detector's own arguments, so the rules
    stay pure and their signatures stay unchanged.
    """
    index = {}  # type: Dict[str, List[Transaction]]
    for txn in txns:
        index.setdefault(txn.isin, []).append(txn)
    return index


# --- 1. quantity rollforward -------------------------------------------------


def detect_qty_rollforward(prior, current, txns):
    # type: (Snapshot, Snapshot, Sequence[Transaction]) -> List[Break]
    """
    Prior share count, plus every signed transaction delta, must equal the
    current share count. This is the most basic claim a custodian makes and the
    one most often quietly false.

    Positions opened during the window are skipped: with no prior line there is
    nothing to roll forward from, and treating an absent prior as zero would
    report every new purchase as a break.
    """
    out = []
    prior_by = prior.by_isin()
    current_by = current.by_isin()
    by_isin = _txns_by_isin(txns)

    for isin, cur in sorted(current_by.items()):
        pri = prior_by.get(isin)
        if pri is None:
            continue

        deltas = [t for t in by_isin.get(isin, []) if t.quantity != ZERO]
        moved = sum((t.quantity for t in deltas), ZERO)
        expected = q_qty(pri.quantity + moved)
        actual = q_qty(cur.quantity)
        gap = q_qty(actual - expected)
        if abs(gap) <= QTY_TOLERANCE:
            continue

        exposure = abs(gap) * cur.price
        citations = [pri.source, cur.source] + [t.source for t in deltas]
        out.append(
            Break(
                kind="QTY_ROLLFORWARD",
                severity=severity_for(exposure),
                isin=isin,
                security=_name(isin),
                custodian=cur.custodian,
                account=cur.account,
                as_of=cur.as_of,
                detail={
                    "opening_quantity": fmt_qty(pri.quantity),
                    "transaction_delta": fmt_qty(moved),
                    "expected_quantity": fmt_qty(expected),
                    "reported_quantity": fmt_qty(actual),
                    "unexplained_quantity": fmt_qty(gap),
                    "value_at_risk": fmt_money(exposure, cur.ccy),
                    "transactions_seen": str(len(deltas)),
                },
                citations=citations,
                value_at_risk=q_money(exposure),
                value_ccy=cur.ccy,
            )
        )
    return out


def detect_position_disappeared(prior, current, txns):
    # type: (Snapshot, Snapshot, Sequence[Transaction]) -> List[Break]
    """
    A holding on the prior statement and absent from this one.

    The merger in the demo data is exactly this shape at both custodians, and it
    is entirely correct — the target ceased to exist and the shares were
    exchanged. So the rule is not "a position vanished"; it is "a position
    vanished and the activity file does not account for it". Getting that
    distinction wrong turns every corporate action into a false critical.
    """
    out = []
    current_by = current.by_isin()
    by_isin = _txns_by_isin(txns)

    for isin, pri in sorted(prior.by_isin().items()):
        if isin in current_by:
            continue
        deltas = [t for t in by_isin.get(isin, []) if t.quantity != ZERO]
        moved = sum((t.quantity for t in deltas), ZERO)
        residual = q_qty(pri.quantity + moved)
        if abs(residual) <= QTY_TOLERANCE:
            continue  # the activity file accounts for the whole holding

        exposure = abs(residual) * pri.price
        out.append(
            Break(
                kind="POSITION_DISAPPEARED",
                severity=severity_for(exposure),
                isin=isin,
                security=_name(isin),
                custodian=prior.custodian,
                account=prior.account,
                as_of=current.as_of,
                detail={
                    "opening_quantity": fmt_qty(pri.quantity),
                    "transaction_delta": fmt_qty(moved),
                    "unexplained_quantity": fmt_qty(residual),
                    "closing_position": "absent from the statement",
                    "value_at_risk": fmt_money(exposure, pri.ccy),
                },
                citations=[pri.source] + [t.source for t in deltas],
                value_at_risk=q_money(exposure),
                value_ccy=pri.ccy,
            )
        )
    return out


# --- 2. corporate actions ----------------------------------------------------


def detect_corp_actions(prior, current, txns, actions):
    # type: (Snapshot, Snapshot, Sequence[Transaction], Sequence[CorporateAction]) -> List[Break]
    """
    For each action in the window, did this custodian apply it, and correctly?

    Backed by the reference feed rather than inferred from the custodians' own
    disagreement — see corpactions.py for why that distinction is the whole
    argument. Note what this rule must NOT do: MSFT went through a 3-for-2 in the
    same window and was handled correctly by everyone, and a rule that keys on
    "share count changed near a corporate action" flags it.
    """
    out = []
    prior_by = prior.by_isin()
    current_by = current.by_isin()
    window = corpactions.actions_in_window(actions, prior.as_of, current.as_of)

    for action in window:
        # Mergers and ticker changes are different shapes and have their own
        # detectors. Routing them through the split path would ask "did the share
        # count scale by the ratio?" of an action that zeroes one position and
        # creates another, and of one that moves no shares at all.
        if action.is_two_legged() or action.kind == "NAME_CHANGE":
            continue
        pri = prior_by.get(action.isin)
        cur = current_by.get(action.isin)
        status = corpactions.application_status(action, pri, cur, list(txns))
        state = status["status"]

        if state in ("applied", "not_held", "unknown"):
            continue

        expected_qty = status.get("expected_qty")
        detail = {
            "action": action.label(),
            "ex_date": action.ex_date.isoformat(),
            "description": action.description,
            "opening_quantity": fmt_qty(pri.quantity) if pri else "n/a",
            "reported_quantity": fmt_qty(cur.quantity) if cur else "n/a",
            "expected_quantity": fmt_qty(expected_qty) if expected_qty is not None else "n/a",
            "booked_transactions": str(len(status.get("booked") or [])),
        }
        citations = [action.source]
        if pri:
            citations.append(pri.source)
        if cur:
            citations.append(cur.source)
        citations.extend(t.source for t in (status.get("booked") or []))

        if state == "not_applied":
            shortfall = abs(Decimal(expected_qty) - cur.quantity)
            exposure = shortfall * cur.price
            detail["unapplied_quantity"] = fmt_qty(shortfall)
            detail["value_at_risk"] = fmt_money(exposure, cur.ccy)
            kind = "CORP_ACTION_UNAPPLIED"
        elif state == "wrong_ratio":
            implied = status.get("implied_ratio")
            exposure = abs(Decimal(expected_qty) - cur.quantity) * cur.price
            detail["implied_ratio"] = (
                "%d-for-%d" % implied if implied else "not a recognised split ratio"
            )
            detail["value_at_risk"] = fmt_money(exposure, cur.ccy)
            kind = "CORP_ACTION_WRONG_RATIO"
        else:  # basis_corrupted
            drift = abs(Decimal(cur.cost_basis) - Decimal(pri.cost_basis))
            exposure = drift
            detail["opening_cost_basis"] = fmt_money(pri.cost_basis, cur.ccy)
            detail["reported_cost_basis"] = fmt_money(cur.cost_basis, cur.ccy)
            detail["basis_drift"] = fmt_money(drift, cur.ccy)
            detail["value_at_risk"] = fmt_money(exposure, cur.ccy)
            kind = "CORP_ACTION_BASIS_CORRUPTED"

        out.append(
            Break(
                kind=kind,
                severity=severity_for(exposure),
                isin=action.isin,
                security=_name(action.isin),
                custodian=current.custodian,
                account=current.account,
                as_of=current.as_of,
                detail=detail,
                citations=citations,
                value_at_risk=q_money(exposure),
                value_ccy=cur.ccy if cur else "",
            )
        )
    return out


def detect_mergers(prior, current, txns, actions):
    # type: (Snapshot, Snapshot, Sequence[Transaction], Sequence[CorporateAction]) -> List[Break]
    """
    Two-legged actions, checked leg by leg.

    The case worth building the whole engine for is `acquirer_not_credited`: the
    custodian removes the target, never credits the replacement shares, and the
    client's holding simply evaporates. Nothing else in this file can see it. The
    rollforward cannot — the position file and the activity file agree with each
    other perfectly. Cross-custodian comparison sees a symptom on a security that
    is not the one that had the action. Only an external record of the merger
    supplies the missing expectation.
    """
    out = []
    prior_by = prior.by_isin()
    current_by = current.by_isin()

    for action in corpactions.actions_in_window(actions, prior.as_of, current.as_of):
        if not action.is_two_legged():
            continue
        st = corpactions.merger_status(action, prior_by, current_by, list(txns))
        state = st["status"]
        if state in ("both_legs_ok", "not_held"):
            continue

        acquirer = st["acquirer_isin"]
        acq_pos = current_by.get(acquirer)
        price = acq_pos.price if acq_pos else ZERO
        ccy = acq_pos.ccy if acq_pos else prior_by[action.isin].ccy
        shortfall = st.get("acquirer_shortfall") or ZERO
        exposure = abs(Decimal(shortfall)) * price

        detail = {
            "action": action.label(),
            "ex_date": action.ex_date.isoformat(),
            "description": action.description,
            "leg_failed": {
                "acquirer_not_credited": "acquirer shares never credited",
                "target_not_removed": "target position never removed",
                "not_processed": "neither leg processed",
            }.get(state, state),
            "target": "%s (%s)" % (_name(action.isin), action.isin),
            "acquirer": "%s (%s)" % (_name(acquirer), acquirer),
            "exchange_ratio": "%d-for-%d" % (action.ratio_num, action.ratio_den),
            "target_quantity_held": fmt_qty(st["prior_target_qty"]) if st.get("prior_target_qty") is not None else "n/a",
            "shares_due": fmt_qty(st["entitlement"]) if st.get("entitlement") is not None else "n/a",
            "expected_acquirer_quantity": fmt_qty(st["expected_acquirer_qty"]) if st.get("expected_acquirer_qty") is not None else "n/a",
            "reported_acquirer_quantity": fmt_qty(st["current_acquirer_qty"]) if st.get("current_acquirer_qty") is not None else "0",
            "shares_missing": fmt_qty(shortfall),
            "value_at_risk": fmt_money(exposure, ccy),
        }

        citations = [action.source]
        for isin in (action.isin, acquirer):
            for bucket in (prior_by, current_by):
                pos = bucket.get(isin)
                if pos is not None:
                    citations.append(pos.source)
        citations.extend(t.source for t in (st.get("booked") or []))

        out.append(
            Break(
                kind="MERGER_UNPROCESSED",
                severity=severity_for(exposure),
                isin=acquirer,
                security=_name(acquirer),
                custodian=current.custodian,
                account=current.account,
                as_of=current.as_of,
                detail=detail,
                citations=citations,
                value_at_risk=q_money(exposure),
                value_ccy=ccy,
            )
        )
    return out


def detect_identifier_stale(current, actions):
    # type: (Snapshot, Sequence[CorporateAction]) -> List[Break]
    """
    A custodian still printing a superseded ticker.

    Nothing is misstated. Quantity, price, basis and valuation are all correct,
    and every arithmetic rule in this file is right to stay silent. What is wrong
    is the identifier — and identifiers are how two systems agree they are
    discussing the same instrument.

    Severity is capped deliberately. The exposure figure is the position's full
    value, because a mis-keyed holding can be dropped from a consolidated report
    entirely, but the probability is not one and the failure is a control
    weakness rather than a misstatement. Reporting a stale ticker as `critical`
    alongside a genuinely missing half-million of stock would be the fastest way
    to teach an operations team to ignore the queue.
    """
    out = []
    by_isin = current.by_isin()

    for action in actions:
        if action.kind != "NAME_CHANGE" or action.ex_date > current.as_of:
            continue
        pos = by_isin.get(action.isin)
        st = corpactions.name_change_status(action, pos)
        if st["status"] != "stale":
            continue

        exposure = pos.market_value
        out.append(
            Break(
                kind="IDENTIFIER_STALE",
                severity=_capped_severity(exposure, "medium"),
                isin=action.isin,
                security=_name(action.isin),
                custodian=current.custodian,
                account=current.account,
                as_of=current.as_of,
                detail={
                    "reported_symbol": st["reported_symbol"],
                    "current_symbol": action.new_symbol,
                    "effective_date": action.ex_date.isoformat(),
                    "description": action.description,
                    "identifier_used_to_match": "CUSIP %s" % securities.CUSIP_BY_ISIN.get(action.isin, "n/a"),
                    "position_reconciles": "yes - quantity, price and cost basis all agree",
                    "position_value": fmt_money(pos.market_value, pos.ccy),
                    "value_at_risk": fmt_money(exposure, pos.ccy),
                },
                citations=[action.source, pos.source],
                value_at_risk=q_money(exposure),
                value_ccy=pos.ccy,
            )
        )
    return out


# --- 3. FX consistency -------------------------------------------------------


def detect_fx_inconsistency(snapshot):
    # type: (Snapshot) -> List[Break]
    """
    A statement must agree with itself. Every non-base holding carries a base-
    currency value, and the document prints the rates it used at the top. Divide
    one by the other and the printed rate should come back.

    Only positions denominated in something other than the base currency are
    examined; a EUR holding in a EUR statement has no conversion to be wrong
    about, and inventing a 1.0000 rate for it would be inventing a finding.
    """
    out = []
    rates = dict((f.pair, f) for f in snapshot.fx_rates)

    for pos in snapshot.positions:
        if pos.ccy == snapshot.base_ccy or pos.reported_base_value is None:
            continue
        pair = "%s/%s" % (pos.ccy, snapshot.base_ccy)
        quoted = rates.get(pair)
        if quoted is None:
            continue
        if pos.market_value == ZERO:
            continue

        expected = q_money(pos.market_value * quoted.rate)
        reported = q_money(pos.reported_base_value)
        gap = q_money(reported - expected)
        if abs(gap) <= FX_TOLERANCE:
            continue

        implied = q_rate(pos.reported_base_value / pos.market_value)
        drift = pct_diff(implied, quoted.rate)
        out.append(
            Break(
                kind="FX_INCONSISTENT",
                severity=severity_for(gap),
                isin=pos.isin,
                security=_name(pos.isin),
                custodian=pos.custodian,
                account=pos.account,
                as_of=pos.as_of,
                detail={
                    "pair": pair,
                    "quoted_rate": str(quoted.rate),
                    "implied_rate": str(implied),
                    "rate_drift_pct": ("%.4f" % drift) if drift is not None else "n/a",
                    "local_value": fmt_money(pos.market_value, pos.ccy),
                    "expected_base_value": fmt_money(expected, snapshot.base_ccy),
                    "reported_base_value": fmt_money(reported, snapshot.base_ccy),
                    "value_at_risk": fmt_money(gap, snapshot.base_ccy),
                },
                citations=[quoted.source, pos.source],
                value_at_risk=q_money(gap),
                value_ccy=snapshot.base_ccy,
            )
        )
    return out


# --- 4. cost basis rollforward -----------------------------------------------


def roll_cost_basis(opening_qty, opening_basis, txns):
    # type: (Decimal, Decimal, Sequence[Transaction]) -> Decimal
    """
    Roll a cost basis through a window on weighted-average unit cost.

    Weighted average, not FIFO, and the reports say so. Neither custodian
    supplies tax lots, and FIFO without lots is not a stricter method — it is a
    guess wearing a stricter method's name. Where lots are available the same
    function shape takes them; where they are not, the honest answer is to state
    which convention produced the number.

    Splits move the share count and leave total basis alone. That is not a
    special case bolted on — it is the definition of a split.
    """
    qty = Decimal(opening_qty)
    basis = Decimal(opening_basis)

    for t in sorted(txns, key=lambda x: (x.trade_date, x.kind)):
        if t.kind == "BUY":
            basis += -t.amount  # amount is a cash outflow, hence negative
            qty += t.quantity
        elif t.kind == "SELL":
            if qty > ZERO:
                unit = basis / qty
                basis -= unit * abs(t.quantity)
            qty += t.quantity
        elif t.kind == "SPLIT":
            qty += t.quantity
    return q_money(basis)


def detect_cost_basis_drift(prior, current, txns):
    # type: (Snapshot, Snapshot, Sequence[Transaction]) -> List[Break]
    """
    Skips any position where either side's basis is unreported. BHP reports no
    cost basis at all, and a rule that read `None` as zero would report every
    Swiss holding as having lost its entire basis — six critical findings, all
    fictional, on the first run in front of a client.
    """
    out = []
    prior_by = prior.by_isin()
    by_isin = _txns_by_isin(txns)

    for isin, cur in sorted(current.by_isin().items()):
        pri = prior_by.get(isin)
        if pri is None or pri.cost_basis is None or cur.cost_basis is None:
            continue

        relevant = by_isin.get(isin, [])
        expected = roll_cost_basis(pri.quantity, pri.cost_basis, relevant)
        reported = q_money(cur.cost_basis)
        drift = q_money(reported - expected)
        if abs(drift) <= BASIS_TOLERANCE:
            continue

        pct = pct_diff(reported, expected)
        out.append(
            Break(
                kind="COST_BASIS_DRIFT",
                severity=severity_for(drift),
                isin=isin,
                security=_name(isin),
                custodian=cur.custodian,
                account=cur.account,
                as_of=cur.as_of,
                detail={
                    "method": "weighted-average unit cost (no tax lots supplied)",
                    "opening_cost_basis": fmt_money(pri.cost_basis, cur.ccy),
                    "expected_cost_basis": fmt_money(expected, cur.ccy),
                    "reported_cost_basis": fmt_money(reported, cur.ccy),
                    "basis_drift": fmt_money(drift, cur.ccy),
                    "drift_pct": ("%.4f" % pct) if pct is not None else "n/a",
                    "transactions_applied": str(len(relevant)),
                    "value_at_risk": fmt_money(drift, cur.ccy),
                },
                citations=[pri.source, cur.source] + [t.source for t in relevant],
                value_at_risk=q_money(drift),
                value_ccy=cur.ccy,
            )
        )
    return out


# --- 5. cross-custodian agreement --------------------------------------------


def in_flight_qty(txns, isin, as_of):
    # type: (Sequence[Transaction], str, object) -> Decimal
    """
    Net share movement traded on or before `as_of` and settling after it.

    This is the quantity that a trade-date statement carries and a settled-date
    one does not, which is the entire difference between two correct statements
    of the same mandate on the same day.
    """
    total = ZERO
    for txn in txns:
        if txn.isin == isin and txn.in_flight_at(as_of):
            total += txn.quantity
    return total


def in_flight_index(txns_by_custodian, as_of_dates):
    # type: (Dict[str, Sequence[Transaction]], Sequence[object]) -> Dict[tuple, Decimal]
    """
    `in_flight_qty` for every instrument at once, keyed `(custodian, as_of, isin)`.

    Same answer as calling `in_flight_qty` per instrument, arrived at by walking
    each movement list once instead of once per instrument. The distinction is
    the whole cost of this rule at scale: every pair of custodians on different
    bases asks the question for every instrument they share, so the per-call form
    is O(instruments x movements) and this is O(dates x movements) with dates
    being the two or three statement dates in the period.

    `tests/test_breaks.py` holds the two forms to the same answers rather than
    trusting the reasoning above, because a faster path that quietly disagrees
    with the slow one is worse than the slow one.
    """
    index = {}  # type: Dict[tuple, Decimal]
    for custodian in sorted(txns_by_custodian):
        for txn in txns_by_custodian[custodian]:
            for as_of in as_of_dates:
                if not txn.in_flight_at(as_of):
                    continue
                key = (custodian, as_of, txn.isin)
                index[key] = index.get(key, ZERO) + txn.quantity
    return index


def _basis_expected_gap(a, b, flight):
    # type: (tuple, tuple, Dict[tuple, Decimal]) -> Decimal
    """
    How far apart two holdings *should* be, given the statements' bases.

    Each side is a (position, basis, custodian) triple taken from the statement
    it arrived on. Zero when both are on the same basis, which is the ordinary
    case and the one that must stay exactly as strict as it was before this rule
    learned about settlement.
    """
    pos_a, basis_a, name_a = a
    pos_b, basis_b, name_b = b
    if basis_a == basis_b:
        return ZERO
    # The unsettled trades are recorded by whichever custodian reports on a trade
    # date basis — the settled-basis custodian will not book them until they
    # settle, in a period that has not closed yet.
    if basis_a == BASIS_TRADE:
        return flight.get((name_a, pos_a.as_of, pos_a.isin), ZERO)
    return -flight.get((name_b, pos_b.as_of, pos_b.isin), ZERO)


def detect_cross_custodian_qty(snapshots, txns_by_custodian=None):
    # type: (Sequence[Snapshot], Optional[Dict[str, Sequence[Transaction]]]) -> List[Break]
    """
    Two custodians reporting the same mandate must report the same share count —
    once both counts have been put on the same statement basis.

    Only instruments held at two or more custodians are compared. A holding that
    exists at one custodian and not the other is not a disagreement — it is a
    single-custodian position, and the demo's ASML and SAP are exactly that.

    ## Why this rule knows about settlement

    A trade-date statement counts a trade the moment it is executed; a
    settled-date one counts it when the shares move. Between the two sits every
    trade in flight over the period end. Both statements are right, and the naive
    difference between them is real, large, and not a break.

    So the difference is measured against what the bases predict, and only the
    residual is reported. A rule that skipped the comparison entirely whenever
    bases differed would be worse than this one: it would go quiet on precisely
    the custodian pair that most needs checking.
    """
    txns_by_custodian = txns_by_custodian or {}
    out = []
    # Basis belongs to the statement, not to the holding, and Position is frozen
    # and should stay that way — so it is paired with each holding as it is
    # indexed. Looking it up later by `pos.custodian` would work right up until a
    # caller built a snapshot whose positions were labelled with something else,
    # and then it would silently compare two trade-date books as though both were
    # settled. Carrying it is one word longer and cannot drift.
    index = {}  # type: Dict[str, List[tuple]]
    dates = set()
    for snap in snapshots:
        dates.add(snap.as_of)
        for pos in snap.positions:
            index.setdefault(pos.isin, []).append((pos, snap.basis, snap.custodian))
            dates.add(pos.as_of)
    # A position's own as_of rather than its snapshot's, for the same reason the
    # basis travels with the holding above: in this pipeline they are always the
    # same, and an index keyed on an assumption is an index that is wrong exactly
    # once, silently, on the day the assumption stops holding.
    flight = in_flight_index(txns_by_custodian, sorted(dates))

    for isin, holdings in sorted(index.items()):
        if len(holdings) < 2:
            continue
        holdings = sorted(holdings, key=lambda h: h[2])
        for i in range(len(holdings) - 1):
            for j in range(i + 1, len(holdings)):
                side_a, side_b = holdings[i], holdings[j]
                (a, basis_a, name_a), (b, basis_b, name_b) = side_a, side_b
                raw_gap = q_qty(a.quantity - b.quantity)
                expected = q_qty(_basis_expected_gap(side_a, side_b, flight))
                gap = q_qty(raw_gap - expected)
                if abs(gap) <= QTY_TOLERANCE:
                    continue

                price = max(a.price, b.price)
                exposure = abs(gap) * price
                ratio = corpactions.infer_ratio(min(a.quantity, b.quantity), max(a.quantity, b.quantity))
                detail = {
                    "custodian_a": name_a,
                    "quantity_a": fmt_qty(a.quantity),
                    "custodian_b": name_b,
                    "quantity_b": fmt_qty(b.quantity),
                    "difference": fmt_qty(gap),
                    "value_at_risk": fmt_money(exposure, a.ccy),
                }
                if expected != ZERO:
                    # The reviewer is looking at two numbers that differ by more
                    # than the finding claims. Say why, on the finding itself,
                    # rather than leaving them to rediscover it.
                    detail["reported_difference"] = fmt_qty(raw_gap)
                    detail["explained_by_settlement"] = fmt_qty(expected)
                    detail["basis_a"] = basis_a
                    detail["basis_b"] = basis_b
                # A clean split ratio between the two is a strong hint, but only a
                # hint — it is recorded as context, never as the finding itself.
                # The reference-backed rule above is what makes the claim.
                if ratio is not None:
                    detail["ratio_between_custodians"] = "%d:%d" % ratio
                    detail["note"] = (
                        "difference is exactly %d:%d, consistent with an unapplied "
                        "corporate action" % ratio
                    )

                out.append(
                    Break(
                        kind="CROSS_CUSTODIAN_QTY",
                        severity=severity_for(exposure),
                        isin=isin,
                        security=_name(isin),
                        custodian="%s vs %s" % (name_a, name_b),
                        account=a.account,
                        as_of=a.as_of,
                        detail=detail,
                        citations=[a.source, b.source],
                        value_at_risk=q_money(exposure),
                        value_ccy=a.ccy,
                    )
                )
    return out


def detect_basis_mismatch(snapshots, txns_by_custodian=None):
    # type: (Sequence[Snapshot], Optional[Dict[str, Sequence[Transaction]]]) -> List[Break]
    """
    Custodians reporting one mandate must be compared on one statement basis.

    This is a control finding, not a misstatement: every statement involved is
    correct on its own terms. What is wrong is the comparison, and it is wrong
    silently — the quantity rule above knows how to net the difference out, but
    only because the settlement dates happened to be reported. A feed that
    stopped carrying :98A::SETT// would leave the same difference looking like a
    six-figure break with nothing to explain it.

    Severity is capped for the same reason as a stale ticker: the exposure is
    notional. Nothing here is missing — it is in flight.
    """
    txns_by_custodian = txns_by_custodian or {}
    bases = {}  # type: Dict[str, List[str]]
    for snap in snapshots:
        bases.setdefault(snap.basis, []).append(snap.custodian)
    if len(bases) < 2:
        return []

    as_of = min(s.as_of for s in snapshots)
    account = snapshots[0].account if snapshots else ""

    # The exposure is the cash value of what is in flight, taken from the
    # movements themselves rather than from a position line — the whole point is
    # that these shares are not on a position line anywhere yet.
    exposure = ZERO
    ccy = ""
    citations = []
    for snap in snapshots:
        if snap.basis != BASIS_TRADE:
            continue
        for txn in txns_by_custodian.get(snap.custodian, []):
            if not txn.in_flight_at(snap.as_of):
                continue
            exposure += abs(txn.amount)
            ccy = ccy or txn.ccy
            citations.append(txn.source)

    detail = {
        "bases_reported": ", ".join(
            "%s (%s)" % (b, ", ".join(sorted(c))) for b, c in sorted(bases.items())
        ),
        "movements_in_flight": str(len(citations)),
        "value_in_flight": fmt_money(exposure, ccy) if ccy else "none",
    }
    return [
        Break(
            kind="STATEMENT_BASIS_MISMATCH",
            severity=_capped_severity(exposure, "medium"),
            isin="",
            security=_name(""),
            custodian=", ".join(sorted(s.custodian for s in snapshots)),
            account=account,
            as_of=as_of,
            detail=detail,
            citations=citations,
            value_at_risk=q_money(exposure),
            value_ccy=ccy,
        )
    ]


def detect_price_divergence(snapshots):
    # type: (Sequence[Snapshot]) -> List[Break]
    """
    Same instrument, same date, materially different price at two custodians.

    Silent on the demo data — both custodians price every shared holding
    identically — and it stays in the desk anyway, because a stale price at one
    custodian is a real and routine break. It is proven by its unit tests rather
    than by the demo, which is the correct relationship between the two.
    """
    out = []
    index = {}  # type: Dict[str, List[Position]]
    for snap in snapshots:
        for pos in snap.positions:
            index.setdefault(pos.isin, []).append(pos)

    for isin, holdings in sorted(index.items()):
        if len(holdings) < 2:
            continue
        holdings = sorted(holdings, key=lambda p: p.custodian)
        for i in range(len(holdings) - 1):
            for j in range(i + 1, len(holdings)):
                a, b = holdings[i], holdings[j]
                if a.ccy != b.ccy or b.price == ZERO:
                    continue
                drift = pct_diff(a.price, b.price)
                if drift is None or abs(drift) <= PRICE_TOLERANCE_PCT:
                    continue
                exposure = abs(a.price - b.price) * min(a.quantity, b.quantity)
                out.append(
                    Break(
                        kind="PRICE_DIVERGENCE",
                        severity=severity_for(exposure),
                        isin=isin,
                        security=_name(isin),
                        custodian="%s vs %s" % (a.custodian, b.custodian),
                        account=a.account,
                        as_of=a.as_of,
                        detail={
                            "custodian_a": a.custodian,
                            "price_a": fmt_money(a.price, a.ccy),
                            "custodian_b": b.custodian,
                            "price_b": fmt_money(b.price, b.ccy),
                            "divergence_pct": "%.4f" % drift,
                            "value_at_risk": fmt_money(exposure, a.ccy),
                        },
                        citations=[a.source, b.source],
                        value_at_risk=q_money(exposure),
                        value_ccy=a.ccy,
                    )
                )
    return out


# --- 6. recurring accrual --------------------------------------------------


def detect_missing_fee_accrual(prior_txns, current_txns, custodian, account, as_of):
    # type: (Sequence[Transaction], Sequence[Transaction], str, str, object) -> List[Break]
    """
    A fee charged last period and not this one.

    Fees are the quietest break in the book. Nothing fails, no balance is out,
    and the manager simply does not get paid — or, in the other direction, the
    client is billed twice and nobody notices until they do. An absence is
    invisible to every rule that compares numbers, because there is no number to
    compare; it has to be looked for on purpose.
    """
    prior_fees = [t for t in prior_txns if t.kind == "FEE"]
    current_fees = [t for t in current_txns if t.kind == "FEE"]
    if not prior_fees or current_fees:
        return []

    expected = sum((abs(t.amount) for t in prior_fees), ZERO)
    ccy = prior_fees[0].ccy
    return [
        Break(
            kind="MISSING_FEE_ACCRUAL",
            severity=severity_for(expected),
            isin="",
            security="(account-level)",
            custodian=custodian,
            account=account,
            as_of=as_of,
            detail={
                "prior_period_fees": str(len(prior_fees)),
                "prior_period_amount": fmt_money(expected, ccy),
                "current_period_fees": "0",
                "expected_amount": fmt_money(expected, ccy),
                "value_at_risk": fmt_money(expected, ccy),
            },
            citations=[t.source for t in prior_fees],
            value_at_risk=q_money(expected),
            value_ccy=ccy,
        )
    ]


# --- orchestration -----------------------------------------------------------


def detect_all(period):
    # type: (Dict[str, object]) -> List[Break]
    """
    Run every rule over a loaded period.

    Takes the plain dict `normalize.load_period` returns, so a test can build one
    by hand and never touch the filesystem.
    """
    prior = period["prior"]
    current = period["current"]
    txns_prior = period["txns_prior"]
    txns_current = period["txns_current"]
    actions = period.get("actions") or []

    found = []  # type: List[Break]

    for custodian in sorted(current.keys()):
        cur_snap = current[custodian]
        pri_snap = prior.get(custodian)
        cur_txns = txns_current.get(custodian, [])
        pri_txns = txns_prior.get(custodian, [])

        found.extend(detect_fx_inconsistency(cur_snap))
        found.extend(detect_identifier_stale(cur_snap, actions))
        if pri_snap is None:
            continue
        found.extend(detect_qty_rollforward(pri_snap, cur_snap, cur_txns))
        found.extend(detect_position_disappeared(pri_snap, cur_snap, cur_txns))
        found.extend(detect_corp_actions(pri_snap, cur_snap, cur_txns, actions))
        found.extend(detect_mergers(pri_snap, cur_snap, cur_txns, actions))
        found.extend(detect_cost_basis_drift(pri_snap, cur_snap, cur_txns))
        found.extend(
            detect_missing_fee_accrual(
                pri_txns, cur_txns, custodian, cur_snap.account, cur_snap.as_of
            )
        )

    snapshots = [current[c] for c in sorted(current.keys())]
    found.extend(detect_cross_custodian_qty(snapshots, txns_current))
    found.extend(detect_basis_mismatch(snapshots, txns_current))
    found.extend(detect_price_divergence(snapshots))

    return _sorted(found)
