# Break Desk — working notes

Multi-custodian position reconciliation. Three custodian statement formats (US
CSV, Swiss PDF text, ISO 15022 SWIFT), three number locales, one quarter, and
every figure in the output traceable to a file and a line number in a source
document.

```
python3 scripts/generate.py            # write the synthetic statements
python3 scripts/build.py               # reconcile -> public/data/breaks.json
python3 scripts/build.py --no-llm      # same, templates only
python3 serve.py                       # http://localhost:8110/desk/
python3 scripts/scale.py               # the pipeline over a 5,000-position book
python3 -m unittest discover -s tests  # 401 tests
```

`scripts/` and the repo root go on `sys.path`, so modules import by their own
names (`import breaks`, not `from scripts import breaks`). `tests/_util.py`
does that bootstrap for the suite.

## Rules that are not negotiable

**Python 3.9.** No `match`, no `X | Y` runtime unions. `from __future__ import
annotations`, `typing.Optional/List/Dict`, `# type:` comments where needed.

**Decimal, never float.** An engine that tolerates `0.1 + 0.2 != 0.3` cannot
tell a real break from its own rounding. `tests/test_pipeline.py` walks the
serialised report and fails on any float that appears anywhere in it.

**Provenance is required, not optional.** Every `Position` and `Transaction`
carries `Provenance(file, line, excerpt)`, and detectors propagate it onto every
`Break`. A finding the reviewer cannot trace to a line is an assertion, not a
finding. A test opens each cited file and checks the cited line literally
matches the recorded excerpt — a citation that drifts by one row is worse than
no citation, because it looks authoritative and sends the reviewer to the wrong
place.

**Parsers refuse; they never default.** Unparseable figure raises. Unmappable
instrument raises. A silent zero is indistinguishable from a genuinely flat
position, and a report that quietly drops a holding is worse than no report.

**`None` is not `Decimal("0")`.** One custodian reports no cost basis at all.

**Detectors are pure.** No file access, no clock, no network, no global state.
That is what makes each rule testable alone, which is the only way to have
confidence in its silence. `tests/test_corpactions.py` greps the module for
`open(`, `datetime.now`, `date.today`, `requests`, `os.environ`.

**Severity comes from value at risk**, never assigned per rule. Control findings
that risk the whole position but misstate nothing get `_capped_severity`.

**Queue order is severity band, then exposure descending.** The band is coarse
on purpose — everything over a hundred thousand is critical — so inside it money
decides. It used to be the rule's name, which put the largest exposure in the
book wherever its rule fell in the alphabet. Unpriced findings sort last within
their band: not small, just not triageable by size. Magnitudes are compared
across currencies without conversion, which is named in `_sorted` rather than
hidden — it is exactly as currency-blind as the banding it refines.

**The model writes prose and nothing else.** `narrative` and `proposed_fix`
only. Enforced twice at runtime: a structural fingerprint of every other field
before and after the call, and a check that every numeric token in the prose
already appears in the computed detail. Tampering raises `NarrativeTamperError`
and stops the run; an invented figure falls back to the template and is counted
in `narrative_fallbacks`. With no API key the system produces complete reports.

`explain.PROSE_FIELDS` names the two-field rule once. `public/app.js` may not
call `parseFloat`, `toFixed`, `Number` or `parseInt` — every figure crosses into
the browser as a string because JSON has no decimal type, and `group()` adds
thousands separators to the digits rather than to a parsed number. Asserted by
grep in `tests/test_serve.py`.

Claude API specifics that bite: model is `claude-opus-5`, thinking is
`{"type": "adaptive"}`, JSON via `output_config.format`. `budget_tokens`,
`temperature`, and assistant prefill all return 400 on this model.

## Test discipline

Every rule is tested twice — that it fires, and that it stays **silent** on the
neighbouring case that merely resembles it. A desk that flags everything gets
switched off in a fortnight and the real break sails through in week three.

The five silences the demo data exists to prove, all load-bearing:

- **Microsoft** — bought into *and* 3-for-2 split in the same quarter, correctly,
  at all three custodians, in three formats, on two statement bases. Anything
  pattern-matching "share count moved near a corporate action" flags it.
- **SpaceX** — gone from both statements, correctly, having been merged out of
  existence. A naive position-disappeared rule reports it twice at critical.
- **ASML, SAP** — EUR holdings in a EUR statement. No conversion to be wrong
  about; inventing a 1.0000 rate would be inventing a finding.
- **Meta** — one control finding for the stale ticker and no phantom quantity
  break, because the line matched on CUSIP.
- **Apple across statement bases** — the expensive one. Northgate reports 1,800
  on a trade-date basis, the others 1,300 on settled. USD 119,450 apart on the
  largest holding, and not a break: one trade dated 29 June settles 2 July. The
  quantity rule nets what the bases explain and reports only the residual; it
  does not go quiet on mismatched pairs, which would silence the comparison that
  most needs checking. `STATEMENT_BASIS_MISMATCH` reports the real problem — the
  comparison — capped, because nothing is missing, it is in flight.

Ratio inference uses an **allowlist of ratios issuers actually declare**, not a
bounds check. Vodafone's 5,000 -> 4,400 reduces to a tidy 22:25, which a "both
terms under thirty" test accepts — and reading that as a split explains away a
real 600-share hole.

## Scale

`scripts/scale.py` builds a book of any size across the three custodians and
seeds a fixed set of ten breaks into it, manifest written before the run. It
found three things and the slow one was the least interesting:

- **A latent crash.** `_cites` summarised sources as `(+27 more)` — a figure the
  prose layer worked out by subtraction, so the invented-figure guard rejected
  it and stopped the pipeline. On the demo book no finding has more than three
  distinct citations, so that branch had never executed. It now names the total,
  which is a fact about the finding and the number a reviewer wants anyway.
- **Two quadratics.** Three detectors filtered the whole movement list inside the
  loop over positions; `_txns_by_isin` groups once. Worse was `in_flight_qty`,
  asked once per instrument per mismatched-basis custodian pair; `in_flight_index`
  walks each list once. 20,000 position lines: 8.5s -> 0.28s, curve now linear.
- **The ranking**, above.

`tests/test_scale.py` asserts complexity by **counting scans, not seconds** — a
list subclass recording its own iteration, so the claim is a property of the
algorithm rather than of the machine CI drew. The counter is itself tested
against a deliberately quadratic scan; a counter that cannot count would make
every one of those tests pass.

The identifier scheme is `USSCALE00033` — ISIN-shaped, unmistakably not one, and
it refuses past 9,999 instruments rather than colliding.

## The diagram

`docs/architecture.svg` is hand-authored, self-contained, and drawn for both
themes. `tests/test_diagram.py` checks it against the code: detector count vs
`len(RULES)`, rule names vs the published list, `+ nine more` vs the remainder,
parsers and model classes vs the modules, custodians vs what `load_period`
reads, and the four figures in its queue vs `breaks.json` by value and severity.
The provenance example is verified by opening the statement and reading the line.

Stale prose reads oddly and somebody fixes it. A stale picture keeps looking
authoritative.

## Data

All statements are synthetic, generated by `scripts/generate.py`, which is
committed so a reader can verify the claim. **No client data goes in this
repository, ever** — not in a fixture, not in a test, not in a screenshot.
`generate.py` also holds `EXPECTED_BREAKS`, the manifest the integration test
holds the pipeline to exactly: a missed break and an invented one are both
failures, and the invented one is worse.

## ISO 15022

MT535 (holdings) and MT536 (movements), parsed in `normalize.py`. The block
grammar is a stack machine: unbalanced or mismatched `:16R:`/`:16S:` raises,
because a dropped close re-parents every block after it and silently moves a
holding into another instrument's sub-balance rather than failing.

Things this parser refuses rather than guesses: a quantity typed `FAMT` (a bond's
notional, not a unit count, and the denomination needed to convert it is not in
the message); a price typed `PRCT`; a `:22F::TRAN//CORP` movement with no
`:22F::CAEV//` event code, since a split and a merger allocation reconcile
against different things; a holding priced in one currency and valued in another;
and `:17B::ACTI//N` alongside a `STAT` block, so a truncated download cannot read
as a quiet quarter.

`parse_swift_decimal` is the strictest of the three number parsers. `1800` is
refused — the comma is mandatory — and `1,800` is **one point eight**. That last
one is why the strictness is not pedantry: the same string is an ordinary US
number meaning something a thousand times larger, and no tolerance catches a
three-order-of-magnitude misreading.

## Not built yet

MT548 (settlement status) and MT564 (corporate action notification). SSI/PSET
mismatch priced in CSDR penalty EUR — `:94F::SAFE//NCSD/` already carries the
place of safekeeping, so the field is there when the rule is written.
