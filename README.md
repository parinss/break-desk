# Break Desk

[![tests and deploy](https://github.com/parinss/break-desk/actions/workflows/pages.yml/badge.svg)](https://github.com/parinss/break-desk/actions/workflows/pages.yml)

Multi-custodian position reconciliation for wealth managers. Three custodians,
three statement formats — a US CSV, a Swiss PDF extract and ISO 15022 SWIFT —
three number locales, one quarter, and every figure in the output traceable to a
file and a line number in a source document.

**[Open the desk →](https://parinss.github.io/break-desk/)** — the live queue on
the demo quarter. Pick any finding and it expands to the custodians' own lines.

![The console: a queue of findings ranked worst first on the left, and on the
right the selected finding — Tesla, USD 542,600 at risk — with its assessment,
its proposed action, the computed figures behind it, and the two custodian
statement lines it was derived from, quoted verbatim with their line
numbers.](docs/console.png)

Every figure on that screen is synthetic and reproducible: `generate.py` wrote
the statements, `build.py` reconciled them, and both are committed.

To run it yourself:

```
python3 scripts/generate.py     # write the synthetic statements
python3 scripts/build.py        # reconcile -> public/data/breaks.json
python3 serve.py                # http://localhost:8110/desk/
python3 scripts/scale.py        # the same pipeline over a 5,000-position book
python3 -m unittest discover -s tests
```

Python 3.9, standard library only. No dependencies, no build step, no
`node_modules`. The optional prose layer uses the Anthropic SDK; without it the
system produces complete reports from templates.

---

## The name

A **break** is the industry's word for it: two records that are supposed to agree
and do not. Not an error, not an exception — a break, because something that
should have been joined has come apart, and somebody has to find where.

A **desk** is where a job gets done all day by the people who own it. Asset
managers have a trading desk and an operations desk; the reconciliations desk is
where breaks land, get triaged, get chased with a custodian, and get closed.

So the name is the workflow rather than the technology. This is not a
reconciliation *engine* that emits a file — it is the thing an operations team
would sit in front of on the first working day after quarter end, ordered the way
they would want to work it, with the evidence attached to each row because the
next step is always a phone call to a custodian who will ask what you are looking
at.

---

## What it does

A wealth manager running one mandate across several custodians gets statements
that disagree. Somebody reconciles them by hand every quarter. This finds the
disagreements, prices them, ranks them by what is actually at risk, and shows
the source line behind every number.

On the demo period it reports **eleven findings across nine instruments**:

| Finding | Instrument | At risk |
|---|---|---|
| `CORP_ACTION_UNAPPLIED` | NVIDIA | USD 285,360.00 |
| `MERGER_UNPROCESSED` | Tesla / SpaceX | USD 542,600.00 |
| `CROSS_CUSTODIAN_QTY` | NVIDIA, Tesla, Vodafone | USD 835,190.00 |
| `COST_BASIS_DRIFT` | Apple | USD 6,756.00 |
| `FX_INCONSISTENT` | Apple | EUR 7,143.11 |
| `QTY_ROLLFORWARD` | Vodafone | USD 7,230.00 |
| `IDENTIFIER_STALE` | Meta (still printed as FB) | USD 329,220.00 |
| `STATEMENT_BASIS_MISMATCH` | account level | USD 119,450.00 |
| `MISSING_FEE_ACCRUAL` | account level | USD 4,287.50 |

Four instruments are examined and reported clean, and **that half matters more**.
See "What it deliberately does not report" below.

---

## How it works

![Architecture: three statement formats parse into one canonical model in which
provenance is a required field; thirteen pure detectors compare that model
against itself and against an independent corporate-action reference feed; the
findings are ranked by severity band and then by value at risk; a language model
writes two prose fields and can touch nothing else.](docs/architecture.svg)

Three shapes fan in to one model, and the fan-in is the whole trick: every
cross-custodian rule takes a sequence of snapshots and knows nothing about where
they came from, so a fourth custodian is a parser and no more. The reference feed
enters detection from outside the custodians on purpose, because a corporate
action inferred from two custodians disagreeing is an explanation built out of
the thing it is explaining. The language model hangs off the side rather than
sitting in the pipe, because it is not in the path of any number.

---

## The five things worth looking at

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

### 5. A third custodian was a parser, and nothing else

`normalize.py` claims in its own docstring that adding a custodian means adding a
parser and touching nothing else. Northgate is where that claim gets tested
rather than asserted: ISO 15022 MT535 and MT536, a tree of `:16R:`/`:16S:` blocks
rather than a table, fields keyed by tag and qualifier rather than by column,
quantities as magnitudes with the direction in `:22H::REDE//`, and a third number
convention where `1,800` means one point eight.

Every cross-custodian detector already took a sequence of snapshots and compared
them pairwise, so the third custodian needed no change to any of them. What it
*did* need was one idea the other two never forced: **statement basis.**

```
:22F::STBA//TRAD
```

That single field is why Northgate reports 1,800 Apple shares and the others
report 1,300, and why none of them is wrong. The quantity rule now measures the
difference against what the bases predict and reports only the residual — it does
not go quiet on mismatched pairs, which would silence exactly the comparison that
most needs checking.

---

## What it deliberately does not report

A reconciliation desk that flags everything is worse than none at all. It gets
switched off in a fortnight and the real break sails through in week three. Each
rule is therefore tested twice — once that it fires, once that it stays silent on
the neighbouring case that merely resembles it.

Five required silences in the demo data:

- **Microsoft** was bought into *and* put through a 3-for-2 split in the same
  quarter, correctly, at all three custodians — in three different file formats,
  on two different statement bases. A rule that pattern-matches "share count
  changed near a corporate action" flags it. This one is load-bearing.
- **SpaceX** vanished from both statements — correctly, having been merged out of
  existence. A naive "position disappeared" rule reports this twice, at
  critical, on a portfolio where nothing is wrong.
- **ASML and SAP** are EUR holdings in a EUR statement. There is no conversion to
  be wrong about, and inventing a 1.0000 rate for them would be inventing a
  finding.
- **Meta's stale ticker** produces exactly one control finding and no phantom
  quantity break, because the line was matched on CUSIP.
- **Apple across a trade-date and a settled-date statement.** Northgate reports
  1,800 shares; the other two report 1,300. A 500-share, **USD 119,450**
  disagreement on the largest holding in the book — and not a break. One trade,
  executed 29 June and settling 2 July, is inside the trade-date statement and
  outside the settled ones. All three custodians are correct.

  This is the most expensive silence in the demo. A quantity rule that does not
  know about statement basis reports a six-figure difference that nobody can
  action, on the position an operations team looks at first. What the desk
  reports instead is the thing that *is* wrong — the comparison — as a capped
  control finding, with the netting shown on the face of it.

Severity is computed from value at risk, never assigned per rule — with a cap for
control findings where the whole position is notionally exposed but nothing is
actually misstated. Ranking a stale ticker `critical` alongside a genuinely
missing half-million of stock is the fastest way to teach an operations team to
ignore the queue.

---

## Under load

Nine instruments is a demo. `scripts/scale.py` builds a synthetic book of any
size across the same three custodians, seeds a fixed set of ten breaks into it,
and runs the real pipeline — same detectors, same prose layer, same report.

```
$ python3 scripts/scale.py --positions 20000
book        6666 instruments, 19997 position lines, 20669 movements
detect        0.28s
pipeline      0.43s   (detect + prose + report + 645 kB)
findings    11, manifest matched
```

It found three things, and the slow one was the least interesting.

**A latent crash, on the first run.** The prose templates summarise citations and
used to end `(+27 more)` — a figure the prose layer worked out by subtraction, so
the invented-figure guard rejected it and stopped the pipeline. On the demo book
no finding ever has more than three distinct citations, so that branch had never
once executed. A real book hits it on the first rollforward against a quarter of
trading. The guard was right and the template was wrong.

**Two quadratics.** Three detectors filtered the whole movement list inside the
loop over positions — O(positions × movements), the natural way to write it, and
invisible at nine instruments. The expensive one was worse: every pair of
custodians on different statement bases asked "what is in flight in this
security?" once per instrument, and answered it by walking every movement the
custodian had reported.

| Position lines | Before | After |
|---|---|---|
| 5,000 | 0.39 s | 0.04 s |
| 10,000 | 2.24 s | 0.10 s |
| 20,000 | 8.47 s | 0.28 s |

**A queue that had stopped meaning anything.** This is the one that mattered.
Severity is a band and a band is coarse on purpose — everything above a hundred
thousand is critical. Inside the band the sort key was the rule's *name*, so the
largest exposure in the book landed wherever its rule happened to fall in the
alphabet. On eleven findings that is cosmetic. On a real book the critical band
alone runs to dozens of rows and an operations team works a queue from the top.

Exposure now orders within the band, and the demo report reads better for it: the
merger and the cross-custodian disagreement it causes now sit adjacent at the top
on the same figure, which is one error seen through two windows rather than two
separate pieces of work.

The complexity claims are asserted by **counting scans, not seconds** — a list
subclass that records its own iteration, so the test states a property of the
algorithm rather than a property of the machine CI happened to run on. A test
that fails when the box is busy teaches people to re-run it, and a test people
re-run until it passes is not a test.

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

**Even the diagram is tested.** `docs/architecture.svg` names modules, functions,
rules and figures, so `tests/test_diagram.py` checks each of them against the
code and against the report the pipeline produces — the four amounts in its queue
are compared to `breaks.json` by value and by severity, and its provenance
example is verified by opening the statement and reading the line. Stale prose
reads oddly and gets fixed; a stale picture keeps looking authoritative.

---

## The data is synthetic, and you can check that

Every statement in `data/statements/` is generated by `scripts/generate.py`,
which is committed. No client data has ever been in this repository. The
generator also holds the manifest of seeded errors, and the integration test
holds the pipeline to it exactly — a missed break and an invented one are both
failures, and in this domain the invented one is worse.

The formats are modelled on real ones: a US custodian's CSV extract in
`1,234.56` with accounting parentheses; a Swiss bank's PDF text in `1.234,56`
with German activity codes and columns held apart by runs of spaces; and ISO
15022 SWIFT — MT535 holdings and MT536 movements — in `1800,`, where the decimal
comma is mandatory, thousands separators are forbidden, and `1,800` means one
point eight.

The same discipline applies to the large book. `scripts/scale.py` invents
identifiers of the form `USSCALE00033` — ISIN-shaped, and unmistakably not one —
for the same reason the demo's SpaceX ISIN is deliberately absurd: an invented
identifier that could be confused with a real instrument is a liability in a
repository anyone can clone.

---

## Layout

```
scripts/
  money.py         parsing and Decimal arithmetic across three locales
  model.py         canonical model; Provenance is a required field
  securities.py    ISIN / CUSIP / ticker crosswalk, including former tickers
  normalize.py     custodian statements in, canonical model out
  corpactions.py   corporate-action reasoning, testable with no files
  breaks.py        thirteen pure detectors
  explain.py       prose layer, with the containment enforced at runtime
  generate.py      synthetic statements + the manifest of seeded errors
  scale.py         the same pipeline over a book of any size, and its manifest
  build.py         the pipeline
serve.py           stdlib server on :8110/desk
public/            two-pane console, no framework and no build step
docs/              architecture diagram, checked against the code by the suite
tests/             401 tests, stdlib unittest
LICENSE            Apache-2.0
```

## Tests

```
$ python3 -m unittest discover -s tests
Ran 401 tests in 4.1s
OK
```

Unit tests for arithmetic, parsing, and each detector's fire-and-silence pair.
Integration tests that run the pipeline over freshly generated statements and
assert the manifest exactly, that every citation in the finished report points at
a line that says what it claims, that the output is byte-identical run to run,
and that a hostile language model degrades the prose and nothing else.
`tests/test_serve.py` speaks HTTP over a real socket, including a symlink that
escapes the document root without a `..` anywhere in the request.
`tests/test_scale.py` holds all of it to a book of five thousand positions.

---

## License

Apache-2.0. Take the detectors, take the parsers, take the provenance model —
the ISO 15022 refusals in `normalize.py` are the part most worth stealing, and
they cost a fortnight to get right. Apache rather than MIT for the explicit
patent grant, so nobody has to wonder.

The statements under `data/` are synthetic and carry no rights worth asserting.

---

## Who wrote this

Parin Shah — Engineering Lead, Reference Data at Addepar (2022–2026), running
the team behind the security master, the identifier crosswalk, and the
corporate-action and pricing feeds under a wealth-management platform, across
20+ vendors.

The reference-data reasoning in here is the part I did not have to look up.
CUSIP beating ticker when the two disagree, former tickers that still have to
resolve, an independent corporate-action record rather than an inference from
the custodians' own disagreement — each of those is a rule written against a
particular way I watched the data go wrong. The desk workflow layered on top of
it is modelled, not lived.

parin.sunil.shah@gmail.com
