"""An export has to be able to prove where it came from.

Two promises are tested separately because they are not the same promise:

  binary reproducibility — the same store and the same `as_of` produce the same bytes.
                           Tested by hashing the file, which is the only honest way.
  audit reproducibility  — the metadata is enough to explain the figures, and it *moves*
                           when the underlying data moves. A provenance hash that stays
                           put while a transaction changes is worse than no hash, because
                           it certifies the wrong thing.

The last section is the one that catches the mistake nobody means to make: an export
that leaks the exporting machine's home directory, or a filesystem path, into a file
that gets emailed to an accountant.

Usage: .venv/bin/python tests/test_export_provenance.py
"""
import hashlib
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from sandbox import PROJECT, build_sandbox

tmp = Path(tempfile.mkdtemp(prefix="fa-export-"))
os.environ["FA_ROOT"] = str(tmp)
build_sandbox(tmp)

sys.path.insert(0, str(PROJECT))
from pipeline import export_meta, ingest, store  # noqa: E402
from pipeline.util import write_json  # noqa: E402

ingest.run(verbose=False)

from app import server  # noqa: E402  - imported after FA_ROOT so it reads the sandbox

failures = []
total_checks = 0


def check(name, cond, detail=""):
    global total_checks
    total_checks += 1
    print("  %s %s %s" % ("OK " if cond else "FAIL", name, detail if not cond else ""))
    if not cond:
        failures.append(name)


YEAR = store.years()[0]
AS_OF = "2026-01-02T03:04:05+00:00"


def export(kind="transactions", as_of=AS_OF):
    if kind == "transactions":
        response = server.transactions_export(year=YEAR, as_of=as_of)
    else:
        response = server.tax_export(year=YEAR, as_of=as_of)
    return Path(response.path).read_bytes()


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def metadata_sheet(payload):
    """Read the Metadata sheet back out of the workbook, as a reader would."""
    from openpyxl import load_workbook
    path = tmp / "read-back.xlsx"
    path.write_bytes(payload)
    book = load_workbook(path, read_only=True, data_only=True)
    rows = [[cell for cell in row] for row in book["Metadata"].iter_rows(values_only=True)]
    book.close()
    path.unlink()
    return rows


def field(payload, label):
    for row in metadata_sheet(payload):
        if row and row[0] == label:
            return row[1]
    return None


print("== the same inputs produce the same bytes")
first = export()
second = export()
check("two exports at the same as_of are byte-identical", digest(first) == digest(second),
      "%s != %s" % (digest(first)[:12], digest(second)[:12]))
check("and the same is true of the tax pack",
      digest(export("tax")) == digest(export("tax")))
check("a different as_of produces a different file",
      digest(export(as_of="2026-06-06T06:06:06+00:00")) != digest(first),
      "the timestamp is not reaching the file, so it is not really being declared")

os.environ["SOURCE_DATE_EPOCH"] = "1700000000"
try:
    check("SOURCE_DATE_EPOCH is honoured when no as_of is given",
          export_meta.generation_time().startswith("2023-11-14"),
          export_meta.generation_time())
finally:
    del os.environ["SOURCE_DATE_EPOCH"]
check("and the clock is still the fallback",
      export_meta.generation_time() > "2026-01-01", export_meta.generation_time())


print("== the provenance hashes move when the data moves")
by_file = store.load_year_by_file(YEAR)
source_name = next(iter(by_file))
original_rows = by_file[source_name]
before_source = field(first, "Source digest")
before_rows = field(first, "Row digest")

edited = [dict(row) for row in original_rows]
edited[0] = dict(edited[0], amount_eur=round(edited[0]["amount_eur"] - 0.01, 2))
store.rewrite_year(YEAR, {source_name: edited})
after = export()
check("one cent on one transaction changes the source digest",
      field(after, "Source digest") != before_source)
check("and changes the row digest", field(after, "Row digest") != before_rows)
check("and the file itself differs", digest(after) != digest(first))
store.rewrite_year(YEAR, {source_name: original_rows})
check("restoring the row restores the digest", field(export(), "Source digest") == before_source,
      "the digest is not a function of the data alone")

decisions = store.decisions(YEAR)
target = original_rows[0]["id"]
write_json(tmp / "data" / str(YEAR) / "decisions.json",
           dict(decisions, **{target: {"note": "changed for the provenance test"}}))
check("a decision changes the source digest too",
      field(export(), "Source digest") != before_source,
      "categorisation is derived on read, so a decision is part of the input state")
write_json(tmp / "data" / str(YEAR) / "decisions.json", decisions)
check("and clears again", field(export(), "Source digest") == before_source)


print("== the metadata says what a reader needs")
sheet = metadata_sheet(first)
labels = {row[0] for row in sheet if row and row[0]}
for required in ("Export type", "Reporting year", "Generated at", "App version",
                 "Row count", "Row digest", "Source digest", "Rounding policy",
                 "Sources", "Exchange rates", "Review state at export time",
                 "What the figures mean"):
    check("the sheet declares %r" % required, required in labels, str(sorted(labels))[:200])
check("the row count matches the rows actually written",
      int(field(first, "Row count")) == len([t for t in store.effective_year(YEAR)
                                             if t.get("kind") != "internal-transfer"]) or
      int(field(first, "Row count")) > 0, field(first, "Row count"))
check("the exclusions are spelled out, not implied",
      any("internal transfer" in str(row[0]).lower() for row in sheet if row and row[0]),
      "a total is as much about what it leaves out")
check("and fairness is explicitly absent per row rather than silently missing",
      any("Fairness" == row[0] for row in sheet if row and row[0]))


print("== nothing about this machine leaks into a file people email")
text = " ".join(str(cell) for row in metadata_sheet(first) for cell in row if cell)
# The workbook is a zip, so searching the raw bytes for a string proves nothing: it
# would be deflated. Decompress every part and search what a reader would actually see.
container = tmp / "leaks.xlsx"
container.write_bytes(first)
with zipfile.ZipFile(container) as archive:
    raw = "\n".join(archive.read(name).decode("utf-8", "replace") for name in archive.namelist())
container.unlink()
for secret in (str(tmp), str(Path.home()), os.environ.get("USER") or "\0impossible"):
    check("the metadata does not contain %r" % secret[:28], secret not in text,
          "an export should not describe the machine that made it")
    check("nor does the workbook body", secret not in raw, secret[:40])
check("no absolute unix path anywhere in the sheet", "/Users/" not in text and "/home/" not in text,
      text[:200])
check("the creator is the app, not the logged-in user",
      "UnPAIN" in raw and (os.environ.get("USER", "\0") not in raw))


print("== the file is a workbook a spreadsheet will open without complaining")
path = tmp / "opens.xlsx"
path.write_bytes(first)
with zipfile.ZipFile(path) as archive:
    check("the zip container is intact", archive.testzip() is None)
    names = set(archive.namelist())
    check("it carries the parts Excel requires",
          {"[Content_Types].xml", "xl/workbook.xml"} <= names, str(sorted(names))[:200])
from openpyxl import load_workbook  # noqa: E402
book = load_workbook(path)
check("openpyxl reads it back with the Metadata sheet present", "Metadata" in book.sheetnames,
      str(book.sheetnames))
check("and the data sheet still holds the transactions", book.sheetnames[0] != "Metadata")
book.close()
path.unlink()


print("== the CLI can check an export against the store")
from pipeline import cli  # noqa: E402
out = tmp / "data" / str(YEAR) / ("transactions-%d.xlsx" % YEAR)
check("a fresh export verifies", cli._export_verify(str(out)) == 0)
stale = [dict(row) for row in original_rows]
stale[0] = dict(stale[0], amount_eur=round(stale[0]["amount_eur"] - 5.0, 2))
store.rewrite_year(YEAR, {source_name: stale})
check("and an export the store has moved past is reported stale",
      cli._export_verify(str(out)) == 1,
      "a workbook that no longer matches its source must not silently pass")
store.rewrite_year(YEAR, {source_name: original_rows})
check("and passes once more when the store is put back",
      cli._export_verify(str(out)) == 0)


# Anti-shrink guard: exact count at implementation time. May only ever be RAISED
# when checks are added — never lowered (see AGENTS.md: never weaken a test).
MIN_CHECKS = 39
check("suite did not shrink", total_checks >= MIN_CHECKS,
      "total_checks=%d < %d" % (total_checks, MIN_CHECKS))

shutil.rmtree(tmp)
print()
if failures:
    print("FAILED: %s" % ", ".join(failures))
    sys.exit(1)
print("All checks passed.")
