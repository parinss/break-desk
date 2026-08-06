#!/usr/bin/env python3
"""
generate.py — writes the synthetic custodian statements under data/statements/.

**No client data has ever touched this repository.** Every figure below is
invented. This module is committed precisely so a reader can verify that claim
themselves: the statements are not scraped, redacted, or anonymised from a real
account — they are constructed here, in the open, from literal tables.

The statements contain deliberate errors. They are declared in `EXPECTED_BREAKS`
at the bottom of this file, which is the contract the integration test asserts
against (tests/test_pipeline.py). If a detector stops finding one of these, or
starts finding something not on the list, the test fails.

Two custodians, deliberately dissimilar — because a reconciliation engine that
only works when both sides look alike has not solved the problem:

  Meridian Securities LLC   US brokerage    CSV     1,300.000  238.90  06/30/2026
  Banque Helvetique Privee  CH booking ctr  PDF-txt 1.300,000  238,90  30.06.2026

Run:  python3 scripts/generate.py
"""

from __future__ import annotations

import os
import sys

import securities

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATEMENTS = os.path.join(ROOT, "data", "statements")

PRIOR = "2026-03-31"
CURRENT = "2026-06-30"
ACCOUNT = "PWM-4471"

MERIDIAN = "Meridian Securities"
BHP = "Banque Helvetique Privee"

# --- security master ---------------------------------------------------------
# Sourced from scripts/securities.py, which the parsers also use. Custodians
# shout their holdings' names in upper case, so the statements do too.
CUSIP_BY_SYMBOL = dict(
    (s.symbol, securities.CUSIP_BY_ISIN[s.isin]) for s in securities.ALL
)
NAME_BY_SYMBOL = dict((s.symbol, s.name.upper()) for s in securities.ALL)
NAME_BY_ISIN = dict((s.isin, s.name.upper()) for s in securities.ALL)

# Superseded ticker, kept so a statement can be generated with a stale security
# master. The CUSIP is unchanged — it is the same instrument — so the position
# still resolves and still reconciles. A custodian whose ticker is stale usually
# has the old legal name too, which is why the description is stale as well.
CUSIP_BY_SYMBOL["FB"] = securities.CUSIP_BY_ISIN["US30303M1027"]
NAME_BY_SYMBOL["FB"] = "FACEBOOK INC"

# --- corporate action reference feed -----------------------------------------
# Stands in for a vendor feed. Its independence from the custodians is the point:
# it is what lets the desk say "this split occurred and BHP did not apply it"
# instead of the circular "these two custodians disagree by a factor that looks
# like a split". Vendors publish ISO dates — a third date format, on purpose.
CORPORATE_ACTIONS = [
    # isin, symbol, ex_date, kind, ratio_new, ratio_old, related_isin,
    # old_symbol, new_symbol, description
    ("US67066G1040", "NVDA", "2026-05-18", "SPLIT", "4", "1", "", "", "",
     "NVIDIA Corporation 4-for-1 common stock split"),
    ("US5949181045", "MSFT", "2026-06-02", "SPLIT", "3", "2", "", "", "",
     "Microsoft Corporation 3-for-2 common stock split"),
    # Ticker change. The ISIN and CUSIP do not move, so nothing in the valuation
    # changes and every arithmetic rule is correctly silent. Only a rule that
    # looks at identifiers can see it.
    ("US30303M1027", "META", "2026-05-05", "NAME_CHANGE", "1", "1", "", "FB", "META",
     "Facebook Inc. renamed Meta Platforms Inc.; ticker FB changed to META"),
    # Two-legged merger: SPCX holders receive two TSLA shares per share held.
    # `related_isin` is the acquirer. Meridian removes the target and never
    # credits the acquirer, which is the failure that balances against itself.
    ("US00SPACEX19", "SPCX", "2026-06-12", "MERGER", "2", "1", "US88160R1014", "", "",
     "Space Exploration Technologies Corp. merged into Tesla Inc.; 2 TSLA per SPCX"),
]


# --- Meridian: positions -----------------------------------------------------
# symbol, quantity, price, market_value, cost_basis
MERIDIAN_POSITIONS = {
    PRIOR: [
        ("AAPL", "1,200.000", "214.35", "257,220.00", "189,240.00"),
        ("NVDA", "800.000", "452.60", "362,080.00", "210,400.00"),
        ("MSFT", "450.000", "498.20", "224,190.00", "168,750.00"),
        ("VOD", "5,000.000", "11.42", "57,100.00", "52,300.00"),
        ("META", "600.000", "512.40", "307,440.00", "214,800.00"),
        ("TSLA", "400.000", "268.50", "107,400.00", "92,600.00"),
        ("SPCX", "1,000.000", "145.80", "145,800.00", "118,000.00"),
    ],
    CURRENT: [
        # Cost basis 217,416.00 is WRONG. Rolling the prior basis forward through
        # the Q2 buy and sell on a weighted-average unit cost gives 224,172.00.
        ("AAPL", "1,300.000", "238.90", "310,570.00", "217,416.00"),
        # 4-for-1 split applied correctly here: 800 -> 3,200, basis unchanged.
        ("NVDA", "3,200.000", "118.90", "380,480.00", "210,400.00"),
        # 3-for-2 split applied correctly at BOTH custodians: 450 +50 bought
        # -> 500, then x1.5 -> 750, basis untouched by the split at 194,015.00.
        # This is the negative test — a corporate action the desk must not flag.
        ("MSFT", "750.000", "341.60", "256,200.00", "194,015.00"),
        # Quantity fell 5,000 -> 4,400 with no transaction to explain it, and the
        # basis did not move with it — the signature of a broken position feed.
        ("VOD", "4,400.000", "12.05", "53,020.00", "52,300.00"),
        # Printed under the superseded ticker FB. The CUSIP is correct, so the
        # position resolves and reconciles perfectly — quantity, price and basis
        # are all right. Only an identifier rule can see anything wrong here.
        ("FB", "600.000", "548.70", "329,220.00", "214,800.00"),
        # The merger's acquirer leg was never credited. Should be 2,400 after
        # receiving 2,000 TSLA for the 1,000 SPCX. Meridian removed the target
        # and stopped, so its position file and its activity file agree with each
        # other about a state that is wrong — and every rollforward passes.
        ("TSLA", "400.000", "271.30", "108,520.00", "92,600.00"),
        # SPCX correctly gone at both custodians. A rule that reported every
        # disappeared position would fire here twice, wrongly.
    ],
}

# --- Meridian: transactions --------------------------------------------------
# trade_date, settle_date, symbol, activity, quantity, price, amount, description
MERIDIAN_TXNS = {
    "2026Q1": [
        ("02/18/2026", "02/20/2026", "VOD", "BUY", "1,000.000", "10.85", "(10,850.00)", "BOUGHT 1000 VOD"),
        ("03/14/2026", "03/14/2026", "MSFT", "DIV", "0.000", "0.00", "1,102.50", "QUALIFIED DIVIDEND MSFT"),
        ("03/31/2026", "03/31/2026", "", "FEE", "0.000", "0.00", "(4,287.50)", "MANAGEMENT FEE Q1 2026"),
    ],
    "2026Q2": [
        ("04/22/2026", "04/24/2026", "MSFT", "BUY", "50.000", "505.30", "(25,265.00)", "BOUGHT 50 MSFT"),
        ("05/12/2026", "05/14/2026", "AAPL", "BUY", "300.000", "231.40", "(69,420.00)", "BOUGHT 300 AAPL"),
        ("05/18/2026", "05/18/2026", "NVDA", "SPLIT", "2,400.000", "0.00", "0.00", "4-FOR-1 STOCK SPLIT NVDA"),
        ("06/02/2026", "06/02/2026", "MSFT", "SPLIT", "250.000", "0.00", "0.00", "3-FOR-2 STOCK SPLIT MSFT"),
        ("06/09/2026", "06/11/2026", "AAPL", "SELL", "200.000", "239.30", "47,860.00", "SOLD 200 AAPL"),
        # The target leg of the merger, booked. The acquirer leg never follows —
        # so the activity file agrees with the position file, and both are wrong.
        ("06/12/2026", "06/12/2026", "SPCX", "MERGER", "(1,000.000)", "0.00", "0.00", "MERGER SPCX INTO TSLA 2-FOR-1"),
        ("06/15/2026", "06/15/2026", "AAPL", "DIV", "0.000", "0.00", "325.00", "QUALIFIED DIVIDEND AAPL"),
        # NOTE: no MANAGEMENT FEE row for Q2. Q1 had one. That absence is break 7.
    ],
}

# --- BHP: positions ----------------------------------------------------------
# isin, quantity, price, ccy, market_value_local, value_eur
BHP_FX = {PRIOR: "0,9240", CURRENT: "0,9170"}

BHP_POSITIONS = {
    PRIOR: [
        ("US0378331005", "1.200,000", "214,35", "USD", "257.220,00", "237.671,28"),
        ("US67066G1040", "800,000", "452,60", "USD", "362.080,00", "334.561,92"),
        ("US5949181045", "450,000", "498,20", "USD", "224.190,00", "207.151,56"),
        ("US92857W3088", "5.000,000", "11,42", "USD", "57.100,00", "52.760,40"),
        ("NL0010273215", "300,000", "892,40", "EUR", "267.720,00", "267.720,00"),
        ("DE0007164600", "1.100,000", "246,80", "EUR", "271.480,00", "271.480,00"),
        ("US30303M1027", "600,000", "512,40", "USD", "307.440,00", "284.074,56"),
        ("US88160R1014", "400,000", "268,50", "USD", "107.400,00", "99.237,60"),
        ("US00SPACEX19", "1.000,000", "145,80", "USD", "145.800,00", "134.719,20"),
    ],
    CURRENT: [
        # EUR value implies USD/EUR 0.8940, not the 0.9170 declared at the top of
        # the same document. The statement disagrees with itself.
        ("US0378331005", "1.300,000", "238,90", "USD", "310.570,00", "277.649,58"),
        # NVDA split NOT applied: still 800 where Meridian shows 3,200.
        ("US67066G1040", "800,000", "118,90", "USD", "95.120,00", "87.225,04"),
        # MSFT 3-for-2 split applied here too. Same action type, same window,
        # correctly handled — so the detector must fire on NVDA and stay quiet
        # on this line, which a ratio-matching heuristic could not manage.
        ("US5949181045", "750,000", "341,60", "USD", "256.200,00", "234.935,40"),
        ("US92857W3088", "5.000,000", "12,05", "USD", "60.250,00", "55.249,25"),
        ("NL0010273215", "300,000", "915,60", "EUR", "274.680,00", "274.680,00"),
        ("DE0007164600", "1.100,000", "253,40", "EUR", "278.740,00", "278.740,00"),
        # BHP keys on ISIN, so the ticker change is invisible to it — which is
        # the point. It cannot have a stale ticker because it never had one.
        ("US30303M1027", "600,000", "548,70", "USD", "329.220,00", "301.894,74"),
        # Merger processed correctly here: both legs. 400 held + 2,000 received.
        ("US88160R1014", "2.400,000", "271,30", "USD", "651.120,00", "597.077,04"),
    ],
}

# date, isin, art, quantity, amount, ccy, text
BHP_TXNS = {
    "2026Q1": [
        ("31.03.2026", "-", "GEBUEHR", "0,000", "-3.847,10", "EUR", "Verwaltungsgebuehren Q1 2026"),
    ],
    "2026Q2": [
        ("22.04.2026", "US5949181045", "KAUF", "50,000", "-25.265,00", "USD", "Kauf 50 MSFT"),
        ("12.05.2026", "US0378331005", "KAUF", "300,000", "-69.420,00", "USD", "Kauf 300 AAPL"),
        # NOTE: no NVDA split row. Meridian booked one on 05/18. That is break 2.
        ("02.06.2026", "US5949181045", "AKTIENSPLIT", "250,000", "0,00", "USD", "Aktiensplit 3:2 MSFT"),
        ("09.06.2026", "US0378331005", "VERKAUF", "200,000", "47.860,00", "USD", "Verkauf 200 AAPL"),
        # Both legs booked. This is what correct looks like.
        ("12.06.2026", "US00SPACEX19", "FUSION", "-1.000,000", "0,00", "USD", "Fusion SPCX in TSLA"),
        ("12.06.2026", "US88160R1014", "FUSION", "2.000,000", "0,00", "USD", "Fusion Zuteilung TSLA 2:1"),
        ("15.06.2026", "US0378331005", "DIVIDENDE", "0,000", "325,00", "USD", "Dividende AAPL"),
        ("30.06.2026", "-", "GEBUEHR", "0,000", "-3.912,40", "EUR", "Verwaltungsgebuehren Q2 2026"),
    ],
}


def _cols(values, widths):
    # type: (list, list) -> str
    """Left-align into fixed columns, guaranteeing >=2 spaces between fields.

    The BHP parser splits on runs of two or more spaces, the way anyone parses
    text pulled out of a PDF. Padding here is therefore not cosmetic — it is the
    delimiter, and a column that overflows its width would corrupt the feed.
    """
    out = []
    for i, v in enumerate(values):
        v = str(v)
        if i == len(values) - 1:
            out.append(v)
        else:
            if len(v) > widths[i] - 2:
                raise ValueError("column %d overflows: %r" % (i, v))
            out.append(v.ljust(widths[i]))
    return "".join(out).rstrip()


def meridian_positions_csv(as_of):
    # type: (str) -> str
    d = as_of[5:7] + "/" + as_of[8:10] + "/" + as_of[0:4]
    lines = [
        "MERIDIAN SECURITIES LLC",
        "One Commerce Plaza, New York NY 10004",
        "",
        "ACCOUNT STATEMENT - POSITION DETAIL",
        "Account: %s" % ACCOUNT,
        "As Of: %s" % d,
        "Base Currency: USD",
        "",
        "Symbol,CUSIP,Description,Quantity,Price,Market Value,Cost Basis,Currency",
    ]
    for sym, qty, px, mv, cb in MERIDIAN_POSITIONS[as_of]:
        lines.append(
            '%s,%s,%s,"%s","%s","%s","%s",USD'
            % (sym, CUSIP_BY_SYMBOL[sym], NAME_BY_SYMBOL[sym], qty, px, mv, cb)
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def meridian_transactions_csv(period):
    # type: (str) -> str
    lines = [
        "MERIDIAN SECURITIES LLC",
        "ACCOUNT STATEMENT - ACTIVITY DETAIL",
        "Account: %s" % ACCOUNT,
        "Period: %s" % period,
        "",
        "Trade Date,Settle Date,Symbol,CUSIP,Activity,Quantity,Price,Amount,Currency,Description",
    ]
    for td, sd, sym, act, qty, px, amt, desc in MERIDIAN_TXNS[period]:
        lines.append(
            '%s,%s,%s,%s,%s,"%s","%s","%s",USD,%s'
            % (td, sd, sym, CUSIP_BY_SYMBOL.get(sym, ""), act, qty, px, amt, desc)
        )
    lines.append("")
    return "\n".join(lines) + "\n"


BHP_POS_WIDTHS = [15, 40, 14, 12, 7, 17, 16]
BHP_TXN_WIDTHS = [13, 15, 14, 13, 16, 7, 40]


def bhp_positions_txt(as_of):
    # type: (str) -> str
    d = as_of[8:10] + "." + as_of[5:7] + "." + as_of[0:4]
    name_by_isin = NAME_BY_ISIN
    lines = [
        "BANQUE HELVETIQUE PRIVEE SA",
        "Rue du Rhone 42, 1204 Geneve",
        "",
        "VERMOEGENSAUSWEIS / RELEVE DE FORTUNE",
        "",
        "Konto / Compte            : %s" % ACCOUNT,
        "Stichtag / Date           : %s" % d,
        "Referenzwaehrung / Monnaie: EUR",
        "",
        "UMRECHNUNGSKURSE / COURS DE CONVERSION",
        "USD/EUR   %s" % BHP_FX[as_of],
        "GBP/EUR   1,1840",
        "",
        "POSITIONEN / POSITIONS",
        _cols(
            ["ISIN", "Bezeichnung", "Anzahl", "Kurs", "Whrg", "Marktwert", "Wert EUR"],
            BHP_POS_WIDTHS,
        ),
    ]
    total = 0
    for isin, qty, px, ccy, mv, eur in BHP_POSITIONS[as_of]:
        lines.append(_cols([isin, name_by_isin[isin], qty, px, ccy, mv, eur], BHP_POS_WIDTHS))
        total += int(eur.replace(".", "").replace(",", ""))
    lines.append("")
    cents = "%d" % total
    formatted = "{:,.2f}".format(int(cents) / 100.0).replace(",", "#").replace(".", ",").replace("#", ".")
    lines.append("TOTAL EUR   %s" % formatted)
    lines.append("")
    return "\n".join(lines) + "\n"


def bhp_transactions_txt(period):
    # type: (str) -> str
    span = "01.04.2026 - 30.06.2026" if period == "2026Q2" else "01.01.2026 - 31.03.2026"
    lines = [
        "BANQUE HELVETIQUE PRIVEE SA",
        "",
        "BEWEGUNGEN / MOUVEMENTS",
        "",
        "Konto / Compte            : %s" % ACCOUNT,
        "Periode / Periode         : %s" % span,
        "",
        _cols(
            ["Datum", "ISIN", "Art", "Anzahl", "Betrag", "Whrg", "Text"],
            BHP_TXN_WIDTHS,
        ),
    ]
    for row in BHP_TXNS[period]:
        lines.append(_cols(list(row), BHP_TXN_WIDTHS))
    lines.append("")
    return "\n".join(lines) + "\n"


def corporate_actions_csv():
    # type: () -> str
    lines = [
        "# Corporate action reference feed",
        "# Source: Elbridge Data Services (synthetic stand-in for a vendor feed)",
        "# Retrieved: 2026-07-02T06:15:00Z",
        "ISIN,Symbol,ExDate,Type,RatioNew,RatioOld,RelatedISIN,OldSymbol,NewSymbol,Description",
    ]
    for isin, sym, ex, kind, num, den, rel, old_sym, new_sym, desc in CORPORATE_ACTIONS:
        lines.append(
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s"
            % (isin, sym, ex, kind, num, den, rel, old_sym, new_sym, desc)
        )
    lines.append("")
    return "\n".join(lines) + "\n"


FILES = [
    ("corporate_actions_2026Q2.csv", corporate_actions_csv),
    ("meridian_positions_2026-03-31.csv", lambda: meridian_positions_csv(PRIOR)),
    ("meridian_positions_2026-06-30.csv", lambda: meridian_positions_csv(CURRENT)),
    ("meridian_transactions_2026Q1.csv", lambda: meridian_transactions_csv("2026Q1")),
    ("meridian_transactions_2026Q2.csv", lambda: meridian_transactions_csv("2026Q2")),
    ("bhp_vermoegensausweis_2026-03-31.txt", lambda: bhp_positions_txt(PRIOR)),
    ("bhp_vermoegensausweis_2026-06-30.txt", lambda: bhp_positions_txt(CURRENT)),
    ("bhp_bewegungen_2026Q1.txt", lambda: bhp_transactions_txt("2026Q1")),
    ("bhp_bewegungen_2026Q2.txt", lambda: bhp_transactions_txt("2026Q2")),
]


def write_all(target_dir=None):
    # type: (str) -> list
    """Write every statement. Returns the list of paths written."""
    target = target_dir or STATEMENTS
    if not os.path.isdir(target):
        os.makedirs(target)
    written = []
    for name, render in FILES:
        path = os.path.join(target, name)
        with open(path, "w") as fh:
            fh.write(render())
        written.append(path)
    return written


# --- the contract ------------------------------------------------------------
# Every break seeded above, and nothing else. tests/test_pipeline.py asserts the
# pipeline's output matches this set exactly — a missed break and an invented one
# are both failures, and in this domain the invented one is the worse of the two.
EXPECTED_BREAKS = [
    # NVDA: 4-for-1 split applied at Meridian, not at BHP.
    ("CORP_ACTION_UNAPPLIED", "US67066G1040", "critical"),
    ("CROSS_CUSTODIAN_QTY", "US67066G1040", "critical"),
    # TSLA/SPCX merger: Meridian removed the target and never credited the
    # acquirer. Only the reference feed can see this — see MERGER_UNPROCESSED.
    ("MERGER_UNPROCESSED", "US88160R1014", "critical"),
    ("CROSS_CUSTODIAN_QTY", "US88160R1014", "critical"),
    ("COST_BASIS_DRIFT", "US0378331005", "high"),
    ("FX_INCONSISTENT", "US0378331005", "high"),
    ("QTY_ROLLFORWARD", "US92857W3088", "high"),
    ("CROSS_CUSTODIAN_QTY", "US92857W3088", "high"),
    # META: ticker still printed as FB. Nothing is misstated; severity capped.
    ("IDENTIFIER_STALE", "US30303M1027", "medium"),
    ("MISSING_FEE_ACCRUAL", "", "medium"),
]

# Positions that must produce NO break. Half of a detector's value is its silence
# — a desk that flags everything gets switched off in a week.
EXPECTED_CLEAN = [
    # MSFT is the load-bearing one. It was bought into during the window AND put
    # through a 3-for-2 split in the same window, and both custodians handled all
    # of it correctly. A detector that pattern-matches "share counts changed near
    # a corporate action" flags this. The desk must not.
    "US5949181045",
    "NL0010273215",  # ASML: BHP-only, EUR-native, no FX conversion to disagree with
    "DE0007164600",  # SAP:  BHP-only, EUR-native
    # SPCX vanished from both statements — correctly, because it was merged out
    # of existence. A "position disappeared" rule that did not consult the
    # activity file would report this twice, at critical, on a portfolio where
    # nothing whatsoever is wrong. That is the false positive that gets a desk
    # switched off, so it is a required silence, not an incidental one.
    "US00SPACEX19",
]

# Findings that exist and must NOT be raised against the securities above. Kept
# separate from EXPECTED_CLEAN because "this security has no break" and "this
# rule did not fire on this security" are different assertions, and the second
# is the one that catches a rule quietly matching on the wrong key.
EXPECTED_NOT_RAISED = [
    ("POSITION_DISAPPEARED", "US00SPACEX19"),  # explained by the merger
    ("CORP_ACTION_UNAPPLIED", "US5949181045"),  # 3-for-2 applied at both
    ("QTY_ROLLFORWARD", "US5949181045"),        # bought into AND split, still balances
    ("CROSS_CUSTODIAN_QTY", "US30303M1027"),    # stale ticker, identical quantities
    ("FX_INCONSISTENT", "NL0010273215"),        # EUR holding in a EUR statement
    ("COST_BASIS_DRIFT", "US88160R1014"),       # basis internally consistent at Meridian
]


if __name__ == "__main__":
    paths = write_all()
    for p in paths:
        sys.stdout.write("wrote %s\n" % os.path.relpath(p, ROOT))
