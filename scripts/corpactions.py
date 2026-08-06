#!/usr/bin/env python3
"""
corpactions.py — corporate-action reasoning.

Corporate actions are where reconciliation engines earn their keep and where
naive ones embarrass themselves. A 4-for-1 split changes the share count, changes
the price, and changes **nothing** about the total cost basis. Get any one of
those three wrong and you have either missed a real break or invented a fake one
on a position that was always fine.

The module is deliberately separated from `breaks.py` because the arithmetic
here is the part worth being right about, and it is testable in isolation with no
statements, no files, and no pipeline.

Three things live here:

1. **Invariants** — what must hold once an action has been applied correctly.
2. **Ratio inference** — reading a ratio out of two disagreeing share counts,
   with an allowlist so the inference cannot fire on arbitrary noise.
3. **Application checks** — did this custodian apply this action, and correctly?

On evidence hierarchy: a finding backed by a reference record ("a 4-for-1 split
on NVDA went ex on 2026-05-18") is `reference`-grade. A finding backed only by
two custodians disagreeing by a suspiciously clean ratio is `inferred`-grade and
is reported as *suspected*. Both are useful; conflating them is not.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

from model import CorporateAction, Position, Transaction

# Ratios that issuers actually declare. This is an allowlist, not a bound, and
# that is deliberate: `4400/5000` reduces to the perfectly tidy 22:25, and a
# bounds check ("both terms under 30") would happily call it a split. No issuer
# has ever declared a 22-for-25 split. An allowlist cannot make that mistake.
ALLOWED_SPLIT_RATIOS = frozenset([
    # forward
    (2, 1), (3, 1), (4, 1), (5, 1), (6, 1), (8, 1), (10, 1), (15, 1), (20, 1),
    (3, 2), (4, 3), (5, 2), (5, 4), (7, 5), (7, 6), (11, 10),
    # reverse
    (1, 2), (1, 3), (1, 4), (1, 5), (1, 6), (1, 8), (1, 10), (1, 15), (1, 20),
    (2, 3), (3, 4), (4, 5),
])

# A share count can carry three decimal places; a ratio derived from two of them
# needs a little room before it is called inexact.
QTY_TOLERANCE = Decimal("0.001")


def infer_ratio(from_qty, to_qty):
    # type: (Decimal, Decimal) -> Optional[Tuple[int, int]]
    """
    Read a plausible split ratio out of two share counts, or None.

    None is the common and correct answer. Two custodians disagreeing about a
    share count is usually a broken feed, a missed trade, or a settlement-date
    difference — not a corporate action. This function returns a ratio only when
    the numbers land exactly on one an issuer would actually declare.
    """
    if from_qty is None or to_qty is None:
        return None
    if from_qty <= 0 or to_qty <= 0:
        return None
    frac = Fraction(Decimal(to_qty)) / Fraction(Decimal(from_qty))
    pair = (frac.numerator, frac.denominator)
    if pair == (1, 1):
        return None
    return pair if pair in ALLOWED_SPLIT_RATIOS else None


def expected_quantity(prior_qty, action):
    # type: (Decimal, CorporateAction) -> Decimal
    """Share count after the action, before any trading."""
    if not action.affects_quantity():
        return prior_qty
    return prior_qty * Decimal(action.ratio_num) / Decimal(action.ratio_den)


def quantity_matches(actual, expected):
    # type: (Decimal, Decimal) -> bool
    return abs(Decimal(actual) - Decimal(expected)) <= QTY_TOLERANCE


def split_delta(prior_qty, action):
    # type: (Decimal, CorporateAction) -> Decimal
    """
    The share delta a correctly-booked split transaction should carry.

    Custodians book a split as an adjustment line, not as a replacement: 800
    shares through a 4-for-1 produce a +2,400 line, not a 3,200 line. Booking
    the target instead of the delta double-counts, and is a real error we want
    the rollforward to catch rather than absorb.
    """
    return expected_quantity(prior_qty, action) - prior_qty


def basis_is_preserved(prior_basis, current_basis, tolerance=Decimal("0.01")):
    # type: (Optional[Decimal], Optional[Decimal], Decimal) -> Optional[bool]
    """
    A split must not change total cost basis. Per-share basis divides; the total
    is untouched. A custodian that scales the total along with the share count
    has just multiplied the client's tax lot by four.

    Returns None when either side is unreported — "we cannot tell" is a distinct
    answer from "it is fine", and callers must not conflate them.
    """
    if prior_basis is None or current_basis is None:
        return None
    return abs(Decimal(current_basis) - Decimal(prior_basis)) <= tolerance


def actions_in_window(actions, start, end, isin=None):
    # type: (List[CorporateAction], object, object, Optional[str]) -> List[CorporateAction]
    """Actions going ex within (start, end]. The open lower bound matters: an
    action on the prior statement date is already reflected in that statement."""
    out = []
    for a in actions:
        if a.ex_date <= start or a.ex_date > end:
            continue
        # Match on every instrument the action moves, not just its subject — a
        # merger is in scope for the acquirer's ISIN as well as the target's.
        if isin is not None and isin not in a.touches():
            continue
        out.append(a)
    return sorted(out, key=lambda a: (a.ex_date, a.isin))


def booked_by(txns, action, isin=None):
    # type: (List[Transaction], CorporateAction, Optional[str]) -> List[Transaction]
    """Transactions that look like this custodian booking this action."""
    if action.kind in ("SPLIT", "REVERSE_SPLIT"):
        kinds = ("SPLIT",)
    elif action.kind in ("MERGER", "SPINOFF"):
        kinds = ("MERGER", "SPLIT")
    else:
        kinds = ("DIV",)
    target = isin if isin is not None else action.isin
    return [
        t for t in txns
        if t.isin == target and t.kind in kinds and t.trade_date == action.ex_date
    ]


def merger_status(action, prior, current, txns):
    # type: (CorporateAction, Dict[str, Position], Dict[str, Position], List[Transaction]) -> Dict[str, object]
    """
    A merger has two legs and they fail independently.

    The target must go to zero. The acquirer must be credited at the exchange
    ratio. A custodian that does one and not the other is the interesting case
    and by far the most damaging: the client's statement simply loses the shares,
    and every arithmetic check still balances, because the position file and the
    activity file agree with each other about a state that is wrong.

    That is why this cannot be inferred from a rollforward. Rollforward asks
    "does this custodian agree with itself?" — and a half-processed merger does.
    Only an external record of the action can ask the other question.

    Statuses: `both_legs_ok`, `target_not_removed`, `acquirer_not_credited`,
    `not_processed`, `not_held`, `unknown`.
    """
    target_isin = action.isin
    acquirer_isin = action.related_isin

    pri_target = prior.get(target_isin)
    cur_target = current.get(target_isin)
    pri_acq = prior.get(acquirer_isin)
    cur_acq = current.get(acquirer_isin)

    result = {
        "action": action,
        "target_isin": target_isin,
        "acquirer_isin": acquirer_isin,
        "prior_target_qty": pri_target.quantity if pri_target else None,
        "current_target_qty": cur_target.quantity if cur_target else None,
        "prior_acquirer_qty": pri_acq.quantity if pri_acq else None,
        "current_acquirer_qty": cur_acq.quantity if cur_acq else None,
        "booked": booked_by(txns, action, target_isin) + booked_by(txns, action, acquirer_isin),
    }

    if pri_target is None:
        result["status"] = "not_held"
        return result

    # Shares the acquirer leg owes: target holding x exchange ratio.
    entitlement = Decimal(pri_target.quantity) * Decimal(action.ratio_num) / Decimal(action.ratio_den)
    result["entitlement"] = entitlement

    target_removed = cur_target is None or quantity_matches(cur_target.quantity, Decimal("0"))

    prior_acq_qty = pri_acq.quantity if pri_acq else Decimal("0")
    expected_acq = prior_acq_qty + entitlement
    # Trading in the acquirer during the window moves it legitimately.
    traded_acq = sum(
        (t.quantity for t in txns if t.isin == acquirer_isin and t.kind in ("BUY", "SELL")),
        Decimal("0"),
    )
    expected_acq += traded_acq
    result["expected_acquirer_qty"] = expected_acq

    actual_acq = cur_acq.quantity if cur_acq else Decimal("0")
    acquirer_credited = quantity_matches(actual_acq, expected_acq)
    result["acquirer_shortfall"] = expected_acq - actual_acq

    if target_removed and acquirer_credited:
        result["status"] = "both_legs_ok"
    elif target_removed and not acquirer_credited:
        result["status"] = "acquirer_not_credited"
    elif not target_removed and acquirer_credited:
        result["status"] = "target_not_removed"
    else:
        result["status"] = "not_processed"
    return result


def name_change_status(action, position):
    # type: (CorporateAction, Optional[Position]) -> Dict[str, object]
    """
    A ticker change moves no money, so every arithmetic rule is correctly silent
    on it. What it moves is an identifier — and identifiers are how two systems
    find each other. A custodian still printing the old ticker after the change
    reconciles fine today against anyone matching on ISIN, and breaks the moment
    the ticker is reissued to a different company, which is a thing that happens.

    Reported as a control finding rather than a valuation one. Nothing is
    misstated; something is stale, and stale reference data is the leading
    indicator of the breaks that do misstate.
    """
    if position is None:
        return {"status": "not_held", "action": action}
    reported = (position.reported_symbol or "").strip().upper()
    if not reported:
        return {"status": "no_symbol_reported", "action": action}
    if reported == action.new_symbol.upper():
        return {"status": "current", "action": action, "reported_symbol": reported}
    if reported == action.old_symbol.upper():
        return {"status": "stale", "action": action, "reported_symbol": reported}
    return {"status": "unrecognised", "action": action, "reported_symbol": reported}


def application_status(action, prior_pos, current_pos, txns):
    # type: (CorporateAction, Optional[Position], Optional[Position], List[Transaction]) -> Dict[str, object]
    """
    Did this custodian apply this action, and correctly?

    Returns a dict rather than a bool because the interesting answers are not
    binary. The states, in the order they are checked:

      not_held        this custodian does not hold the security — not a break
      unknown         held now but not on the prior statement — nothing to compare
      applied         share count moved as the action requires
      not_applied     share count did not move at all
      wrong_ratio     share count moved, but not by the declared ratio
      basis_corrupted quantity right, but total cost basis moved through a split

    `booked` is tracked separately from the quantity check on purpose: a
    custodian can land on the right share count with no transaction to show for
    it, which reconciles today and is unauditable tomorrow.
    """
    if action.is_two_legged() or action.kind == "NAME_CHANGE":
        raise ValueError(
            "%s must be routed to merger_status/name_change_status, not "
            "application_status — it does not scale a single position"
            % action.kind
        )
    if prior_pos is None and current_pos is None:
        return {"status": "not_held", "action": action}
    if prior_pos is None:
        return {"status": "unknown", "action": action, "reason": "no prior position to compare"}

    booked = booked_by(txns, action)
    result = {
        "status": None,
        "action": action,
        "booked": booked,
        "prior_qty": prior_pos.quantity,
        "current_qty": current_pos.quantity if current_pos is not None else None,
    }

    if current_pos is None:
        result["status"] = "unknown"
        result["reason"] = "position closed or absent on the current statement"
        return result

    # Trading inside the window moves the share count too, so the comparison has
    # to net out everything except the action itself.
    traded = sum(
        (t.quantity for t in txns if t.isin == action.isin and t.kind in ("BUY", "SELL")),
        Decimal("0"),
    )
    if action.affects_quantity():
        # Order matters and we do not know it, so accept either: the action
        # applied to the pre-trade count, or to the post-trade count.
        candidates = [
            expected_quantity(prior_pos.quantity, action) + traded,
            expected_quantity(prior_pos.quantity + traded, action),
        ]
        unapplied = prior_pos.quantity + traded

        if any(quantity_matches(current_pos.quantity, c) for c in candidates):
            result["status"] = "applied"
            result["expected_qty"] = candidates[1]
        elif quantity_matches(current_pos.quantity, unapplied):
            result["status"] = "not_applied"
            result["expected_qty"] = candidates[1]
        else:
            result["status"] = "wrong_ratio"
            result["expected_qty"] = candidates[1]
            result["implied_ratio"] = infer_ratio(unapplied, current_pos.quantity)
    else:
        result["status"] = "applied" if booked else "not_applied"

    if result["status"] == "applied" and action.affects_quantity():
        # A split contributes exactly zero to total cost basis — but buys and
        # sells in the same window move it for legitimate reasons, and from two
        # statements alone there is no way to attribute a basis change between
        # the two causes. So this check runs only when the window is free of
        # trading in this security. When it is not, the cost-basis rollforward in
        # breaks.py is the detector that covers it, and duplicating a weaker
        # version of it here would only manufacture false positives.
        traded_this_isin = [
            t for t in txns if t.isin == action.isin and t.kind in ("BUY", "SELL")
        ]
        if traded_this_isin:
            result["basis_preserved"] = None
            result["basis_note"] = "not isolable: %d trade(s) in window" % len(traded_this_isin)
        else:
            preserved = basis_is_preserved(prior_pos.cost_basis, current_pos.cost_basis)
            result["basis_preserved"] = preserved
            if preserved is False:
                result["status"] = "basis_corrupted"

    return result
