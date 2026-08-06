"""
_util.py — path bootstrap and fixture builders.

Every test imports this first. It puts `scripts/` and the repo root on the path
so the modules import by their own names, exactly as they do under `build.py`
and `serve.py` — a test suite that imports the code differently from the way it
runs is testing a different program.

The builders exist so a test states only the thing it is about. A rollforward
test cares about two share counts; it should not have to name a currency, an
account and a provenance record to say so. Defaults are deliberately boring and
every one of them is overridable.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")

for _p in (SCRIPTS, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from model import (  # noqa: E402
    Break,
    CorporateAction,
    FxRate,
    Position,
    Provenance,
    Snapshot,
    Transaction,
)

# Real entries from the security master. Detectors call securities.name_for(),
# so fixtures use identifiers that actually resolve — an invented ISIN would
# exercise the fallback path rather than the one production takes.
AAPL = "US0378331005"
NVDA = "US67066G1040"
MSFT = "US5949181045"
VOD = "US92857W3088"
ASML = "NL0010273215"
SAP = "DE0007164600"
META = "US30303M1027"
TSLA = "US88160R1014"
SPCX = "US00SPACEX19"

PRIOR = date(2026, 3, 31)
CURRENT = date(2026, 6, 30)
MID = date(2026, 5, 18)

CUST_A = "Custodian A"
CUST_B = "Custodian B"
ACCOUNT = "TEST-0001"


def D(value):
    # type: (object) -> Decimal
    return value if isinstance(value, Decimal) else Decimal(str(value))


def prov(file="fixture.csv", line=1, excerpt="fixture line"):
    # type: (str, int, str) -> Provenance
    return Provenance(file=file, line=line, excerpt=excerpt)


def pos(isin, qty, price="100.00", as_of=CURRENT, custodian=CUST_A, ccy="USD",
        market_value=None, cost_basis=None, reported_base_value=None,
        reported_symbol="", account=ACCOUNT, source=None):
    # type: (...) -> Position
    quantity = D(qty)
    unit = D(price)
    return Position(
        as_of=as_of,
        custodian=custodian,
        account=account,
        isin=isin,
        quantity=quantity,
        price=unit,
        ccy=ccy,
        market_value=D(market_value) if market_value is not None else quantity * unit,
        cost_basis=D(cost_basis) if cost_basis is not None else None,
        reported_base_value=(
            D(reported_base_value) if reported_base_value is not None else None
        ),
        reported_symbol=reported_symbol,
        source=source or prov(),
    )


def txn(isin, kind, qty="0", amount="0", trade_date=MID, custodian=CUST_A,
        ccy="USD", account=ACCOUNT, source=None):
    # type: (...) -> Transaction
    return Transaction(
        trade_date=trade_date,
        custodian=custodian,
        account=account,
        isin=isin,
        kind=kind,
        quantity=D(qty),
        amount=D(amount),
        ccy=ccy,
        source=source or prov(file="activity.csv"),
    )


def action(isin, kind="SPLIT", num=4, den=1, ex_date=MID, related="",
           old_symbol="", new_symbol="", description="", source=None):
    # type: (...) -> CorporateAction
    return CorporateAction(
        isin=isin,
        ex_date=ex_date,
        kind=kind,
        ratio_num=num,
        ratio_den=den,
        related_isin=related,
        old_symbol=old_symbol,
        new_symbol=new_symbol,
        description=description or "%s %s" % (isin, kind),
        source=source or prov(file="corporate_actions.csv", line=2),
    )


def snap(positions, as_of=CURRENT, custodian=CUST_A, base_ccy="USD",
         account=ACCOUNT, fx_rates=None):
    # type: (...) -> Snapshot
    return Snapshot(
        as_of=as_of,
        custodian=custodian,
        account=account,
        base_ccy=base_ccy,
        positions=list(positions),
        fx_rates=list(fx_rates or []),
    )


def fx(pair, rate, as_of=CURRENT, custodian=CUST_A, source=None):
    # type: (...) -> FxRate
    return FxRate(
        as_of=as_of,
        custodian=custodian,
        pair=pair,
        rate=D(rate),
        source=source or prov(file="statement.txt", line=8),
    )


def brk(kind="QTY_ROLLFORWARD", severity="high", isin=AAPL, security="Apple Inc.",
        custodian=CUST_A, account=ACCOUNT, as_of=CURRENT, detail=None,
        citations=None, value_at_risk=None, value_ccy="USD"):
    # type: (...) -> Break
    return Break(
        kind=kind,
        severity=severity,
        isin=isin,
        security=security,
        custodian=custodian,
        account=account,
        as_of=as_of,
        detail=dict(detail or {}),
        citations=list(citations or [prov(file="meridian.csv", line=12)]),
        value_at_risk=D(value_at_risk) if value_at_risk is not None else None,
        value_ccy=value_ccy,
    )


def period(prior=None, current=None, txns_prior=None, txns_current=None, actions=None):
    # type: (...) -> dict
    """The exact shape `normalize.load_period` returns, buildable without files."""
    return {
        "prior": prior or {},
        "current": current or {},
        "txns_prior": txns_prior or {},
        "txns_current": txns_current or {},
        "actions": list(actions or []),
    }


def kinds(breaks):
    # type: (list) -> list
    return [b.kind for b in breaks]


def has_break(breaks, kind, isin=None):
    # type: (list, str, str) -> bool
    return any(b.kind == kind and (isin is None or b.isin == isin) for b in breaks)
