"""The deterministic gate every extracted statement passes before it is imported.

An extractor is a plugin. `scripts/*/extractor.json` points at any callable, and the
`extract-statement` LLM skill produces the same shape by hand. Asking each of them to
have reconciled its own output makes the project's one safety invariant a convention:
a new extractor, a changed one, or a model having a bad day can label anything
`status: "ok"` and the store has no way to disagree. The admission layer used to read
that word and import whatever CSV was sitting next to it.

This module is the disagreeing. It re-does the arithmetic here, from the two things an
extractor cannot fake after the fact — the balances the statement itself prints, and
the exact CSV that is about to be read — and imports nothing unless

    opening + sum(rows) == closing

to the cent. It also holds the report to its own claims: a report that says it found
40 transactions totalling -812.30 must have written 40 rows totalling -812.30, or the
file changed between the reconciliation and the import and neither number means
anything.
"""
import math
from pathlib import Path

from . import formats
from .util import cents


class ExtractionRejected(ValueError):
    """The extraction did not prove itself. Nothing may be imported."""


def storable(value):
    """The report with any non-finite number replaced by null.

    A report is kept beside the upload as evidence, including a rejected one — that is
    the record of *why* nothing was imported. But an extractor that produced a NaN
    balance produces a report that no JSON writer will accept, and refusing to store
    the evidence of a failure turns a clean rejection into a 500.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: storable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [storable(item) for item in value]
    return value


def _balance_cents(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExtractionRejected(
            "the report has no %s, so there is nothing to reconcile against (got %r). "
            "A statement without balances cannot be admitted as reconciled." % (label, value))
    if not math.isfinite(float(value)):
        raise ExtractionRejected("the %s is not a finite number (%r)" % (label, value))
    return cents(value)


def read_rows(csv_path):
    """Read the CSV exactly as ingestion will read it, in integer cents."""
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        raise ExtractionRejected("the extractor reported success but wrote no file")
    cfg = formats.detect(csv_path)
    rows = formats.parse(csv_path, cfg)
    if not rows:
        raise ExtractionRejected("the extracted file holds no transactions")
    currencies = {(row.get("currency") or "EUR").upper() for row in rows}
    if len(currencies) > 1:
        # Balances are one currency, so a mixed-currency file cannot be reconciled
        # against them — summing the rows would be adding euros to reais.
        raise ExtractionRejected("the extracted rows mix currencies (%s), which no single "
                                 "opening/closing balance can prove" % ", ".join(sorted(currencies)))
    return rows, sum(cents(row["amount"]) for row in rows)


def _check_anchors(report, opening, closing):
    """Balance anchors, when present, must be the balances that were just proved.

    They are not required: the opening/closing pair above is already the bank-authored
    arithmetic, and N26 statements whose period line does not parse legitimately carry
    none. What is not allowed is an anchor that contradicts the reconciliation, because
    an anchor becomes a permanent checkpoint the doctor re-verifies for years.
    """
    anchor_rows = report.get("balance_anchors") or []
    if not anchor_rows:
        return
    if not isinstance(anchor_rows, list) or len(anchor_rows) != 2:
        raise ExtractionRejected("balance_anchors must be the opening and the closing balance")
    try:
        values = [cents(anchor["balance"]) for anchor in anchor_rows]
        dates = [str(anchor["date"]) for anchor in anchor_rows]
    except (KeyError, TypeError, ValueError):
        raise ExtractionRejected("each balance anchor needs a date and a finite balance")
    if dates[0] > dates[1]:
        raise ExtractionRejected("the balance anchors are not in date order (%s, %s)" % tuple(dates))
    if values != [opening, closing]:
        raise ExtractionRejected(
            "the balance anchors (%.2f, %.2f) are not the balances the statement reconciled "
            "against (%.2f, %.2f)" % (values[0] / 100.0, values[1] / 100.0,
                                      opening / 100.0, closing / 100.0))


def admit(report, csv_path, trusted_opening_cents=None):
    """Verify an extraction report against the file it wrote. Raises, or returns facts.

    Every caller that turns an extraction into stored transactions goes through here.
    """
    if not isinstance(report, dict):
        raise ExtractionRejected("the extractor returned no report")
    if report.get("status") != "ok":
        raise ExtractionRejected(report.get("error") or "the extractor did not report status 'ok'")
    opening = _balance_cents(report.get("opening_balance"), "opening balance")
    closing = _balance_cents(report.get("closing_balance"), "closing balance")
    if report.get("opening_balance_source") == "derived":
        if trusted_opening_cents is None:
            raise ExtractionRejected(
                "the extractor derived its opening balance from the first row instead of reading "
                "a balance printed by the bank. Record that opening balance manually for this "
                "account, then retry; otherwise a missing first row cannot be detected")
        if int(trusted_opening_cents) != opening:
            raise ExtractionRejected(
                "the derived opening balance %.2f disagrees with the independently recorded "
                "opening balance %.2f" % (opening / 100.0, trusted_opening_cents / 100.0))
    rows, total = read_rows(csv_path)
    if opening + total != closing:
        raise ExtractionRejected(
            "the %d extracted row(s) add up to %.2f, which does not carry the opening balance "
            "%.2f to the closing balance %.2f — off by %.2f"
            % (len(rows), total / 100.0, opening / 100.0, closing / 100.0,
               (opening + total - closing) / 100.0))
    claimed_total = report.get("sum_of_transactions")
    if claimed_total is not None and cents(claimed_total) != total:
        raise ExtractionRejected(
            "the report reconciled a total of %.2f but the file it wrote totals %.2f, so the "
            "two are not describing the same set of transactions"
            % (float(claimed_total), total / 100.0))
    claimed_count = report.get("transactions_extracted")
    if isinstance(claimed_count, int) and not isinstance(claimed_count, bool) \
            and claimed_count != len(rows):
        raise ExtractionRejected(
            "the report reconciled %d transaction(s) but the file it wrote holds %d"
            % (claimed_count, len(rows)))
    _check_anchors(report, opening, closing)
    return {"rows": len(rows), "total_cents": total,
            "opening_cents": opening, "closing_cents": closing}
