#!/usr/bin/env python3
"""
explain.py — turns a detected break into English, without letting English touch
the arithmetic.

Every number in this system is produced by `breaks.py` and is reproducible from
the source documents. The language model's entire job is to say what the numbers
mean to someone triaging a queue at 08:00. It writes two strings and nothing
else.

That is a claim, and claims of this shape are usually enforced by nothing but the
author's good intentions. Here it is enforced twice, at runtime, on every break:

  1. **Structural.** The writer returns two strings. They are placed into
     `narrative` and `proposed_fix` via `dataclasses.replace`. Every other field
     is fingerprinted before the call and compared after; any difference raises
     `NarrativeTamperError` and the pipeline stops. The model is not given a path
     to a numeric field, and if one ever appeared, the fingerprint would catch it.

  2. **Semantic.** Prose is scanned for numeric tokens. Every one must already
     appear somewhere in the break's computed detail. A model that invents a
     figure — the failure mode that actually matters, because an invented figure
     is indistinguishable from a real one to the reader — has its narrative
     rejected and the deterministic template used instead. The rejection is
     recorded, not swallowed.

The consequence worth stating to a buyer: with no API key configured, this module
produces complete, correct, shippable reports. The model is an upgrade to the
prose, never a dependency of the finding.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from model import Break, to_json

MODEL = "claude-opus-5"

# Digit groups, with or without separators: 1,300.000  1.300,000  4  0.9170
_NUMERIC = re.compile(r"\d[\d,. ']*")


class NarrativeTamperError(RuntimeError):
    """Raised when an explanation pass changed anything but the two prose fields."""


class InventedFigureError(ValueError):
    """Raised when prose contains a number absent from the computed detail."""


# --- the guarantee -----------------------------------------------------------


# The only two fields a language model may ever write. Named once rather than
# spelled out at each site: the rule is enforced here, relied on by the tamper
# check, drawn in docs/architecture.svg, and asserted in three test files. A
# claim repeated in five places as a literal is a claim that will one day be
# true in four of them.
PROSE_FIELDS = ("narrative", "proposed_fix")


def fingerprint(brk):
    # type: (Break) -> str
    """Everything about a break except the two fields prose is allowed to fill."""
    blanked = dict((field, "") for field in PROSE_FIELDS)
    return to_json(replace(brk, **blanked), indent=None)


def _normalise_number(token):
    # type: (str) -> str
    """
    Reduce a numeric token to comparable digits.

    `1,300.000`, `1.300,000` and `1300` must all compare equal: the same figure
    is written three ways across this repository's two custodians, and a check
    that treated them as different would reject correct prose.
    """
    digits = re.sub(r"[^\d]", "", token)
    return digits.lstrip("0") or "0"


def _numbers_in(text):
    # type: (str) -> List[str]
    return [_normalise_number(m) for m in _NUMERIC.findall(text or "")]


def permitted_numbers(brk):
    # type: (Break) -> set
    """Every figure the desk actually computed, in normalised form."""
    corpus = [brk.isin, brk.security, brk.custodian, brk.account, brk.kind,
              brk.severity, brk.as_of.isoformat()]
    corpus.extend(str(v) for v in brk.detail.values())
    corpus.extend(str(k) for k in brk.detail.keys())
    corpus.extend(c.cite() for c in brk.citations)
    corpus.extend(c.excerpt for c in brk.citations)
    # How many sources a finding has is a fact about the finding, computed here
    # from the citation list, and prose is entitled to say it — `_cites` does,
    # every time a break carries more sources than it prints. Without this the
    # guard rejects its own templates, which is not a hypothetical: on the demo
    # book no finding has more than three distinct citations, and on a real one
    # a rollforward against a quarter of trading has dozens. The rule the guard
    # enforces is unchanged — every figure in the prose must be one the desk
    # computed — and the count of sources is one of them.
    corpus.append(str(len(brk.citations)))
    corpus.append(str(len(set(c.cite() for c in brk.citations))))

    allowed = set()
    for chunk in corpus:
        allowed.update(_numbers_in(chunk))
    return allowed


def check_prose(brk, *texts):
    # type: (Break, str) -> None
    """Raise if any prose carries a figure the desk did not compute."""
    allowed = permitted_numbers(brk)
    for text in texts:
        for token in _numbers_in(text):
            if token not in allowed:
                raise InventedFigureError(
                    "prose for %s contains %r, which appears nowhere in the "
                    "computed detail" % (brk.key(), token)
                )


# --- deterministic templates -------------------------------------------------
#
# These are the product. The model rewrites them more fluently; it does not
# supply anything they lack.


def _d(brk, key, default="n/a"):
    # type: (Break, str, str) -> str
    return brk.detail.get(key, default)


def _cites(brk, limit=3):
    # type: (Break, int) -> str
    """
    The first few sources, and how many there are in total.

    The total rather than the remainder, deliberately. `(+27 more)` is a figure
    the prose layer worked out by subtraction, and the invented-figure guard is
    right to reject a number that exists nowhere but in the sentence that prints
    it. `30 sources in all` is a property of the finding — it is `len(citations)`
    — and it is the number a reviewer wants anyway: how much evidence is behind
    this, not how much of it was elided.
    """
    seen = []
    for c in brk.citations:
        cite = c.cite()
        if cite not in seen:
            seen.append(cite)
    shown = seen[:limit]
    tail = "" if len(seen) <= limit else " (%d sources in all)" % len(seen)
    return ", ".join(shown) + tail


def _template(brk):
    # type: (Break) -> Tuple[str, str]
    k = brk.kind

    if k == "QTY_ROLLFORWARD":
        return (
            "%s at %s opened the period at %s units and closed at %s, but the "
            "transaction file explains a movement of only %s. That leaves %s "
            "units — %s — with no booking behind them. Either a trade was never "
            "delivered into the activity feed, or the position file was adjusted "
            "outside it. Sources: %s."
            % (brk.security, brk.custodian, _d(brk, "opening_quantity"),
               _d(brk, "reported_quantity"), _d(brk, "transaction_delta"),
               _d(brk, "unexplained_quantity"), _d(brk, "value_at_risk"), _cites(brk)),
            "Ask %s for the activity detail covering this position for the full "
            "period, including cancel-and-corrects and any non-trade adjustments. "
            "If no booking exists, the position file is wrong and the holding "
            "must not be reported to the client until it is restated."
            % brk.custodian,
        )

    if k == "CORP_ACTION_UNAPPLIED":
        return (
            "A %s in %s went ex on %s. %s has not applied it: the position still "
            "reads %s units where %s are due, leaving %s units unaccounted for at "
            "%s. No corresponding booking appears in the activity file. The "
            "action is confirmed independently by the reference feed, so this is "
            "not an inference from the custodians disagreeing — it is one "
            "custodian being demonstrably behind. Sources: %s."
            % (_d(brk, "action"), brk.security, _d(brk, "ex_date"), brk.custodian,
               _d(brk, "reported_quantity"), _d(brk, "expected_quantity"),
               _d(brk, "unapplied_quantity"), _d(brk, "value_at_risk"), _cites(brk)),
            "Escalate to %s corporate actions and request the adjustment be "
            "booked with the correct ex-date. Suppress client reporting for this "
            "holding until it is applied — valuation, performance and any "
            "percentage-of-portfolio figure derived from it are all wrong in the "
            "meantime." % brk.custodian,
        )

    if k == "CORP_ACTION_WRONG_RATIO":
        return (
            "%s applied the %s in %s, but not at the declared ratio: the position "
            "reads %s units against the %s the action requires. Sources: %s."
            % (brk.custodian, _d(brk, "action"), brk.security,
               _d(brk, "reported_quantity"), _d(brk, "expected_quantity"), _cites(brk)),
            "Send the reference-feed record to %s and request a restatement. A "
            "wrong ratio is worse than an unapplied action — it looks settled."
            % brk.custodian,
        )

    if k == "CORP_ACTION_BASIS_CORRUPTED":
        return (
            "%s applied the %s in %s to the share count correctly, but the total "
            "cost basis moved from %s to %s. A split divides basis per share and "
            "leaves the total untouched, so this is an error in the adjustment, "
            "not a consequence of it. Sources: %s."
            % (brk.custodian, _d(brk, "action"), brk.security,
               _d(brk, "opening_cost_basis"), _d(brk, "reported_cost_basis"), _cites(brk)),
            "Raise with %s and have the basis restated to the opening figure. "
            "Flag to tax reporting: a corrupted basis flows straight into "
            "realised gain on the next disposal." % brk.custodian,
        )

    if k == "MERGER_UNPROCESSED":
        return (
            "%s merged on %s — %s. %s holds %s of the target and is due %s "
            "shares of %s, but the position reads %s where %s are expected. "
            "%s. %s shares worth %s have simply gone missing from the client's "
            "holdings, and nothing else in the reconciliation can see it: the "
            "position file and the activity file agree with each other, because "
            "both are missing the same leg. Sources: %s."
            % (_d(brk, "target"), _d(brk, "ex_date"), _d(brk, "exchange_ratio"),
               brk.custodian, _d(brk, "target_quantity_held"), _d(brk, "shares_due"),
               _d(brk, "acquirer"), _d(brk, "reported_acquirer_quantity"),
               _d(brk, "expected_acquirer_quantity"),
               _d(brk, "leg_failed").capitalize(), _d(brk, "shares_missing"),
               _d(brk, "value_at_risk"), _cites(brk)),
            "Raise a corporate-actions query with %s citing the reference-feed "
            "record and the exchange ratio, and request the entitlement be "
            "booked with the correct effective date. Until it is, the account is "
            "understated and every performance and allocation figure derived "
            "from it is wrong — do not release client reporting for this "
            "portfolio." % brk.custodian,
        )

    if k == "IDENTIFIER_STALE":
        return (
            "%s is still reporting %s under the ticker %s. It became %s on %s. "
            "The position itself is correct — %s — because the line was matched "
            "on %s rather than on the ticker, and the identifier did not change. "
            "Nothing is misstated. What is stale is the security master, and a "
            "ticker is how two systems agree they are discussing the same "
            "instrument. Tickers are reissued to unrelated companies, and on the "
            "day this one is, the match stops being harmless. Sources: %s."
            % (brk.custodian, brk.security, _d(brk, "reported_symbol"),
               _d(brk, "current_symbol"), _d(brk, "effective_date"),
               _d(brk, "position_reconciles"), _d(brk, "identifier_used_to_match"),
               _cites(brk)),
            "Ask %s when their security master was last refreshed against the "
            "reference feed. Treat the answer as the finding: a ticker four "
            "weeks stale usually means unapplied corporate actions are sitting "
            "behind it, and those do move money. Confirm any downstream system "
            "that matches on ticker rather than ISIN or CUSIP." % brk.custodian,
        )

    if k == "POSITION_DISAPPEARED":
        return (
            "%s held %s of %s at the start of the period and reports no position "
            "at all now. The activity file accounts for %s, leaving %s — worth "
            "%s — with no disposal, transfer or corporate action behind it. "
            "Sources: %s."
            % (brk.custodian, _d(brk, "opening_quantity"), brk.security,
               _d(brk, "transaction_delta"), _d(brk, "unexplained_quantity"),
               _d(brk, "value_at_risk"), _cites(brk)),
            "Ask %s for the full activity history on this security including "
            "transfers out, corporate actions and any position-transfer events, "
            "then confirm against the depository record. A holding that leaves "
            "without a booking is either a delivery that was never reported or a "
            "position that was never there." % brk.custodian,
        )

    if k == "FX_INCONSISTENT":
        return (
            "The %s statement quotes %s at %s, but this line converts %s to %s — "
            "an implied rate of %s, off by %s%%. The document disagrees with "
            "itself, and the base-currency figure is overstated or understated by "
            "%s. Sources: %s."
            % (brk.custodian, _d(brk, "pair"), _d(brk, "quoted_rate"),
               _d(brk, "local_value"), _d(brk, "reported_base_value"),
               _d(brk, "implied_rate"), _d(brk, "rate_drift_pct"),
               _d(brk, "value_at_risk"), _cites(brk)),
            "Recompute the base-currency column at the quoted rate before this "
            "reaches consolidated reporting. Ask %s which rate is authoritative "
            "and on what timestamp — if the line is right and the header is "
            "wrong, every other converted position on the statement is affected."
            % brk.custodian,
        )

    if k == "COST_BASIS_DRIFT":
        return (
            "%s reports cost basis of %s for %s. Rolling the opening basis of %s "
            "forward through the period's activity on %s gives %s — a difference "
            "of %s (%s%%). Sources: %s."
            % (brk.custodian, _d(brk, "reported_cost_basis"), brk.security,
               _d(brk, "opening_cost_basis"), _d(brk, "method"),
               _d(brk, "expected_cost_basis"), _d(brk, "basis_drift"),
               _d(brk, "drift_pct"), _cites(brk)),
            "Request tax-lot detail from %s and re-run the comparison lot by lot; "
            "the recomputation above uses weighted-average because no lots were "
            "supplied, and a genuine lot-level difference would explain part of "
            "the gap. Anything left after that is a basis error and affects "
            "realised gain on the next sale." % brk.custodian,
        )

    if k == "CROSS_CUSTODIAN_QTY":
        base = (
            "%s and %s disagree on %s: %s units against %s, a difference of %s "
            "worth %s. Both statements carry the same as-of date, so one of them "
            "is wrong. Sources: %s."
            % (_d(brk, "custodian_a"), _d(brk, "custodian_b"), brk.security,
               _d(brk, "quantity_a"), _d(brk, "quantity_b"), _d(brk, "difference"),
               _d(brk, "value_at_risk"), _cites(brk))
        )
        if "ratio_between_custodians" in brk.detail:
            base += (
                " The two counts stand in a ratio of exactly %s, which is what an "
                "unapplied corporate action looks like — see the corporate action "
                "finding on the same security."
                % _d(brk, "ratio_between_custodians")
            )
        return (
            base,
            "Reconcile against the transfer agent or depository record before "
            "either figure is used. Do not average them and do not adopt the "
            "larger — a consolidated report built on the wrong side of this is "
            "wrong in the client's favour or the firm's, and both are reportable.",
        )

    if k == "PRICE_DIVERGENCE":
        return (
            "%s and %s price %s differently on the same date: %s against %s, a "
            "gap of %s%% worth %s. Sources: %s."
            % (_d(brk, "custodian_a"), _d(brk, "custodian_b"), brk.security,
               _d(brk, "price_a"), _d(brk, "price_b"), _d(brk, "divergence_pct"),
               _d(brk, "value_at_risk"), _cites(brk)),
            "Identify which custodian's pricing source is stale and on what "
            "timestamp each was struck. Divergence on a liquid name is usually a "
            "stale feed; on an illiquid one it is usually a genuine difference of "
            "valuation policy, and that needs a documented decision, not a fix.",
        )

    if k == "STATEMENT_BASIS_MISMATCH":
        return (
            "The custodians on this mandate do not report on the same statement "
            "basis: %s. %s movements worth %s were traded before the period end "
            "and settle after it, so they sit inside one set of statements and "
            "outside the other. Every statement is correct; the comparison "
            "between them is what is not. Sources: %s."
            % (_d(brk, "bases_reported"), _d(brk, "movements_in_flight"),
               _d(brk, "value_in_flight"), _cites(brk)),
            "Nothing needs correcting at a custodian. Fix the comparison: agree "
            "one basis for the mandate and request statements on it, or keep the "
            "netting and make it explicit in the pack. The exposure here is that "
            "the netting depends on settlement dates being reported — the day a "
            "feed stops carrying them, this reappears as a six-figure quantity "
            "break with nothing to explain it.",
        )

    if k == "MISSING_FEE_ACCRUAL":
        return (
            "%s charged %s in management fees in the prior period and nothing in "
            "this one. Nothing has failed and no balance is out — the fee is "
            "simply absent, which is why an absence has to be looked for on "
            "purpose rather than found by comparing figures. Sources: %s."
            % (brk.custodian, _d(brk, "prior_period_amount"), _cites(brk)),
            "Confirm with billing whether the fee was waived, deferred, or "
            "missed. If missed, raise the accrual in the current period rather "
            "than the next — a fee booked a quarter late distorts both periods' "
            "performance and the client will see it.",
        )

    return (
        "%s on %s at %s. Sources: %s."
        % (brk.kind.replace("_", " ").lower(), brk.security, brk.custodian, _cites(brk)),
        "Review the cited source lines and confirm with the custodian.",
    )


def _tidy(text):
    # type: (str) -> str
    """
    Security names carry their legal suffix — "Apple Inc.", "ASML Holding N.V."
    — so a template that ends a sentence after one produces "Apple Inc..".
    Collapse the doubled stop without disturbing the abbreviation itself.
    """
    return re.sub(r"\.\.(?!\.)", ".", text)


def deterministic_writer(brk):
    # type: (Break) -> Tuple[str, str]
    """The default. No network, no key, no dependency — and complete on its own."""
    narrative, fix = _template(brk)
    return _tidy(narrative), _tidy(fix)


# --- optional Claude writer --------------------------------------------------

SYSTEM_PROMPT = """You are writing triage notes for a reconciliation desk at a \
European wealth manager. Your reader is an operations analyst clearing a queue: \
they need to know what is wrong, why it matters, and what to do next, in that \
order.

You are given a break that has ALREADY been detected and quantified. Every \
figure is final and computed from source documents.

Absolute rules:
- Use ONLY figures that appear in the supplied detail. Copy them exactly as \
given, including currency codes, separators and signs.
- Never compute, convert, round, restate or estimate a figure. Do not turn \
310,570.00 into "roughly 310k". Do not sum two figures. Do not derive a \
percentage.
- Write any number that is NOT in the supplied detail as a word: "two \
custodians", not "2 custodians".
- Do not hedge with "appears to" or "may be" when the detail is definite, and do \
not assert a cause the detail does not support.
- No preamble, no headings, no bullet points, no markdown. Plain prose.

narrative: two to four sentences. What is wrong and what it is worth.
proposed_fix: two to three sentences. The concrete next action, addressed to \
someone who will carry it out today."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "narrative": {"type": "string"},
        "proposed_fix": {"type": "string"},
    },
    "required": ["narrative", "proposed_fix"],
    "additionalProperties": False,
}


def claude_writer(client=None, model=MODEL, effort="low"):
    # type: (object, str, str) -> Callable[[Break], Tuple[str, str]]
    """
    Build a writer backed by Claude.

    `client` is injected so the tests can pass a stub — including a deliberately
    hostile one that returns fabricated figures — without a network call or a key.
    """
    if client is None:
        import anthropic  # imported lazily: the desk runs without this installed

        client = anthropic.Anthropic()

    def write(brk):
        # type: (Break) -> Tuple[str, str]
        payload = to_json(
            {
                "kind": brk.kind,
                "severity": brk.severity,
                "security": brk.security,
                "isin": brk.isin,
                "custodian": brk.custodian,
                "as_of": brk.as_of.isoformat(),
                "detail": brk.detail,
                "sources": [c.cite() for c in brk.citations],
            }
        )
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": _SCHEMA},
            },
            messages=[{"role": "user", "content": payload}],
        )
        import json

        text = "".join(
            blk.text for blk in response.content if getattr(blk, "type", "") == "text"
        )
        parsed = json.loads(text)
        return parsed["narrative"], parsed["proposed_fix"]

    return write


# --- the pass ----------------------------------------------------------------


def explain_all(breaks, writer=None, on_reject=None):
    # type: (Sequence[Break], Optional[Callable], Optional[Callable]) -> List[Break]
    """
    Attach prose to every break, and prove nothing else moved.

    A writer that fails — network error, malformed output, invented figure — does
    not fail the run. It falls back to the deterministic template, which was
    always sufficient. `on_reject(brk, exc)` is called so the fallback is
    recorded rather than silently absorbed; the pipeline logs it and the report
    marks the break as template-written.

    A writer that mutates a non-prose field is a different matter entirely and
    raises. There is no safe way to continue from that.
    """
    writer = writer or deterministic_writer
    out = []

    for brk in breaks:
        before = fingerprint(brk)
        narrative, fix = None, None

        if writer is not deterministic_writer:
            try:
                narrative, fix = writer(brk)
                check_prose(brk, narrative, fix)
            except Exception as exc:  # noqa: BLE001 - deliberate: any failure falls back
                if on_reject is not None:
                    on_reject(brk, exc)
                narrative, fix = None, None

        if narrative is None:
            narrative, fix = deterministic_writer(brk)
            check_prose(brk, narrative, fix)  # the templates are held to it too

        explained = replace(brk, narrative=narrative.strip(), proposed_fix=fix.strip())

        after = fingerprint(explained)
        if after != before:
            raise NarrativeTamperError(
                "explanation pass altered a computed field on %s" % brk.key()
            )
        out.append(explained)

    return out


def writer_from_env(enabled=None):
    # type: (Optional[bool]) -> Callable[[Break], Tuple[str, str]]
    """
    Claude when a key is present and BREAK_DESK_LLM is not switched off;
    templates otherwise. Defaulting to the deterministic path means a missing key
    degrades the prose and nothing else.
    """
    if enabled is None:
        enabled = os.environ.get("BREAK_DESK_LLM", "1") not in ("0", "false", "no")
    if not enabled or not os.environ.get("ANTHROPIC_API_KEY"):
        return deterministic_writer
    try:
        return claude_writer()
    except Exception:  # noqa: BLE001 - SDK absent or misconfigured
        return deterministic_writer
