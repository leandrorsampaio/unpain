#!/usr/bin/env python3
"""Coordinate-based extractor for N26 account statements (Kontoauszug, German).

The statement carries its own Zusammenfassung page, which is the reconciliation
gate: `alter Kontostand + Einkommende - Ausgehende == neuer Kontostand`, and the
extracted transactions must sum to `neuer - alter`. Nothing is written unless both
hold, so a missed or misread row fails the statement instead of silently landing
in the books.

Layout (verified against twelve real 2025 statements):

    Beschreibung                        Verbuchungsdatum      Betrag
    Max Mustermann                     08.01.2025     -50,00€
    Belastungen                                              <- category
    IBAN: DE11... • BIC: BANKDEFFXXX                         <- counterparty IBAN
    Sent from N26                                            <- reference text
    Wertstellung 08.01.2025                                  <- closes the block

A transaction is a variable-height block: the amount line, then any of category,
IBAN and free text, closed by `Wertstellung`. A month with no activity is normal
and extracts cleanly as zero transactions.

Usage: python extract.py STATEMENT.pdf [-o out.csv] [--allow-review]
"""
import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import namedtuple
from pathlib import Path

Word = namedtuple("Word", "page left top width text")

MONEY_RE = re.compile(r"^[+-]?(?:\d{1,3}(?:\.\d{3})*|\d+),\d{2}€$")
DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")

# Columns are derived per page from the row header rather than hardcoded: the
# personal and joint-account (Gemeinschaftskonto) statements use different
# margins. Personal puts Verbuchungsdatum at 342 with descriptions reaching 297;
# joint puts it at 359 with descriptions reaching 348 — a fixed split that suits
# one truncates the other.
DATE_COLUMN_WIDTH = 60.0   # a dd.mm.yyyy is ~50pt wide, left-aligned on the header
COLUMN_MARGIN = 6.0
SUMMARY_AMOUNT_MIN = 400.0  # summary labels end by ~130, its values start at 482+
FOOTER_TOP = 740.0       # address block and page number live below this
ROW_BAND = 6.0           # on personal statements the name sits ~2pt above its amount
# The summary page is headed "Zusammenfassung" on personal statements and
# "Übersicht" on joint ones; the labels below are identical on both. Joint
# statements add a "Davon Gebühren" sub-line, which matches no label and is ignored.
SUMMARY_TITLES = ("Zusammenfassung", "Übersicht")
SUMMARY_LABELS = {
    "opening": "Dein alter Kontostand",
    "outgoing": "Ausgehende Transaktionen",
    "incoming": "Einkommende Transaktionen",
    "closing": "Dein neuer Kontostand",
}


def money_cents(text):
    """German money with a euro suffix to integer cents. '-4.475,00€' -> -447500."""
    cleaned = str(text).strip().rstrip("€").replace(".", "").replace(",", ".")
    return int(round(float(cleaned) * 100))


def read_tsv(pdf):
    executable = shutil.which("pdftotext")
    if not executable:
        raise RuntimeError("pdftotext is required (install Poppler with: brew install poppler)")
    with tempfile.NamedTemporaryFile(suffix=".tsv") as tmp:
        proc = subprocess.run(
            [executable, "-tsv", str(pdf), tmp.name], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if proc.returncode:
            raise RuntimeError("pdftotext failed: %s" % proc.stderr.strip())
        rows = list(csv.DictReader(open(tmp.name, encoding="utf-8"), delimiter="\t"))
    return [
        Word(int(r["page_num"]), float(r["left"]), float(r["top"]),
             float(r["width"]), r["text"])
        for r in rows if r["level"] == "5" and r["text"] and not r["text"].startswith("###")
    ]


def joined(words):
    """Read words in visual order: line by line, then left to right."""
    return " ".join(w.text for w in sorted(words, key=lambda w: (round(w.top), w.left))).strip()


def lines_of(words):
    """Group words into visual lines, returned as (top, [words]) sorted by top."""
    buckets = {}
    for word in words:
        buckets.setdefault(round(word.top), []).append(word)
    return [(top, buckets[top]) for top in sorted(buckets)]


def statement_summary(words):
    """The Zusammenfassung page: the four balances, the period and the account IBAN."""
    summary = {}
    for page in sorted({w.page for w in words}):
        page_words = [w for w in words if w.page == page]
        text_by_line = {top: joined(line) for top, line in lines_of(page_words)}
        if not any(value.startswith(SUMMARY_TITLES) for value in text_by_line.values()):
            continue
        for key, label in SUMMARY_LABELS.items():
            tops = [top for top, value in text_by_line.items() if value.startswith(label)]
            if not tops:
                raise ValueError("statement summary is missing '%s'" % label)
            anchor_top = tops[0]
            values = [w for w in page_words
                      if w.left >= SUMMARY_AMOUNT_MIN and MONEY_RE.match(w.text)
                      and abs(w.top - anchor_top) <= ROW_BAND]
            if len(values) != 1:
                raise ValueError("could not read the value for '%s'" % label)
            summary[key + "_cents"] = money_cents(values[0].text)
        break
    if not summary:
        raise ValueError("no Zusammenfassung page found. Is this an N26 Kontoauszug?")

    first = [w for w in words if w.page == 1]
    period = [w.text for w in sorted(first, key=lambda w: (w.top, w.left)) if DATE_RE.match(w.text)]
    summary["period_text"] = "%s - %s" % (period[0], period[1]) if len(period) >= 2 else ""
    ibans = [w.text for w in words if re.match(r"^DE\d{20}$", w.text) and w.top >= FOOTER_TOP]
    summary["account_iban"] = ibans[0] if ibans else None
    return summary


def parse_transactions(words, allow_review=False):
    """Return (transactions, issues, fatal_issues).

    Each amount in the Betrag column opens a block; the block runs to the next
    amount or to the footer, and its description-column lines carry the category,
    the counterparty IBAN and the reference text.
    """
    transactions, issues, fatal_issues = [], [], []
    for page in sorted({w.page for w in words}):
        page_words = [w for w in words if w.page == page and w.top < FOOTER_TOP]
        header = [w for w in page_words if w.text == "Verbuchungsdatum"]
        if not header:
            continue   # summary and notes pages carry no transactions
        # Calibrate the columns off this page's own header.
        date_left = min(w.left for w in header)
        desc_max = date_left - COLUMN_MARGIN
        date_min, date_max = desc_max, date_left + DATE_COLUMN_WIDTH
        amount_min = date_max
        body_top = min(w.top for w in header) + ROW_BAND
        anchors = sorted([w for w in page_words
                          if w.left >= amount_min and w.top > body_top and MONEY_RE.match(w.text)],
                         key=lambda w: w.top)
        for index, anchor in enumerate(anchors):
            block_end = anchors[index + 1].top - ROW_BAND if index + 1 < len(anchors) else FOOTER_TOP
            row_issues = []
            same_line = [w for w in page_words if abs(w.top - anchor.top) <= ROW_BAND]
            dates = [w for w in same_line if date_min <= w.left < date_max and DATE_RE.match(w.text)]
            if len(dates) != 1:
                row_issues.append("expected one booking date, found %d" % len(dates))
                booking_date = None
            else:
                day, month, year = DATE_RE.match(dates[0].text).groups()
                booking_date = "%s-%s-%s" % (year, month, day)
            counterparty = joined([w for w in same_line if w.left < desc_max])

            # Everything below the amount line, up to this block's own terminator.
            # `Wertstellung` closes every block, which is what bounds the last block
            # on a page: the footer sits at 762 on personal statements but 702 on
            # joint ones, so a fixed cut-off lets the address block and the account's
            # own IBAN leak into the final transaction's text.
            body = [w for w in page_words
                    if w.left < desc_max and anchor.top + ROW_BAND < w.top <= block_end]
            category, reference, counterparty_iban = "", [], ""
            for _, line_words in lines_of(body):
                text = joined(line_words)
                if text.startswith("Wertstellung"):
                    break             # value date; the booking date is authoritative
                if text.startswith("IBAN:"):
                    found = re.search(r"\b([A-Z]{2}\d{18,32})\b", text.replace(" ", ""))
                    counterparty_iban = found.group(1) if found else ""
                    continue
                if text in ("Belastungen", "Gutschriften") and not category:
                    category = text
                    continue
                reference.append(text)

            if not counterparty:
                row_issues.append("missing counterparty")
            if row_issues:
                issue = "page %d, amount %s: %s" % (page, anchor.text, "; ".join(row_issues))
                issues.append(issue)
                if not allow_review or booking_date is None:
                    fatal_issues.append(issue)
                    continue
            transactions.append({
                "date": booking_date,
                "amount_cents": money_cents(anchor.text),
                "currency": "EUR",
                "counterparty": counterparty or "N26 transaction",
                "purpose": "N26%s%s%s" % (
                    " / " + category if category else "",
                    " · " + " ".join(reference) if reference else "",
                    (" [EXTRACTION REVIEW: %s]" % "; ".join(row_issues)) if row_issues else "",
                ),
                "counterparty_iban": counterparty_iban,
                "force_review": bool(row_issues),
                "order": (page, anchor.top),
            })
    return transactions, issues, fatal_issues


def write_csv(path, transactions):
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=(
            "date", "amount", "currency", "counterparty", "purpose",
            "counterparty_iban", "force_review",
        ))
        writer.writeheader()
        for txn in sorted(transactions, key=lambda t: (t["date"], t["order"])):
            writer.writerow({
                "date": txn["date"],
                "amount": "%.2f" % (txn["amount_cents"] / 100.0),
                "currency": txn["currency"],
                "counterparty": txn["counterparty"],
                "purpose": txn["purpose"],
                "counterparty_iban": txn["counterparty_iban"],
                "force_review": "true" if txn["force_review"] else "false",
            })
    tmp_path.replace(path)


def extract_pdf(pdf, output, allow_review=False):
    """Extract one statement and return its report; never write an unsafe CSV."""
    pdf, output = Path(pdf), Path(output)
    report = {"input": str(pdf), "output": str(output),
              "mode": "allow-review" if allow_review else "strict"}
    if not pdf.is_file():
        raise ValueError("PDF does not exist: %s" % pdf)
    words = read_tsv(pdf)
    summary = statement_summary(words)
    txns, issues, fatal_issues = parse_transactions(words, allow_review)
    sum_cents = sum(t["amount_cents"] for t in txns)
    expected_sum = summary["closing_cents"] - summary["opening_cents"]
    # The summary must be internally consistent, and the rows must explain the move
    # from the old balance to the new one. A month with no activity satisfies both.
    summary_ok = (summary["opening_cents"] + summary["incoming_cents"] -
                  abs(summary["outgoing_cents"]) == summary["closing_cents"])
    transaction_ok = sum_cents == expected_sum
    safe_issues = not fatal_issues and (not issues or allow_review)
    ok = summary_ok and transaction_ok and safe_issues
    report.update({
        "status": "ok" if ok else "failed",
        "period": summary["period_text"],
        "account_iban": summary["account_iban"],
        "transactions_extracted": len(txns),
        "transactions_for_review": sum(1 for t in txns if t["force_review"]),
        "opening_balance": summary["opening_cents"] / 100.0,
        "incoming_total": summary["incoming_cents"] / 100.0,
        "outgoing_total": summary["outgoing_cents"] / 100.0,
        "closing_balance": summary["closing_cents"] / 100.0,
        "sum_of_transactions": sum_cents / 100.0,
        "discrepancy": (sum_cents - expected_sum) / 100.0,
        "summary_consistent": summary_ok,
        "issues": issues,
        "fatal_issues": fatal_issues,
    })
    if ok:
        output.parent.mkdir(parents=True, exist_ok=True)
        write_csv(output, txns)
    else:
        report["output"] = None
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("-o", "--output", type=Path, help="output CSV (default: beside PDF)")
    parser.add_argument("--allow-review", action="store_true",
                        help="emit questionable rows with force_review=true")
    args = parser.parse_args(argv)
    output = args.output or args.pdf.with_suffix(".csv")
    log_path = output.with_suffix(".extract-log.json")
    report = {"input": str(args.pdf), "output": str(output),
              "mode": "allow-review" if args.allow_review else "strict"}
    exit_code = 1
    try:
        report = extract_pdf(args.pdf, output, args.allow_review)
        if report["status"] == "ok":
            exit_code = 0
    except Exception as exc:  # noqa: BLE001
        report.update({"status": "failed", "error": str(exc)})
    finally:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("Log: %s" % log_path)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
