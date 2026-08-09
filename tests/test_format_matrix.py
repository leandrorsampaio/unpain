"""Every declared bank format, read correctly — and refused when it is not.

Two formats shipped with no fixture at all (`volksbank`, `nubank-conta`), so nothing
proved they even detected. The formats that did have fixtures were only ever fed valid
files, so nothing proved what happens to an invalid one — and "what happens to an
invalid one" is the whole question: a statement that imports *partly* is worse than one
that does not import at all, because the totals then look finished.

Each format below gets one sanitized statement, checked field by field, and then the
same mutation table applied to it. Every mutation must be refused with a message that
names the problem, and nothing may be written when it is. Adding a format means adding
it here; the manifest count is asserted so a new `pipeline/formats/*.json` cannot arrive
without a fixture.

Every row is synthetic. IBANs are the DE00-prefixed documentation range.

Usage: .venv/bin/python tests/test_format_matrix.py
"""
import csv
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from sandbox import PROJECT

tmp = Path(tempfile.mkdtemp(prefix="fa-format-matrix-"))
os.environ["FA_ROOT"] = str(tmp)
(tmp / "data").mkdir(parents=True)

sys.path.insert(0, str(PROJECT))
from pipeline import formats  # noqa: E402
from pipeline.util import cents  # noqa: E402

failures = []
total_checks = 0


def check(name, cond, detail=""):
    global total_checks
    total_checks += 1
    if not cond:
        print("  FAIL %s %s" % (name, detail))
        failures.append(name)
    return cond


def write(name, rows, delimiter=";", encoding="utf-8", newline="\r\n", bom=False):
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL,
                        lineterminator=newline)
    writer.writerows(rows)
    text = ("﻿" if bom else "") + buffer.getvalue()
    path = tmp / name
    path.write_bytes(text.encode(encoding))
    return path


def parse(path):
    return formats.parse(path, formats.detect(path))


def refuses(rows, label, delimiter=";", **kwargs):
    """A mutated statement must raise, and say something specific about why."""
    path = write("mutant.csv", rows, delimiter=delimiter, **kwargs)
    try:
        result = parse(path)
    except ValueError as exc:
        return check(label, len(str(exc)) > 30, "message too vague: %r" % str(exc)[:80])
    except Exception as exc:
        return check(label, False, "raised %s instead of a readable ValueError: %s"
                     % (type(exc).__name__, exc))
    return check(label, False, "accepted the file and returned %d row(s)" % len(result))


# ---------------------------------------------------------------------------
# One sanitized statement per declared format. `rows` is the whole file.
# `expect` is what the pipeline must read out of the two transaction rows.
# ---------------------------------------------------------------------------
IBAN = "DE00000000000000000001"

FIXTURES = {
    "deutsche-bank-giro": {
        "delimiter": ";",
        "rows": [
            ["Buchungstag", "Begünstigter / Auftraggeber", "Verwendungszweck", "Betrag",
             "Währung", "IBAN / Kontonummer"],
            ["04.03.2026", "SUPERMARKT GMBH", "Einkauf", "-1.234,56", "EUR", IBAN],
            ["05.03.2026", "ARBEITGEBER GMBH", "Gehalt", "2.500,00", "EUR", IBAN],
        ],
        "expect": [("2026-03-04", -1234.56, "EUR", "SUPERMARKT GMBH"),
                   ("2026-03-05", 2500.00, "EUR", "ARBEITGEBER GMBH")],
        "amount_column": 3,
    },
    "dkb-giro": {
        "delimiter": ";",
        "rows": [
            ["Buchungsdatum", "Zahlungsempfänger*in", "Verwendungszweck", "Betrag (€)", "IBAN"],
            ["04.03.26", "SUPERMARKT GMBH", "Einkauf", "-1.234,56", IBAN],
            ["05.03.26", "ARBEITGEBER GMBH", "Gehalt", "2.500,00", IBAN],
        ],
        "expect": [("2026-03-04", -1234.56, "EUR", "SUPERMARKT GMBH"),
                   ("2026-03-05", 2500.00, "EUR", "ARBEITGEBER GMBH")],
        "amount_column": 3,
    },
    "volksbank": {
        "delimiter": ";",
        "rows": [
            ["Buchungstag", "Valutadatum", "Name Zahlungsbeteiligter", "IBAN Zahlungsbeteiligter",
             "Verwendungszweck", "Betrag", "Waehrung"],
            ["04.03.2026", "04.03.2026", "SUPERMARKT GMBH", IBAN, "Einkauf", "-1.234,56", "EUR"],
            ["05.03.2026", "05.03.2026", "ARBEITGEBER GMBH", IBAN, "Gehalt", "2.500,00", "EUR"],
        ],
        "expect": [("2026-03-04", -1234.56, "EUR", "SUPERMARKT GMBH"),
                   ("2026-03-05", 2500.00, "EUR", "ARBEITGEBER GMBH")],
        "amount_column": 5,
    },
    "barclays-de": {
        "delimiter": ";",
        "rows": [
            ["Referenznummer", "Buchungsdatum", "Beschreibung", "Betrag"],
            ["R00001", "04.03.2026", "SUPERMARKT GMBH Einkauf", "-1.234,56"],
            ["R00002", "05.03.2026", "ARBEITGEBER GMBH Gehalt", "2.500,00"],
        ],
        "expect": [("2026-03-04", -1234.56, "EUR", "SUPERMARKT GMBH Einkauf"),
                   ("2026-03-05", 2500.00, "EUR", "ARBEITGEBER GMBH Gehalt")],
        "amount_column": 3,
        "date_column": 1,
    },
    "n26": {
        "delimiter": ",",
        "rows": [
            ["Booking Date", "Partner Name", "Payment Reference", "Amount (EUR)", "Partner Iban"],
            ["2026-03-04", "SUPERMARKT GMBH", "Einkauf", "-1234.56", IBAN],
            ["2026-03-05", "ARBEITGEBER GMBH", "Gehalt", "2500.00", IBAN],
        ],
        "expect": [("2026-03-04", -1234.56, "EUR", "SUPERMARKT GMBH"),
                   ("2026-03-05", 2500.00, "EUR", "ARBEITGEBER GMBH")],
        "amount_column": 3,
    },
    "nubank-conta": {
        "delimiter": ",",
        "rows": [
            ["Data", "Valor", "Identificador", "Descrição"],
            ["04/03/2026", "-1234.56", "id-0001", "Compra no supermercado"],
            ["05/03/2026", "2500.00", "id-0002", "Transferencia recebida"],
        ],
        "expect": [("2026-03-04", -1234.56, "BRL", "Compra no supermercado"),
                   ("2026-03-05", 2500.00, "BRL", "Transferencia recebida")],
        "amount_column": 1,
    },
    "nubank-card": {
        "delimiter": ",",
        # Card exports list spending positive; sign -1 normalizes it.
        "rows": [
            ["date", "title", "amount"],
            ["2026-03-04", "Supermercado", "1.234,56"],
            ["2026-03-05", "Pagamento recebido", "- 2.500,00"],
        ],
        "expect": [("2026-03-04", -1234.56, "BRL", "Supermercado"),
                   ("2026-03-05", 2500.00, "BRL", "Pagamento recebido")],
        "amount_column": 2,
    },
    "generic-extracted": {
        "delimiter": ",",
        "rows": [
            ["date", "amount", "currency", "counterparty", "purpose", "counterparty_iban",
             "force_review"],
            ["2026-03-04", "-1234.56", "EUR", "SUPERMARKT GMBH", "Einkauf", "", "false"],
            ["2026-03-05", "2500.00", "EUR", "ARBEITGEBER GMBH", "Gehalt", "", "false"],
        ],
        "expect": [("2026-03-04", -1234.56, "EUR", "SUPERMARKT GMBH"),
                   ("2026-03-05", 2500.00, "EUR", "ARBEITGEBER GMBH")],
        "amount_column": 1,
    },
    "cash": {
        "delimiter": ",",
        "rows": [
            ["date", "account", "amount", "currency", "description", "category"],
            ["2026-03-04", "cash-person1", "-1234.56", "EUR", "Markt", ""],
            ["2026-03-05", "cash-person1", "2500.00", "EUR", "Gefunden", ""],
        ],
        "expect": [("2026-03-04", -1234.56, "EUR", "Markt"),
                   ("2026-03-05", 2500.00, "EUR", "Gefunden")],
        "amount_column": 2,
    },
    "deutsche-bank-kreditkarte": {
        "delimiter": ";",
        # Five preamble lines precede the header; two columns are named 'Betrag' and are
        # addressed by position (4 = foreign, 6 = the bank's own EUR figure).
        "rows": [
            ["Umsatzanzeige"], ["Karte", "1234XXXXXXXX5678"], ["Abrechnungsdatum", "06.03.2026"],
            [""], [""],
            ["Belegdatum", "Eingangstag", "Verwendungszweck", "Fremdwährung", "Betrag", "Kurs",
             "Betrag", "Währung"],
            ["4.3.2026", "5.3.2026", "SUPERMARKT GMBH", "", "", "", "-1.234,56", "EUR"],
            ["5.3.2026", "6.3.2026", "ARBEITGEBER GMBH", "", "", "", "2.500,00", "EUR"],
            ["", "", "Saldo", "", "", "", "1.265,44", "EUR"],
        ],
        "expect": [("2026-03-04", -1234.56, "EUR", "SUPERMARKT GMBH"),
                   ("2026-03-05", 2500.00, "EUR", "ARBEITGEBER GMBH")],
        "amount_column": 6,
        "header_row": 5,
    },
}

# A format file with no fixture here is a format nobody has ever read a statement with.
declared = {json.loads(path.read_text())["name"]
            for path in (PROJECT / "pipeline" / "formats").glob("*.json")}
print("== every declared format has a fixture")
check("no format is left untested", declared == set(FIXTURES),
      "missing: %s | unknown: %s" % (sorted(declared - set(FIXTURES)), sorted(set(FIXTURES) - declared)))

# The linter can only see that the `fixture` field is non-empty; the fixtures live here.
# So this is where "the fixture it names actually exists" has to be asserted, or a
# manifest can claim a statement nobody ever parsed with it.
missing_fixture = []
for path in (PROJECT / "pipeline" / "formats").glob("*.json"):
    if path.name.endswith(".schema.json"):
        continue
    manifest = json.loads(path.read_text())
    if manifest.get("fixture") not in FIXTURES:
        missing_fixture.append((path.name, manifest.get("fixture")))
check("every manifest names a fixture that exists here", not missing_fixture,
      str(missing_fixture))


# ---------------------------------------------------------------------------
print("== each format reads its own statement correctly")
for name, spec in sorted(FIXTURES.items()):
    delimiter = spec["delimiter"]
    path = write("%s.csv" % name, spec["rows"], delimiter=delimiter)
    detected = formats.detect(path)
    if not check("%s: detected" % name, detected["name"] == name, "got %s" % detected["name"]):
        continue
    rows = formats.parse(path, detected)
    check("%s: reads exactly its transaction rows" % name, len(rows) == len(spec["expect"]),
          "got %d: %r" % (len(rows), rows))
    for got, (date, amount, currency, counterparty) in zip(rows, spec["expect"]):
        check("%s: date" % name, got["date"] == date, "%s != %s" % (got["date"], date))
        check("%s: amount and sign" % name, cents(got["amount"]) == cents(amount),
              "%s != %s" % (got["amount"], amount))
        check("%s: currency" % name, (got["currency"] or "EUR").upper() == currency,
              "%s != %s" % (got["currency"], currency))
        check("%s: counterparty" % name, counterparty in (got["counterparty"] or ""),
              "%r lacks %r" % (got["counterparty"], counterparty))
    check("%s: a thousands separator is not read as a decimal" % name,
          cents(rows[0]["amount"]) == -123456, str(rows[0]["amount"]))


# ---------------------------------------------------------------------------
# The mutation table. Each entry takes the valid rows and breaks one thing about
# a single transaction line, which must refuse the whole file.
# ---------------------------------------------------------------------------
print("== a broken statement is refused whole, never imported in part")


def mutate(rows, row_index, column, value):
    copy = [list(r) for r in rows]
    copy[row_index][column] = value
    return copy


for name, spec in sorted(FIXTURES.items()):
    rows, delimiter = spec["rows"], spec["delimiter"]
    header = spec.get("header_row", 0)
    first = header + 1
    money = spec["amount_column"]
    date_column = spec.get("date_column", 0)

    for label, value in [
        ("text where money should be", "NOT_AN_AMOUNT"),
        ("an empty-looking placeholder", "-"),
        ("NaN", "NaN"),
        ("Infinity", "Infinity"),
        ("a formula", "=SUM(A1:A9)"),
    ]:
        refuses(mutate(rows, first, money, value), "%s: refuses %s" % (name, label), delimiter)

    refuses(mutate(rows, first, date_column, "01.01.0001" if "." in str(rows[first][date_column])
                   else "0001-01-01"),
            "%s: refuses a year no statement could carry" % name, delimiter)

    # A file whose decimal style contradicts the format reads every amount wrong by a
    # factor of a hundred or a thousand, and stays self-consistent while doing it.
    style_swapped = [list(r) for r in rows]
    swap = str.maketrans({",": ".", ".": ","})
    for index in range(first, len(rows)):
        if len(style_swapped[index]) > money and style_swapped[index][money]:
            style_swapped[index][money] = str(style_swapped[index][money]).translate(swap)
    style_swapped.append(list(style_swapped[first]))
    style_swapped.append(list(style_swapped[first + 1] if first + 1 < len(rows)
                              else style_swapped[first]))
    refuses(style_swapped, "%s: refuses the wrong decimal style" % name, delimiter)


# ---------------------------------------------------------------------------
print("== files that are unusual but valid still read")

db = FIXTURES["deutsche-bank-giro"]
check("a UTF-8 BOM does not hide the header",
      len(parse(write("bom.csv", db["rows"], delimiter=";", bom=True))) == 2)
check("a CP1252 export still decodes",
      len(parse(write("cp1252.csv", db["rows"], delimiter=";", encoding="cp1252"))) == 2)
check("unix line endings are fine",
      len(parse(write("unix.csv", db["rows"], delimiter=";", newline="\n"))) == 2)

quoted = [list(r) for r in db["rows"]]
quoted[1][2] = "Einkauf; Filiale 12\nZeile zwei"
rows = parse(write("quoted.csv", quoted, delimiter=";"))
check("a delimiter and a newline inside a quoted field do not split the row",
      len(rows) == 2 and "Zeile zwei" in rows[0]["purpose"], str(rows))

trailer = [list(r) for r in db["rows"]] + [["Alter Kontostand", "", "", "1.000,00", "EUR", ""]]
rows = parse(write("trailer.csv", trailer, delimiter=";"))
check("a trailer line is not counted as a transaction", len(rows) == 2, str(rows))

blank_amount = [list(r) for r in db["rows"]] + [["06.03.2026", "HINWEIS", "Kein Betrag", "", "EUR", ""]]
rows = parse(write("blank.csv", blank_amount, delimiter=";"))
check("a dated row with no amount at all is informational, not a lost transaction",
      len(rows) == 2, str(rows))

zero = [list(r) for r in db["rows"]] + [["06.03.2026", "NULLBUCHUNG", "storniert", "0,00", "EUR", ""]]
check("a zero-amount row is skipped", len(parse(write("zero.csv", zero, delimiter=";"))) == 2)

empty = [db["rows"][0]]
check("a statement for a month with no activity reads as zero rows, not an error",
      parse(write("empty.csv", empty, delimiter=";")) == [])


# ---------------------------------------------------------------------------
print("== a statement that states its own total is held to it")
card = FIXTURES["deutsche-bank-kreditkarte"]
wrong_total = [list(r) for r in card["rows"]]
wrong_total[-1][6] = "9.999,99"
refuses(wrong_total, "the printed Saldo must match the rows read", ";")
check("and the honest one still parses",
      len(parse(write("card-ok.csv", card["rows"], delimiter=";"))) == 2)


# Anti-shrink guard: exact count at implementation time. May only ever be RAISED
# when checks are added — never lowered (see AGENTS.md: never weaken a test).
MIN_CHECKS = 160
check("suite did not shrink", total_checks >= MIN_CHECKS,
      "total_checks=%d < %d" % (total_checks, MIN_CHECKS))

shutil.rmtree(tmp)
if failures:
    print("\nFAILED: %s" % ", ".join(sorted(set(failures))[:12]))
    sys.exit(1)
print("\nFormat matrix passed: %d checks over %d formats." % (total_checks, len(FIXTURES)))
