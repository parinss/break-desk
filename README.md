# Break Desk

Multi-custodian position reconciliation for wealth managers. Two custodians, two
statement formats, two number locales, one quarter — and every figure in the
output traceable to a file and a line number in a source document.

```
python3 scripts/generate.py     # write the synthetic statements
python3 scripts/build.py        # reconcile -> public/data/breaks.json
python3 serve.py                # http://localhost:8110/desk/
python3 -m unittest discover -s tests
```

Python 3.9, standard library only. No dependencies, no build step, no
`node_modules`. The optional prose layer uses the Anthropic SDK; without it the
system produces complete reports from templates.

---

## What it does

A wealth manager running one mandate across two custodians gets two statements
that disagree. Somebody reconciles them by hand every quarter. This finds the
disagreements, prices them, ranks them by what is actually at risk, and shows
the source line behind every number.

On the demo period it reports **ten findings across nine instruments**:

| Finding | Instrument | At risk |
|---|---|---|
| `CORP_ACTION_UNAPPLIED` | NVIDIA | USD 285,360.00 |
| `MERGER_UNPROCESSED` | Tesla / SpaceX | USD 542,600.00 |
| `CROSS_CUSTODIAN_QTY` | NVIDIA, Tesla, Vodafone | USD 835,190.00 |
| `COST_BASIS_DRIFT` | Apple | USD 6,756.00 |
| `FX_INCONSISTENT` | Apple | EUR 7,143.11 |
| `QTY_ROLLFORWARD` | Vodafone | USD 7,230.00 |
| `IDENTIFIER_STALE` | Meta (still printed as FB) | USD 329,220.00 |
| `MISSING_FEE_ACCRUAL` | account level | USD 4,287.50 |

Four instruments are examined and reported clean, and **that half matters more**.
See "What it deliberately does not report" below.

---

## The four things worth looking at

### 1. Every figure carries its provenance

`Provenance(file, line, excerpt)` is a required field on every Position and
Transaction, not optional metadata, and the detectors propagate it onto every
finding. Click any finding in the UI and it expands to the literal source lines
it was derived from:

```
corporate_actions_2026Q2.csv:5
  US67066G1040,NVDA,2026-05-18,SPLIT,4,1,,,,NVIDIA Corporation 4-for-1 common stock split

bhp_vermoegensausweis_2026-06-30.txt:17
  US67066G1040   NVIDIA CORPORATION   800,000   118,90   USD   95.120,00   87.225,04
```

A break the reviewer cannot trace back to a page and a line is an assertion, not
a finding — and an assertion is not deployable in a regulated shop.

### 2. Corporate actions are first-class, and backed by a reference feed

Detection runs against an independent corporate-action record, not against the
custodians' own disagreement. Inferring "a 4-for-1 split happened" from two
custodians disagreeing 4:1 is circular — the disagreement is the thing being
explained. An external record turns it into *this split occurred on this date;
custodian A applied it, custodian B did not*.

Three shapes are handled separately, because they fail differently:

- **Splits** scale one position and must leave total cost basis untouched.
- **Mergers** have two legs that fail independently — the target must go to zero
  and the acquirer must be credited at the exchange ratio.
- **Ticker changes** move no money at all, so every arithmetic rule is correctly
  silent and a control finding reports the staleness on its own terms.

Ratio inference uses an **allowlist of ratios issuers actually declare**, not a
bounds check. Vodafone's 5,000 → 4,400 reduces to a perfectly tidy 22:25, and a
"both terms under thirty" test would call that a split — explaining away a real
600-share hole. No issuer has declared a 22-for-25 split.

### 3. The merger case is the one the architecture exists for

Meridian removed the merger target and never credited the replacement shares.
Two thousand shares of the acquirer — USD 542,600 — simply left the client's
holdings.

**Nothing that checks a custodian against itself can see this.** The position
file and the activity file agree with each other perfectly, because both are
missing the same leg. Quantity rollforward passes. Position-disappeared passes,
correctly, because the activity file does account for the target. Only an
external record of the action supplies the missing expectation.

That is asserted directly, as a test:

```python
def test_rollforward_cannot_see_the_case_this_rule_exists_for(self):
    self.assertEqual(detect_qty_rollforward(self.prior, current, txns), [])
    self.assertEqual(detect_position_disappeared(self.prior, current, txns), [])
    self.assertEqual(len(detect_mergers(self.prior, current, txns, [self.act])), 1)
```

### 4. The language model writes prose and cannot touch a number

A model writes two fields per finding — `narrative` and `proposed_fix` — and
nothing else. That is enforced twice, at runtime, on every finding:

- **Structurally.** Every non-prose field is fingerprinted before the call and
  compared after. Any difference raises `NarrativeTamperError` and the pipeline
  stops.
- **Semantically.** Prose is scanned for numeric tokens, and every one must
  already appear in the computed detail. A model that invents a figure has its
  narrative rejected and the deterministic template used instead — recorded in
  the report, never swallowed.

Both are proved by hostile stubs in `tests/test_explain.py`: one that fabricates
`EUR 9,999,999.00`, one that reaches past the prose fields and edits a computed
one. The first falls back; the second stops the run.

**With no API key configured, the system produces complete, correct, shippable
reports.** The model is an upgrade to the prose, never a dependency of the
finding.

---

## What it deliberately does not report

A reconciliation desk that flags everything is worse than none at all. It gets
switched off in a fortnight and the real break sails through in week three. Each
rule is therefore tested twice — once that it fires, once that it stays silent on
the neighbouring case that merely resembles it.

Four required silences in the demo data:

- **Microsoft** was bought into *and* put through a 3-for-2 split in the same
  quarter, correctly, at both custodians. A rule that pattern-matches "share
  count changed near a corporate action" flags it. This one is load-bearing.
- **SpaceX** vanished from both statements — correctly, having been merged out of
  existence. A naive "position disappeared" rule reports this twice, at
  critical, on a portfolio where nothing is wrong.
- **ASML and SAP** are EUR holdings in a EUR statement. There is no conversion to
  be wrong about, and inventing a 1.0000 rate for them would be inventing a
  finding.
- **Meta's stale ticker** produces exactly one control finding and no phantom
  quantity break, because the line was matched on CUSIP.

Severity is computed from value at risk, never assigned per rule — with a cap for
control findings where the whole position is notionally exposed but nothing is
actually misstated. Ranking a stale ticker `critical` alongside a genuinely
missing half-million of stock is the fastest way to teach an operations team to
ignore the queue.

---

## Design decisions

**Decimal everywhere, never float.** `0.1 + 0.2` is not `0.3`, and an engine that
tolerates that cannot tell a real break from its own rounding. `tests/test_pipeline.py`
walks the serialised report and fails on any float.

**Parsers refuse rather than default.** An unparseable figure raises; an
unmappable instrument raises. A silent zero is indistinguishable from a genuinely
flat position, and a report that quietly omits a holding is worse than no report
— the reviewer has no way to know it is incomplete.

**`None` is not zero.** One custodian reports no cost basis at all. Reading that
as zero would produce a critical finding on every one of its holdings on the
first run in front of a client, all of them fictional.

**Detectors are pure.** No file access, no clock, no network, no global state.
That is what makes each rule testable one at a time, which is the only way to
have confidence in its silence.

**The report publishes its own rule list and its clean list.** A reviewer's first
question is never "what did you find" — it is "what did you look for, and what
did you clear". A findings list with neither cannot be falsified.

---

## The data is synthetic, and you can check that

Every statement in `data/statements/` is generated by `scripts/generate.py`,
which is committed. No client data has ever been in this repository. The
generator also holds the manifest of seeded errors, and the integration test
holds the pipeline to it exactly — a missed break and an invented one are both
failures, and in this domain the invented one is worse.

The formats are modelled on real ones: a US custodian's CSV extract in
`1,234.56` with accounting parentheses, and a Swiss bank's PDF text in
`1.234,56` with German activity codes and columns held apart by runs of spaces.

---

## Layout

```
scripts/
  money.py         parsing and Decimal arithmetic across two locales
  model.py         canonical model; Provenance is a required field
  securities.py    ISIN / CUSIP / ticker crosswalk, including former tickers
  normalize.py     custodian statements in, canonical model out
  corpactions.py   corporate-action reasoning, testable with no files
  breaks.py        twelve pure detectors
  explain.py       prose layer, with the containment enforced at runtime
  generate.py      synthetic statements + the manifest of seeded errors
  build.py         the pipeline
serve.py           stdlib server on :8110/desk
public/            two-pane console, no framework and no build step
tests/             286 tests, stdlib unittest
```

## Tests

```
$ python3 -m unittest discover -s tests
Ran 286 tests in 3.1s
OK
```

Unit tests for arithmetic, parsing, and each detector's fire-and-silence pair.
Integration tests that run the pipeline over freshly generated statements and
assert the manifest exactly, that every citation in the finished report points at
a line that says what it claims, that the output is byte-identical run to run,
and that a hostile language model degrades the prose and nothing else.
`tests/test_serve.py` speaks HTTP over a real socket, including a symlink that
escapes the document root without a `..` anywhere in the request.

---

## Who wrote this

Parin Shah — Engineering Manager, Market Data at Addepar (2022–2026), running the
team behind the security master, the identifier crosswalk, and the
corporate-action and pricing feeds under a wealth-management platform, across 20+
vendors.

That is the half of this repository I did not have to look up. The reference-data
reasoning here — CUSIP beating ticker when the two disagree, former tickers that
still have to resolve, an independent corporate-action record rather than an
inference from the custodians' own disagreement — is the failure mode I spent
four years watching. The desk workflow layered on top of it is modelled, not
lived.

parin.sunil.shah@gmail.com
