#!/usr/bin/env python3
"""
securities.py — the security master, and the crosswalk between custodian keys.

This is the least glamorous file in the repository and the one without which
nothing else works. Meridian identifies a holding by ticker and CUSIP; BHP
identifies it by ISIN. Neither statement contains the other's key. Any claim
that "the two custodians disagree about NVDA" is only meaningful once something
has established that Meridian's `NVDA / 67066G104` and BHP's `US67066G1040` are
the same instrument.

In production this table comes from a vendor feed or the client's own IBOR. Here
it is a literal, so the demo has no external dependency — but the shape is the
same, and so is the failure mode: an instrument missing from the master is
silently invisible to every cross-custodian rule. `unmapped()` exists so that
failure is reported rather than absorbed.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from model import Security

# Two letters of country code, nine of national identifier, one check digit.
# It lives here rather than in the parser because the shape is a property of the
# identifier, not of the file it happened to be read out of — the master itself
# is checked against it, and a parser is only one of the things that cares.
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

# isin, symbol, cusip, name, ccy
_ROWS = [
    ("US0378331005", "AAPL", "037833100", "Apple Inc.", "USD"),
    ("US67066G1040", "NVDA", "67066G104", "NVIDIA Corporation", "USD"),
    ("US5949181045", "MSFT", "594918104", "Microsoft Corporation", "USD"),
    ("US92857W3088", "VOD", "92857W308", "Vodafone Group plc ADR", "USD"),
    ("NL0010273215", "ASML", "N07059186", "ASML Holding N.V.", "EUR"),
    ("DE0007164600", "SAP", "D66992104", "SAP SE", "EUR"),
    ("US30303M1027", "META", "30303M102", "Meta Platforms Inc.", "USD"),
    ("US88160R1014", "TSLA", "88160R101", "Tesla Inc.", "USD"),
    # Wholly invented. SpaceX is a private company and has never had a listed
    # security; the ISIN below is deliberately unmistakable so nobody mistakes
    # this row for a real instrument. It exists to exercise a merger.
    ("US00SPACEX19", "SPCX", "00SPACEX1", "Space Exploration Technologies Corp.", "USD"),
]

# Tickers that used to mean something else. FB became META and the ISIN did not
# move — which is the entire argument for keying on ISIN or CUSIP. A ticker is a
# display label with a lifecycle: it changes under a company, and it is reissued
# to unrelated companies afterwards. A security master that forgets its former
# symbols cannot read last quarter's statements; one that treats a former symbol
# as current will mis-key a position the day a new issuer picks the ticker up.
_FORMER_SYMBOLS = {
    "FB": "US30303M1027",
}

ALL = [Security(isin=r[0], symbol=r[1], name=r[3], ccy=r[4]) for r in _ROWS]

BY_ISIN = dict((s.isin, s) for s in ALL)  # type: Dict[str, Security]
BY_SYMBOL = dict((s.symbol, s) for s in ALL)  # type: Dict[str, Security]
BY_CUSIP = dict((r[2], BY_ISIN[r[0]]) for r in _ROWS)  # type: Dict[str, Security]

CUSIP_BY_ISIN = dict((r[0], r[2]) for r in _ROWS)  # type: Dict[str, str]


def isin_for(symbol=None, cusip=None):
    # type: (Optional[str], Optional[str]) -> Optional[str]
    """
    Resolve a custodian key to an ISIN. CUSIP wins when both are supplied:
    tickers are recycled between issuers and across venues, CUSIPs are not.

    Former tickers resolve too, and are tried last. A statement printed after a
    ticker change but built from a stale security master will say FB where it
    means META; the position is still Meta Platforms and must reconcile as such.
    Refusing to resolve it would turn a cosmetic staleness into a phantom break,
    and resolving it silently would hide a real one — so it resolves here, and
    `is_former_symbol` lets the detector report the staleness on its own terms.

    Returns None rather than raising. A holding we cannot map is a real and
    common condition — the caller decides whether that is fatal, and the
    pipeline reports it as an unmapped line rather than dropping it silently.
    """
    if cusip:
        sec = BY_CUSIP.get(cusip.strip())
        if sec is not None:
            return sec.isin
    if symbol:
        key = symbol.strip().upper()
        sec = BY_SYMBOL.get(key)
        if sec is not None:
            return sec.isin
        if key in _FORMER_SYMBOLS:
            return _FORMER_SYMBOLS[key]
    return None


def is_former_symbol(symbol, isin):
    # type: (str, str) -> bool
    """True when `symbol` is a superseded ticker for `isin`."""
    if not symbol:
        return False
    key = symbol.strip().upper()
    return _FORMER_SYMBOLS.get(key) == isin


def current_symbol(isin):
    # type: (str) -> str
    sec = BY_ISIN.get(isin)
    return sec.symbol if sec is not None else ""


def name_for(isin):
    # type: (str) -> str
    sec = BY_ISIN.get(isin)
    return sec.name if sec is not None else isin


def ccy_for(isin):
    # type: (str) -> Optional[str]
    sec = BY_ISIN.get(isin)
    return sec.ccy if sec is not None else None


def unmapped(keys):
    # type: (List[str]) -> List[str]
    """Keys that resolve to nothing. Surfaced, never swallowed."""
    missing = []
    for k in keys:
        if isin_for(symbol=k, cusip=k) is None and k not in BY_ISIN:
            missing.append(k)
    return sorted(set(missing))
