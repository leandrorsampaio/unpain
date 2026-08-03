#!/usr/bin/env python3
"""Coordinate-based extractor for Banco Rendimento account statements (extrato).

The statement prints a running balance on every transaction row, so the balance
chain is the reconciliation gate: walked oldest to newest, every printed balance
must equal the previous one plus the printed amount. A row that fails is never
written; strict mode refuses the whole statement.

Layout (stable across pages, verified on a real 2025 statement):

    Documento    Lançamento                          Valor (R$)      Saldo
    29/12/2025                                        <- date section header
    1234567      Rec Pgto Pix Cp :...MARIA MUSTERMANN        100,00    1.100,00
                 Saldo Final                                      1.280,00

Sections run newest-first, but rows inside one date run oldest-first. A long
description wraps onto the lines above and below its own amount, so rows are cut
on the Saldo column rather than on text lines.

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

MONEY_RE = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}$|^-?\d+,\d{2}$")
DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")

# Column boundaries in PDF points. Headers sit at Documento 60, Lançamento 121,
# Valor 442, Saldo 510. Both money columns are right-aligned, so a wide value
# starts further left: across a real statement Valor spans 446-464 and Saldo
# 495-512. The split sits in that gap — putting it at 495 loses every balance
# in the ten-thousands, which is how the oldest rows went missing.
DOC_MAX = 115.0
DESC_MIN, DESC_MAX = 115.0, 435.0
VALOR_MIN, VALOR_MAX = 435.0, 480.0
SALDO_MIN = 480.0
ROW_BAND = 12.0          # how far a wrapped description may sit from its amount


def money_cents(text):
    """Brazilian money to integer cents. '1.234,56' -> 123456, '-40,00' -> -4000."""
    cleaned = str(text).strip().replace(".", "").replace(",", ".")
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
    """Read words in visual order: line by line, then left to right.

    Sorting on `left` alone scrambles a wrapped description, because the
    continuation line starts at the left margin and would sort ahead of the words
    that precede it on the line above.
    """
    return " ".join(w.text for w in sorted(words, key=lambda w: (round(w.top), w.left))).strip()


def statement_period(words):
    """The 'De dd/mm/yyyy a dd/mm/yyyy' line, used to label the statement."""
    first = [w for w in words if w.page == 1]
    dates = [w.text for w in sorted(first, key=lambda w: (w.top, w.left))
             if DATE_RE.match(w.text)]
    # the print timestamp is the first date on the page; the period follows it
    period = [d for d in dates if d not in ("",)]
    if len(period) >= 3:
        return "%s - %s" % (period[1], period[2])
    return ""


def parse_transactions(words, allow_review=False):
    """Return (transactions, issues, fatal_issues, checkpoints).

    Rows are found by anchoring on the Saldo column: a transaction row has a
    balance AND an amount, a 'Saldo Final' row has only a balance.
    """
    transactions, issues, fatal_issues, checkpoints = [], [], [], []
    # One continuous pass over the document. A date section can be split by a page
    # break, leaving rows at the top of the next page whose header is on the page
    # before, so the current date has to survive the page boundary.
    current_date = None
    for page in sorted({w.page for w in words}):
        page_words = [w for w in words if w.page == page]
        rows = sorted(
            [w for w in page_words
             if (w.left >= SALDO_MIN and MONEY_RE.match(w.text))
             or (w.left < DOC_MAX and DATE_RE.match(w.text))],
            key=lambda w: w.top)
        for anchor in rows:
            if DATE_RE.match(anchor.text) and anchor.left < DOC_MAX:
                day, month, year = DATE_RE.match(anchor.text).groups()
                current_date = "%s-%s-%s" % (year, month, day)
                continue
            band = [w for w in page_words if abs(w.top - anchor.top) <= ROW_BAND]
            valor = [w for w in band if VALOR_MIN <= w.left < VALOR_MAX and MONEY_RE.match(w.text)]
            desc_words = [w for w in band if DESC_MIN <= w.left < DESC_MAX]
            description = joined(desc_words)
            if not valor:
                # 'Saldo Final' closes a date section; keep it as a checkpoint. The
                # trailing summary block (Saldo Atual etc.) labels itself outside the
                # description column, so it falls through here and is ignored.
                if description.startswith("Saldo Final"):
                    checkpoints.append({"page": page, "balance_cents": money_cents(anchor.text)})
                continue
            location = "page %d, balance %s" % (page, anchor.text)
            row_issues = []
            if len(valor) != 1:
                row_issues.append("expected one amount, found %d" % len(valor))
            doc_words = [w for w in band if w.left < DOC_MAX and not DATE_RE.match(w.text)]
            booking_date = current_date
            if booking_date is None:
                row_issues.append("no date section before this row")
            if not description:
                row_issues.append("missing description")
            if row_issues:
                issue = "%s: %s" % (location, "; ".join(row_issues))
                issues.append(issue)
                if not allow_review or booking_date is None:
                    fatal_issues.append(issue)
                    continue
            transactions.append({
                "date": booking_date,
                "amount_cents": money_cents(valor[0].text),
                "balance_cents": money_cents(anchor.text),
                "currency": "BRL",
                "document": joined(doc_words),
                "counterparty": description or "Banco Rendimento transaction",
                "purpose": "Banco Rendimento%s%s" % (
                    " / " + joined(doc_words) if doc_words else "",
                    (" [EXTRACTION REVIEW: %s]" % "; ".join(row_issues)) if row_issues else "",
                ),
                "page": page,
                "order": (page, anchor.top),
                "force_review": bool(row_issues),
            })
    return transactions, issues, fatal_issues, checkpoints


def verify_chain(transactions):
    """Walk the printed balances oldest to newest. Returns (opening, closing, breaks).

    Date sections run newest-first down the document, but the rows inside one date
    run oldest-first. Sorting by date ascending fixes the section order, and keeping
    document order within a date preserves the sequence the balances were printed in.
    """
    if not transactions:
        return None, None, []
    ordered = sorted(transactions, key=lambda t: (t["date"], t["order"]))
    opening = ordered[0]["balance_cents"] - ordered[0]["amount_cents"]
    breaks = []
    running = opening
    for txn in ordered:
        running += txn["amount_cents"]
        if running != txn["balance_cents"]:
            breaks.append("%s %s: balance %.2f does not follow from %.2f %+.2f" % (
                txn["date"], txn["counterparty"][:40], txn["balance_cents"] / 100.0,
                (running - txn["amount_cents"]) / 100.0, txn["amount_cents"] / 100.0))
            running = txn["balance_cents"]   # resync so one break does not cascade
    return opening, ordered[-1]["balance_cents"], breaks


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
                "counterparty_iban": "",
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
    txns, issues, fatal_issues, checkpoints = parse_transactions(words, allow_review)
    if not txns:
        raise ValueError("No transactions found. Is this a Banco Rendimento extrato?")
    opening, closing, breaks = verify_chain(txns)
    sum_cents = sum(t["amount_cents"] for t in txns)
    chain_ok = not breaks and opening is not None and opening + sum_cents == closing
    safe_issues = not fatal_issues and (not issues or allow_review)
    ok = chain_ok and safe_issues
    report.update({
        "status": "ok" if ok else "failed",
        "period": statement_period(words),
        "account_iban": None,
        "transactions_extracted": len(txns),
        "transactions_for_review": sum(1 for t in txns if t["force_review"]),
        "opening_balance": None if opening is None else opening / 100.0,
        "closing_balance": None if closing is None else closing / 100.0,
        "sum_of_transactions": sum_cents / 100.0,
        "discrepancy": 0.0 if opening is None else (opening + sum_cents - closing) / 100.0,
        "balance_breaks": breaks,
        "date_checkpoints": len(checkpoints),
        "issues": issues,
        "fatal_issues": fatal_issues + breaks,
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
                        help="emit balance-proven questionable rows with force_review=true")
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
