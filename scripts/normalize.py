#!/usr/bin/env python3
"""
normalize.py — custodian statements in, canonical model out.

Everything downstream of this file is custodian-agnostic. That is the contract:
`breaks.py` must never know that Meridian writes `1,300.000` and BHP writes
`1.300,000`, or that one says SELL and the other says VERKAUF. Adding a third
custodian should mean adding a parser here and touching nothing else.

## Sign conventions

Custodians do not agree on signs, so the canonical model picks one and every
parser converts into it. This is the single most consequential decision in the
file, because a sign error in a rollforward looks exactly like a real break:

  `Transaction.quantity`  signed delta to the share count.
                          BUY +300, SELL -200, SPLIT +2,400 (the *delta*, not
                          the resulting total), DIV and FEE 0.

  `Transaction.amount`    signed cash flow into the account.
                          BUY -69,420.00, SELL +47,860.00, DIV +325.00,
                          FEE -4,287.50.

Both statements report a sale as a positive 200. Both are negated here.

## On failing loudly

An instrument that does not resolve to an ISIN raises. This is deliberate and it
is not the usual "log a warning and carry on": an unidentifiable holding cannot
be reconciled, and a report that quietly omits a position is worse than no report
— the reviewer has no way to know it is incomplete. Better to refuse to produce
one. The same applies to a malformed number: `money.py` raises rather than
defaulting to zero, and nothing here catches it.
"""

from __future__ import annotations

import csv
import io
import os
import re
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

import securities
from model import CorporateAction, FxRate, Position, Provenance, Snapshot, Transaction
from money import (
    ParseError,
    parse_eu_date,
    parse_eu_decimal,
    parse_iso_date,
    parse_us_date,
    parse_us_decimal,
)

# Re-exported: the pattern belongs to the identifier and is defined with the
# security master, but parsing is where most callers meet it.
ISIN_RE = securities.ISIN_RE
_MULTISPACE = re.compile(r"\s{2,}")
_FX_LINE = re.compile(r"^([A-Z]{3})/([A-Z]{3})\s+([\d.,]+)\s*$")

MERIDIAN = "Meridian Securities"
BHP = "Banque Helvetique Privee"

# Custodian activity vocabularies -> canonical kinds.
_MERIDIAN_KINDS = {
    "BUY": "BUY", "SELL": "SELL", "DIV": "DIV", "FEE": "FEE", "SPLIT": "SPLIT",
    "INT": "DIV", "MGMTFEE": "FEE", "MERGER": "MERGER",
}
_BHP_KINDS = {
    "KAUF": "BUY", "VERKAUF": "SELL", "DIVIDENDE": "DIV", "GEBUEHR": "FEE",
    "AKTIENSPLIT": "SPLIT", "SPLIT": "SPLIT", "ZINS": "DIV",
    "FUSION": "MERGER", "MERGER": "MERGER",
}

# Kinds whose reported quantity is a magnitude that must be negated to become a
# signed delta. Listed explicitly rather than inferred from the amount's sign,
# because a zero-proceeds transfer out has no sign to infer from.
#
# MERGER is deliberately absent. A merger has a leg that removes shares and a
# leg that adds them, and no rule keyed on the activity code alone can tell them
# apart — so both statements carry the sign explicitly, Meridian in accounting
# parentheses and BHP with a leading minus, and the parser trusts what it reads
# rather than guessing which leg it is looking at.
_NEGATE_QTY = ("SELL",)


def _excerpt(line):
    # type: (str) -> str
    text = line.rstrip("\n")
    return text if len(text) <= 160 else text[:157] + "..."


def _header_value(lines, label):
    # type: (List[str], str) -> Optional[str]
    """Value from a `Label: value` line in a statement preamble."""
    pattern = re.compile(r"^\s*" + label + r"\s*:\s*(.+?)\s*$", re.IGNORECASE)
    for line in lines:
        m = pattern.match(line)
        if m:
            return m.group(1).strip()
    return None


def _require(value, what, path):
    # type: (Optional[str], str, str) -> str
    if value is None or value == "":
        raise ParseError("could not find %s in %s" % (what, os.path.basename(path)))
    return value


def _resolve(symbol, cusip, path, lineno, raw):
    # type: (Optional[str], Optional[str], str, int, str) -> str
    isin = securities.isin_for(symbol=symbol, cusip=cusip)
    if isin is None:
        raise ParseError(
            "unmapped instrument (symbol=%r cusip=%r) at %s:%d -- %s"
            % (symbol, cusip, os.path.basename(path), lineno, _excerpt(raw))
        )
    return isin


def _read(path):
    # type: (str) -> List[str]
    with io.open(path, "r", encoding="utf-8") as fh:
        return fh.read().splitlines()


def _find_header(lines, prefix, path):
    # type: (List[str], str, str) -> int
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            return i
    raise ParseError(
        "no header row starting %r in %s" % (prefix, os.path.basename(path))
    )


def _csv_fields(line):
    # type: (str) -> List[str]
    return next(csv.reader([line]))


# --- Meridian ----------------------------------------------------------------


def parse_meridian_positions(path):
    # type: (str) -> Snapshot
    lines = _read(path)
    base = os.path.basename(path)
    as_of = parse_us_date(_require(_header_value(lines, "As Of"), "'As Of'", path))
    account = _require(_header_value(lines, "Account"), "'Account'", path)
    base_ccy = _header_value(lines, "Base Currency") or "USD"

    start = _find_header(lines, "Symbol,CUSIP", path)
    snapshot = Snapshot(as_of=as_of, custodian=MERIDIAN, account=account, base_ccy=base_ccy)

    for offset, raw in enumerate(lines[start + 1:]):
        if not raw.strip():
            continue
        lineno = start + 2 + offset
        f = _csv_fields(raw)
        if len(f) < 8:
            raise ParseError("expected 8 columns at %s:%d -- %s" % (base, lineno, _excerpt(raw)))
        symbol, cusip, _name, qty, price, mv, cost, ccy = f[:8]
        isin = _resolve(symbol, cusip, path, lineno, raw)
        snapshot.positions.append(
            Position(
                as_of=as_of,
                custodian=MERIDIAN,
                account=account,
                isin=isin,
                quantity=parse_us_decimal(qty),
                price=parse_us_decimal(price),
                ccy=ccy.strip() or base_ccy,
                market_value=parse_us_decimal(mv),
                cost_basis=parse_us_decimal(cost) if cost.strip() else None,
                reported_symbol=symbol.strip().upper(),
                source=Provenance(file=base, line=lineno, excerpt=_excerpt(raw)),
            )
        )
    return snapshot


def parse_meridian_transactions(path):
    # type: (str) -> List[Transaction]
    lines = _read(path)
    base = os.path.basename(path)
    account = _require(_header_value(lines, "Account"), "'Account'", path)
    start = _find_header(lines, "Trade Date,Settle Date", path)

    out = []
    for offset, raw in enumerate(lines[start + 1:]):
        if not raw.strip():
            continue
        lineno = start + 2 + offset
        f = _csv_fields(raw)
        if len(f) < 10:
            raise ParseError("expected 10 columns at %s:%d -- %s" % (base, lineno, _excerpt(raw)))
        trade_date, _settle, symbol, cusip, activity, qty, _price, amount, ccy, _desc = f[:10]

        kind = _MERIDIAN_KINDS.get(activity.strip().upper())
        if kind is None:
            raise ParseError(
                "unknown activity %r at %s:%d" % (activity, base, lineno)
            )

        # Account-level lines (fees, interest) carry no instrument. That is not
        # an unmapped security — it is a line that legitimately has none.
        isin = ""
        if symbol.strip() or cusip.strip():
            isin = _resolve(symbol, cusip, path, lineno, raw)

        quantity = parse_us_decimal(qty) if qty.strip() else Decimal("0")
        if kind in _NEGATE_QTY:
            quantity = -quantity
        if kind in ("DIV", "FEE"):
            quantity = Decimal("0")

        out.append(
            Transaction(
                trade_date=parse_us_date(trade_date),
                custodian=MERIDIAN,
                account=account,
                isin=isin,
                kind=kind,
                quantity=quantity,
                amount=parse_us_decimal(amount) if amount.strip() else Decimal("0"),
                ccy=ccy.strip() or "USD",
                source=Provenance(file=base, line=lineno, excerpt=_excerpt(raw)),
            )
        )
    return out


# --- Banque Helvetique Privee ------------------------------------------------
#
# The input is text pulled out of a PDF, so the columns are held apart by runs of
# spaces and nothing else. Splitting on two-or-more spaces is exactly as fragile
# as it sounds, which is why the parser verifies the field count on every line
# and refuses the file rather than quietly mis-assigning a column.


def parse_bhp_positions(path):
    # type: (str) -> Snapshot
    lines = _read(path)
    base = os.path.basename(path)
    as_of = parse_eu_date(_require(_header_value(lines, r"Stichtag / Date"), "'Stichtag'", path))
    account = _require(_header_value(lines, r"Konto / Compte"), "'Konto'", path)
    base_ccy = _header_value(lines, r"Referenzwaehrung / Monnaie") or "EUR"

    snapshot = Snapshot(as_of=as_of, custodian=BHP, account=account, base_ccy=base_ccy)

    # FX block. Rates are stated once per document and every non-base holding is
    # supposed to have been converted with them — "supposed to" being the whole
    # reason the FX detector exists.
    in_fx = False
    for i, raw in enumerate(lines):
        if raw.startswith("UMRECHNUNGSKURSE"):
            in_fx = True
            continue
        if in_fx:
            m = _FX_LINE.match(raw)
            if not m:
                in_fx = False
                continue
            snapshot.fx_rates.append(
                FxRate(
                    as_of=as_of,
                    custodian=BHP,
                    pair="%s/%s" % (m.group(1), m.group(2)),
                    rate=parse_eu_decimal(m.group(3)),
                    source=Provenance(file=base, line=i + 1, excerpt=_excerpt(raw)),
                )
            )

    start = _find_header(lines, "ISIN", path)
    for offset, raw in enumerate(lines[start + 1:]):
        if not raw.strip():
            break  # the position block ends at the first blank line
        lineno = start + 2 + offset
        f = _MULTISPACE.split(raw.strip())
        if len(f) != 7:
            raise ParseError(
                "expected 7 space-delimited columns at %s:%d, got %d -- %s"
                % (base, lineno, len(f), _excerpt(raw))
            )
        isin, _name, qty, price, ccy, mv, eur = f
        if not ISIN_RE.match(isin):
            raise ParseError("not an ISIN at %s:%d -- %r" % (base, lineno, isin))
        if isin not in securities.BY_ISIN:
            raise ParseError(
                "unmapped ISIN %s at %s:%d -- %s" % (isin, base, lineno, _excerpt(raw))
            )
        ccy = ccy.strip()
        snapshot.positions.append(
            Position(
                as_of=as_of,
                custodian=BHP,
                account=account,
                isin=isin,
                quantity=parse_eu_decimal(qty),
                price=parse_eu_decimal(price),
                ccy=ccy,
                market_value=parse_eu_decimal(mv),
                # BHP does not report cost basis at all. None, not zero — and the
                # cost-basis detector must therefore stay silent here rather than
                # reporting every BHP holding as having lost its entire basis.
                cost_basis=None,
                reported_base_value=parse_eu_decimal(eur),
                source=Provenance(file=base, line=lineno, excerpt=_excerpt(raw)),
            )
        )
    return snapshot


def parse_bhp_transactions(path):
    # type: (str) -> List[Transaction]
    lines = _read(path)
    base = os.path.basename(path)
    account = _require(_header_value(lines, r"Konto / Compte"), "'Konto'", path)
    start = _find_header(lines, "Datum", path)

    out = []
    for offset, raw in enumerate(lines[start + 1:]):
        if not raw.strip():
            break
        lineno = start + 2 + offset
        f = _MULTISPACE.split(raw.strip())
        if len(f) != 7:
            raise ParseError(
                "expected 7 space-delimited columns at %s:%d, got %d -- %s"
                % (base, lineno, len(f), _excerpt(raw))
            )
        datum, isin, art, qty, amount, ccy, _text = f

        kind = _BHP_KINDS.get(art.strip().upper())
        if kind is None:
            raise ParseError("unknown Art %r at %s:%d" % (art, base, lineno))

        isin = isin.strip()
        if isin in ("-", ""):
            isin = ""
        elif isin not in securities.BY_ISIN:
            raise ParseError(
                "unmapped ISIN %s at %s:%d -- %s" % (isin, base, lineno, _excerpt(raw))
            )

        quantity = parse_eu_decimal(qty) if qty.strip() else Decimal("0")
        if kind in _NEGATE_QTY:
            quantity = -quantity
        if kind in ("DIV", "FEE"):
            quantity = Decimal("0")

        out.append(
            Transaction(
                trade_date=parse_eu_date(datum),
                custodian=BHP,
                account=account,
                isin=isin,
                kind=kind,
                quantity=quantity,
                amount=parse_eu_decimal(amount),
                ccy=ccy.strip(),
                source=Provenance(file=base, line=lineno, excerpt=_excerpt(raw)),
            )
        )
    return out


# --- corporate action reference feed -----------------------------------------


def parse_corporate_actions(path):
    # type: (str) -> List[CorporateAction]
    """
    The vendor feed. ISO dates — a third date format, because real feeds do not
    coordinate with the custodians they describe.
    """
    lines = _read(path)
    base = os.path.basename(path)
    start = _find_header(lines, "ISIN,Symbol,ExDate", path)

    out = []
    for offset, raw in enumerate(lines[start + 1:]):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        lineno = start + 2 + offset
        f = _csv_fields(raw)
        if len(f) < 10:
            raise ParseError("expected 10 columns at %s:%d -- %s" % (base, lineno, _excerpt(raw)))
        isin, _symbol, ex_date, kind, num, den, related, old_sym, new_sym, desc = f[:10]

        isin = isin.strip()
        if isin not in securities.BY_ISIN:
            raise ParseError("unmapped ISIN %s at %s:%d" % (isin, base, lineno))
        related = related.strip()
        if related and related not in securities.BY_ISIN:
            raise ParseError(
                "unmapped related ISIN %s at %s:%d" % (related, base, lineno)
            )
        try:
            ratio_num, ratio_den = int(num), int(den)
        except ValueError:
            raise ParseError("non-integer ratio %r:%r at %s:%d" % (num, den, base, lineno))
        if ratio_num <= 0 or ratio_den <= 0:
            raise ParseError("non-positive ratio %d:%d at %s:%d" % (ratio_num, ratio_den, base, lineno))

        kind = kind.strip().upper()
        if kind == "SPLIT" and ratio_num < ratio_den:
            kind = "REVERSE_SPLIT"
        if kind in ("MERGER", "SPINOFF") and not related:
            raise ParseError(
                "%s at %s:%d names no related instrument -- a two-legged action "
                "with one leg is not interpretable" % (kind, base, lineno)
            )

        out.append(
            CorporateAction(
                isin=isin,
                ex_date=parse_iso_date(ex_date),
                kind=kind,
                ratio_num=ratio_num,
                ratio_den=ratio_den,
                related_isin=related,
                old_symbol=old_sym.strip().upper(),
                new_symbol=new_sym.strip().upper(),
                description=desc.strip(),
                source=Provenance(file=base, line=lineno, excerpt=_excerpt(raw)),
            )
        )
    return out


# --- loading a whole period --------------------------------------------------


def load_period(statements_dir):
    # type: (str) -> Dict[str, object]
    """
    Load every statement for the demo period into one structure.

    Returns plain dicts keyed by custodian rather than a bespoke class: the shape
    is stable, the detectors only read from it, and a dict is trivially
    constructible in a test without touching the filesystem.
    """
    j = lambda n: os.path.join(statements_dir, n)  # noqa: E731

    return {
        "prior": {
            MERIDIAN: parse_meridian_positions(j("meridian_positions_2026-03-31.csv")),
            BHP: parse_bhp_positions(j("bhp_vermoegensausweis_2026-03-31.txt")),
        },
        "current": {
            MERIDIAN: parse_meridian_positions(j("meridian_positions_2026-06-30.csv")),
            BHP: parse_bhp_positions(j("bhp_vermoegensausweis_2026-06-30.txt")),
        },
        "txns_prior": {
            MERIDIAN: parse_meridian_transactions(j("meridian_transactions_2026Q1.csv")),
            BHP: parse_bhp_transactions(j("bhp_bewegungen_2026Q1.txt")),
        },
        "txns_current": {
            MERIDIAN: parse_meridian_transactions(j("meridian_transactions_2026Q2.csv")),
            BHP: parse_bhp_transactions(j("bhp_bewegungen_2026Q2.txt")),
        },
        "actions": parse_corporate_actions(j("corporate_actions_2026Q2.csv")),
    }
