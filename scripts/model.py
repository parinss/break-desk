#!/usr/bin/env python3
"""
model.py — the canonical model every custodian feed collapses into.

Design rule: **every figure carries its provenance**. A break the reviewer
cannot trace back to a page and a line in a source document is an assertion,
not a finding — and an assertion is not deployable in a regulated shop.

`Provenance` is therefore not optional metadata; it is a required field on
Position and Transaction, and the detectors propagate it onto every Break.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Provenance:
    """Where a figure came from. `line` is 1-indexed, as a human counts."""

    file: str
    line: int
    excerpt: str

    def cite(self):
        # type: () -> str
        return "%s:%d" % (self.file, self.line)


@dataclass(frozen=True)
class Security:
    """Security master entry — the crosswalk that makes cross-custodian work."""

    isin: str
    symbol: str
    name: str
    ccy: str


@dataclass(frozen=True)
class Position:
    as_of: date
    custodian: str
    account: str
    isin: str
    quantity: Decimal
    price: Decimal
    ccy: str
    market_value: Decimal
    source: Provenance
    # The ticker the custodian actually printed, kept verbatim. It is not used to
    # identify the instrument — CUSIP and ISIN do that — but a custodian still
    # printing a superseded ticker is itself a finding, and the only way to see
    # it is to record what they wrote rather than what they meant.
    reported_symbol: str = ""
    # Not every custodian reports cost basis. `None` means "not reported" and is
    # distinct from Decimal("0"), which would mean "reported as zero" — the
    # cost-basis detector must skip the former and flag the latter.
    cost_basis: Optional[Decimal] = None
    # What the custodian claims this position is worth in the statement's base
    # currency. Only present when the holding is denominated in something other
    # than the base currency, which is exactly when it can disagree with the FX
    # rate printed on the same page.
    reported_base_value: Optional[Decimal] = None


@dataclass(frozen=True)
class Transaction:
    trade_date: date
    custodian: str
    account: str
    isin: str
    kind: str  # BUY | SELL | DIV | FEE | SPLIT | FX
    quantity: Decimal
    amount: Decimal
    ccy: str
    source: Provenance


@dataclass(frozen=True)
class CorporateAction:
    """
    An action as recorded by an independent reference source — not inferred from
    a custodian's own numbers.

    This distinction is the whole point. Inferring "a 4-for-1 split happened"
    from the fact that two custodians disagree 4:1 is circular: the disagreement
    is what we are trying to explain. An independent record turns the finding
    from "these numbers look like a split" into "this split occurred on this
    date; custodian A applied it, custodian B did not" — which is a statement a
    reviewer can act on without re-deriving it.

    `ratio_num:ratio_den` is new shares per old share: a 4-for-1 split is 4:1,
    a 3-for-2 is 3:2, a 1-for-10 reverse split is 1:10.
    """

    isin: str
    ex_date: date
    kind: str  # SPLIT | REVERSE_SPLIT | MERGER | NAME_CHANGE | SPINOFF | CASH_DIV
    ratio_num: int
    ratio_den: int
    description: str
    source: Provenance
    # MERGER and SPINOFF touch two instruments. `isin` is the one being acted on
    # — the target that disappears, the parent that spins off — and `related_isin`
    # is the one received. A single-ISIN action model cannot express either, and
    # a reconciliation engine that cannot express a merger will report one as two
    # unrelated breaks: a position that vanished and a position that appeared.
    related_isin: str = ""
    # For NAME_CHANGE: what the ticker became. The ISIN does not move.
    new_symbol: str = ""
    old_symbol: str = ""
    cash_per_share: Optional[Decimal] = None

    def label(self):
        # type: () -> str
        if self.kind in ("SPLIT", "REVERSE_SPLIT"):
            return "%d-for-%d %s" % (
                self.ratio_num, self.ratio_den, self.kind.lower().replace("_", " ")
            )
        if self.kind == "MERGER":
            return "merger (%d-for-%d exchange)" % (self.ratio_num, self.ratio_den)
        if self.kind == "NAME_CHANGE":
            return "ticker change %s to %s" % (self.old_symbol, self.new_symbol)
        return self.kind.lower().replace("_", " ")

    def multiplier(self):
        # type: () -> Decimal
        """Factor a share count is multiplied by. 4-for-1 -> 4; 1-for-10 -> 0.1."""
        return Decimal(self.ratio_num) / Decimal(self.ratio_den)

    def affects_quantity(self):
        # type: () -> bool
        """
        Whether the share count of `isin` moves.

        A merger zeroes the target outright rather than scaling it, so it is
        excluded here and handled on its own path — scaling a position to zero
        by a ratio is not what a merger does, and pretending otherwise puts a
        division into a code path that must never have one.
        """
        return self.kind in ("SPLIT", "REVERSE_SPLIT")

    def is_two_legged(self):
        # type: () -> bool
        return self.kind in ("MERGER", "SPINOFF") and bool(self.related_isin)

    def touches(self):
        # type: () -> List[str]
        """Every instrument this action moves. Used to route detection."""
        out = [self.isin]
        if self.related_isin:
            out.append(self.related_isin)
        return out


@dataclass(frozen=True)
class FxRate:
    as_of: date
    custodian: str
    pair: str  # e.g. "USD/EUR"
    rate: Decimal
    source: Provenance


@dataclass
class Snapshot:
    """One custodian's view of one account on one date."""

    as_of: date
    custodian: str
    account: str
    base_ccy: str
    positions: List[Position] = field(default_factory=list)
    fx_rates: List[FxRate] = field(default_factory=list)

    def by_isin(self):
        # type: () -> Dict[str, Position]
        return dict((p.isin, p) for p in self.positions)


# Break severities, ordered. The UI sorts on this, so it is data, not cosmetics.
SEVERITY_ORDER = ["critical", "high", "medium", "low"]


@dataclass
class Break:
    """
    A detected discrepancy.

    `narrative` and `proposed_fix` are the only fields an LLM may ever write.
    Everything else is computed deterministically and is asserted immutable
    across the explain stage (see tests/test_explain.py).
    """

    kind: str
    severity: str
    isin: str
    security: str
    custodian: str
    account: str
    as_of: date
    detail: Dict[str, str]
    citations: List[Provenance]
    # `detail` is presentation — formatted strings a human reads. This is the
    # same figure as a number, kept separately so the UI can sort and total by
    # exposure without parsing its own output back into arithmetic. `value_ccy`
    # is carried alongside because totalling USD and EUR into one number would be
    # a worse error than any break in the report.
    value_at_risk: Optional[Decimal] = None
    value_ccy: str = ""
    narrative: str = ""
    proposed_fix: str = ""

    def key(self):
        # type: () -> str
        return "%s|%s|%s|%s" % (self.kind, self.isin, self.custodian, self.as_of.isoformat())


class _Encoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, date):
            return o.isoformat()
        if hasattr(o, "__dataclass_fields__"):
            return asdict(o)
        return super(_Encoder, self).default(o)


def to_json(obj, indent=2):
    # type: (object, Optional[int]) -> str
    """Deterministic JSON: sorted keys, Decimals as strings (never floats)."""
    return json.dumps(obj, cls=_Encoder, indent=indent, sort_keys=True)
