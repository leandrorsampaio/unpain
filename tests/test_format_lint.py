"""A bank format definition is code, and this is its compiler.

A manifest decides which column holds the money and whether a comma is a decimal point.
Getting that wrong is not a crash — it is a statement that imports cleanly with every
amount off by a factor of a hundred. So each field gets a mutation that must be refused.

The other half is ambiguity. Detection used to return the first manifest that matched,
in directory order, so two overlapping signatures meant the *filename* decided which
parser read somebody's bank statement, and adding a format could silently change how an
existing bank was read.

Usage: .venv/bin/python tests/test_format_lint.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from sandbox import PROJECT

tmp = Path(tempfile.mkdtemp(prefix="fa-format-lint-"))
os.environ["FA_ROOT"] = str(tmp)

sys.path.insert(0, str(PROJECT))
from pipeline import format_lint, formats  # noqa: E402

failures = []
total_checks = 0


def check(name, cond, detail=""):
    global total_checks
    total_checks += 1
    print("  %s %s %s" % ("OK " if cond else "FAIL", name, detail if not cond else ""))
    if not cond:
        failures.append(name)


VALID = {
    "name": "test-bank",
    "signature": ["Buchungstag", "Betrag"],
    "delimiter": ";",
    "decimal": "comma",
    "date_format": "%d.%m.%Y",
    "columns": {"date": "Buchungstag", "amount": "Betrag", "counterparty": "Name"},
    "currency": "EUR",
    "verification_status": "verified-sanitized",
    "verified_at": "2026-08-08",
    "fixture": "test-bank",
}


def _raises(catalogue):
    try:
        format_lint.assert_clean(catalogue)
    except format_lint.ManifestError:
        return True
    return False


def _no_match(path):
    try:
        formats.detect(path)
    except ValueError as exc:
        return "No format matched" in str(exc)
    return False


def _ambiguous():
    """Two shipped formats made to overlap, then asked to read one file.

    Patching load_formats is the only honest way to build this: the real catalogue is
    unambiguous, which is exactly what the linter guarantees, so the ambiguous case has
    to be constructed."""
    original = formats.load_formats
    a = dict(VALID, name="alpha", signature=["Buchungstag", "Betrag"])
    b = dict(VALID, name="beta", signature=["Buchungstag"])
    formats.load_formats = lambda: [a, b]
    try:
        formats.detect(tmp / "ambiguous.csv")
        return False
    except ValueError as exc:
        return "matches 2 formats" in str(exc) and "alpha" in str(exc) and "beta" in str(exc)
    finally:
        formats.load_formats = original


def problems(patch=None, drop=()):
    manifest = dict(VALID)
    manifest.update(patch or {})
    for key in drop:
        manifest.pop(key, None)
    return format_lint.lint_manifest(manifest, "test.json")


print("== the shipped catalogue is valid")
report = format_lint.lint()
check("every checked-in manifest lints clean", report["ok"], "\n  ".join(report["problems"][:6]))
check("and there are as many as the format matrix covers", report["manifests"] == 10,
      str(report["manifests"]))
check("a valid manifest produces no problems", problems() == [], str(problems()))


print("== every field that decides how money is read")
CASES = [
    ("no name", {}, ("name",), "name"),
    ("an empty name", {"name": "  "}, (), "name"),
    ("no signature", {}, ("signature",), "signature"),
    ("an empty signature", {"signature": []}, (), "signature"),
    ("a signature of non-text", {"signature": [1, 2]}, (), "signature"),
    ("a multi-character delimiter", {"delimiter": ";;"}, (), "delimiter"),
    ("an unknown decimal style", {"decimal": "apostrophe"}, (), "decimal"),
    ("a sign that is not 1 or -1", {"sign": 100}, (), "sign"),
    ("a sign of zero, which erases every amount", {"sign": 0}, (), "sign"),
    ("a date format that is not one", {"date_format": "yesterday"}, (), "date_format"),
    ("an unparseable date pattern", {"date_format": "%Q.%Z"}, (), "date_format"),
    ("no columns at all", {}, ("columns",), "columns"),
    ("no date column", {"columns": {"amount": "Betrag"}}, (), "columns.date"),
    ("no amount column", {"columns": {"date": "Buchungstag"}}, (), "columns"),
    ("both amount models at once",
     {"columns": {"date": "d", "amount": "a", "amount_debit": "s"}}, (), "columns"),
    ("a column mapped to a field the parser never reads",
     {"columns": {"date": "d", "amount": "a", "colour": "c"}}, (), "columns.colour"),
    ("a column mapped to something that is neither a name nor an index",
     {"columns": {"date": "d", "amount": True}}, (), "columns.amount"),
    ("a currency that is not a code", {"currency": "euros"}, (), "currency"),
    ("a misspelled top-level key", {"decimal_separator": "comma"}, (), "decimal_separator"),
    ("no verification status", {}, ("verification_status",), "verification_status"),
    ("an invented verification status", {"verification_status": "trust-me"}, (),
     "verification_status"),
    ("a verified_at that is not a date", {"verified_at": "last summer"}, (), "verified_at"),
    ("no fixture", {}, ("fixture",), "fixture"),
    ("a statement_total with no match text", {"statement_total": {"amount_column": 3}}, (),
     "statement_total.match"),
    ("a statement_total with no amount column", {"statement_total": {"match": "Saldo"}}, (),
     "statement_total.amount_column"),
    ("a balance_row that is not an object", {"balance_row": "Kontostand"}, (), "balance_row"),
]
for label, patch, drop, field in CASES:
    found = problems(patch, drop)
    check("rejected: %s" % label,
          any(field in problem.field for problem in found),
          "fields reported: %s" % [p.field for p in found])

check("a manifest that is not an object at all is rejected",
      format_lint.lint_manifest([1, 2, 3], "test.json") != [])
check("every problem in one manifest is reported, not just the first",
      len(problems({"decimal": "x", "sign": 5, "delimiter": "??"})) >= 3,
      str([p.field for p in problems({"decimal": "x", "sign": 5, "delimiter": "??"})]))


print("== two formats may not both match one file")
overlap = [("a.json", dict(VALID, name="a", signature=["Datum", "Betrag"])),
           ("b.json", dict(VALID, name="b", signature=["Datum", "Betrag", "Extra"]))]
clashes = format_lint.overlapping_signatures(overlap)
check("a signature that is a subset of another is reported", len(clashes) == 1, str(clashes))
check("and both formats are named", set(clashes[0][:2]) == {"a", "b"}, str(clashes))
distinct = [("a.json", dict(VALID, name="a", signature=["Datum", "Betrag"])),
            ("b.json", dict(VALID, name="b", signature=["Booking Date", "Amount"]))]
check("distinct signatures do not clash", format_lint.overlapping_signatures(distinct) == [])
check("the shipped catalogue has no overlaps",
      format_lint.overlapping_signatures(format_lint.load_manifests()) == [],
      str(format_lint.overlapping_signatures(format_lint.load_manifests())))


print("== a catalogue is linted as a whole")
catalogue = tmp / "formats"
catalogue.mkdir()
(catalogue / "one.json").write_text(json.dumps(dict(VALID, name="one")), encoding="utf-8")
(catalogue / "two.json").write_text(json.dumps(dict(VALID, name="one")), encoding="utf-8")
report = format_lint.lint(catalogue)
check("two manifests with the same name are rejected",
      any("duplicates" in problem for problem in report["problems"]), str(report["problems"]))
(catalogue / "two.json").write_text("{not json", encoding="utf-8")
report = format_lint.lint(catalogue)
check("a manifest that is not readable JSON is reported",
      any("readable JSON" in problem for problem in report["problems"]), str(report["problems"]))
check("and assert_clean refuses to start on it", _raises(catalogue))
(catalogue / "two.json").unlink()
(catalogue / "manifest.schema.json").write_text('{"title": "not a manifest"}', encoding="utf-8")
report = format_lint.lint(catalogue)
check("a schema file is not mistaken for a manifest", report["ok"], str(report["problems"]))
check("and a clean catalogue starts", not _raises(catalogue))


print("== detection refuses to guess")
statement = tmp / "ambiguous.csv"
statement.write_text("Buchungstag;Betrag;Extra\n04.03.2026;-1,00;x\n", encoding="utf-8")
check("a file no format matches is refused", _no_match(statement))
check("and a file two formats match is refused, naming both", _ambiguous())
check("best-guess formats are visible in the report",
      "barclays-de" in format_lint.lint()["best_guess"], str(format_lint.lint()["best_guess"]))


# Anti-shrink guard: exact count at implementation time. May only ever be RAISED
# when checks are added — never lowered (see AGENTS.md: never weaken a test).
MIN_CHECKS = 43
check("suite did not shrink", total_checks >= MIN_CHECKS,
      "total_checks=%d < %d" % (total_checks, MIN_CHECKS))

shutil.rmtree(tmp)
print()
if failures:
    print("FAILED: %s" % ", ".join(failures))
    sys.exit(1)
print("All checks passed.")
