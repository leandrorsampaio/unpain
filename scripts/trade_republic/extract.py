#!/usr/bin/env python3
"""Deterministic Trade Republic account-statement PDF extractor.

The parser uses word coordinates emitted by Poppler, not visual line wrapping.
Amounts are represented as integer cents and every transaction is checked
against Trade Republic's running balance before any CSV is written.
"""
import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path


MONEY_RE = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}$|^-?\d+,\d{2}$")
MONTHS = {
    "jan": 1, "feb": 2, "märz": 3, "maerz": 3, "apr": 4, "mai": 5,
    "juni": 6, "juli": 7, "aug": 8, "sep": 9, "sept": 9, "okt": 10,
    "mrz": 3,
    "nov": 11, "dez": 12,
}


@dataclass(frozen=True)
class Word:
    page: int
    left: float
    top: float
    width: float
    text: str


def money_cents(raw):
    value = raw.replace(".", "").replace(",", ".")
    return int(round(float(value) * 100))


def parse_date(words):
    tokens = [w.text.strip().rstrip(".") for w in sorted(words, key=lambda w: (w.top, w.left))]
    day = year = month = None
    for token in tokens:
        folded = token.lower()
        if day is None and token.isdigit() and 1 <= int(token) <= 31:
            day = int(token)
        if folded in MONTHS:
            month = MONTHS[folded]
        if token.isdigit() and len(token) == 4:
            year = int(token)
    if not all((day, month, year)):
        raise ValueError("incomplete date: %s" % " ".join(tokens))
    return date(year, month, day).isoformat()


def joined(words):
    """Join positioned words, preserving line order without PDF wrapping noise."""
    lines = []
    for word in sorted(words, key=lambda w: (w.top, w.left)):
        line = next((line for line in lines if abs(line[0] - word.top) <= 2.0), None)
        if line is None:
            line = [word.top, []]
            lines.append(line)
        line[1].append(word)
    return " ".join(
        " ".join(w.text for w in sorted(line_words, key=lambda w: w.left))
        for _, line_words in sorted(lines, key=lambda line: line[0])
    ).strip()


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


def statement_summary(words):
    first = [w for w in words if w.page == 1 and w.top < 300]
    labels = {w.text: w for w in first if w.text in ("ANFANGSSALDO", "ZAHLUNGSEINGANG", "ZAHLUNGSAUSGANG", "ENDSALDO")}
    missing = set(("ANFANGSSALDO", "ZAHLUNGSEINGANG", "ZAHLUNGSAUSGANG", "ENDSALDO")) - set(labels)
    if missing:
        raise ValueError("statement summary missing: %s" % ", ".join(sorted(missing)))

    def below(label):
        anchor = labels[label]
        candidates = [w for w in first if MONEY_RE.match(w.text) and
                      abs(w.left - anchor.left) < 45 and 0 < w.top - anchor.top < 30]
        if len(candidates) != 1:
            raise ValueError("could not read summary value for %s" % label)
        return money_cents(candidates[0].text)

    period_words = [w for w in first if 420 <= w.left and 130 <= w.top <= 145]
    ibans = [w.text.replace(" ", "") for w in first if re.match(r"^DE\d{20}$", w.text.replace(" ", ""))]
    return {
        "opening_cents": below("ANFANGSSALDO"),
        "incoming_cents": below("ZAHLUNGSEINGANG"),
        "outgoing_cents": below("ZAHLUNGSAUSGANG"),
        "closing_cents": below("ENDSALDO"),
        "period_text": joined(period_words),
        "account_iban": ibans[0] if ibans else None,
    }


def amount_agrees(printed_text, delta_cents):
    """Does the printed amount corroborate the movement in the running balance?

    The amount booked is always the balance delta; this is the cross-check that the
    row was read off the right line. Statements differ in whether the amount column
    carries a sign: annual statements print bare magnitudes and let the running
    balance express direction, so a debit shows as '7,50' against a delta of -7.50.
    Compare signed when the statement gives us a sign to compare — losing that check
    where it exists would be a real loss — and by magnitude when it does not.
    """
    printed_cents = money_cents(printed_text)
    if printed_text.strip().startswith("-"):
        return printed_cents == delta_cents
    return abs(printed_cents) == abs(delta_cents)


def parse_transactions(words, opening_cents, allow_review=False):
    transactions, issues, fatal_issues = [], [], []
    previous_balance = opening_cents
    pages = sorted({w.page for w in words})
    for page in pages:
        page_words = [w for w in words if w.page == page]
        cash_sections = [w.top for w in page_words if w.text == "BARMITTELÜBERSICHT"]
        first_cash_section = min(cash_sections) if cash_sections else 760
        saldo_headers = [w for w in page_words if w.text == "SALDO" and w.top < first_cash_section]
        if not saldo_headers:
            continue
        body_top = max(w.top for w in saldo_headers) + 5
        section_ends = [w.top for w in page_words if w.text in ("BARMITTELÜBERSICHT", "TRANSAKTIONSÜBERSICHT")
                        and w.top > body_top]
        footer_starts = [w.top for w in page_words if w.text == "Trade" and 70 <= w.left <= 80 and w.top > body_top]
        body_bottom = min(section_ends + footer_starts) if section_ends or footer_starts else 760
        anchors = sorted(
            [w for w in page_words if body_top < w.top < body_bottom and
             w.left >= 465 and MONEY_RE.match(w.text)],
            key=lambda w: w.top,
        )
        for index, anchor in enumerate(anchors):
            upper = body_top if index == 0 else (anchors[index - 1].top + anchor.top) / 2
            lower = body_bottom if index == len(anchors) - 1 else (anchor.top + anchors[index + 1].top) / 2
            row = [w for w in page_words if upper <= w.top < lower and w.top < body_bottom]
            row_issues = []
            current_balance = money_cents(anchor.text)
            delta = current_balance - previous_balance
            printed = [w for w in row if 315 <= w.left < 465 and MONEY_RE.match(w.text)]
            if len(printed) != 1:
                row_issues.append("expected one printed amount, found %d" % len(printed))
            elif not amount_agrees(printed[0].text, delta):
                row_issues.append("printed amount %s disagrees with balance delta %.2f" %
                                  (printed[0].text, delta / 100.0))
            try:
                booking_date = parse_date([w for w in row if w.left < 100])
            except (ValueError, OverflowError) as exc:
                booking_date = None
                row_issues.append(str(exc))
            txn_type = joined([w for w in row if 98 <= w.left < 160])
            description = joined([w for w in row if 155 <= w.left < 350])
            # Current TR statements sometimes render a missing optional field as
            # the literal Java value "null", attached to the merchant text.
            description = re.sub(r"null\b", "", description).strip()
            if not txn_type:
                row_issues.append("missing transaction type")
            if not description:
                row_issues.append("missing description")

            location = "page %d, balance %s" % (page, anchor.text)
            if delta == 0:
                row_issues.append("zero balance delta")
            if row_issues:
                issue = "%s: %s" % (location, "; ".join(row_issues))
                issues.append(issue)
                if not allow_review or booking_date is None or delta == 0:
                    fatal_issues.append(issue)
                    previous_balance = current_balance
                    continue
            transactions.append({
                "date": booking_date,
                "amount_cents": delta,
                "currency": "EUR",
                "counterparty": description or "Trade Republic transaction",
                "purpose": "Trade Republic / %s%s" % (
                    txn_type or "unknown type",
                    (" [EXTRACTION REVIEW: %s]" % "; ".join(row_issues)) if row_issues else "",
                ),
                "counterparty_iban": "",
                "force_review": bool(row_issues),
                "page": page,
                "balance_cents": current_balance,
            })
            previous_balance = current_balance
    return transactions, issues, fatal_issues, previous_balance


def write_csv(path, transactions):
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=(
            "date", "amount", "currency", "counterparty", "purpose",
            "counterparty_iban", "force_review",
        ))
        writer.writeheader()
        for txn in transactions:
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
    txns, issues, fatal_issues, last_balance = parse_transactions(
        words, summary["opening_cents"], allow_review
    )
    sum_cents = sum(t["amount_cents"] for t in txns)
    expected_sum = summary["closing_cents"] - summary["opening_cents"]
    summary_ok = (summary["opening_cents"] + summary["incoming_cents"] -
                  summary["outgoing_cents"] == summary["closing_cents"])
    transaction_ok = sum_cents == expected_sum and last_balance == summary["closing_cents"]
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
    except Exception as exc:
        report.update({"status": "failed", "error": str(exc)})
    finally:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("Log: %s" % log_path)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
