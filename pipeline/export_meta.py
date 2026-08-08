"""What an exported workbook has to say about where its numbers came from.

A spreadsheet outlives the app that made it. Someone opens `transactions-2025.xlsx` two
years later and the only questions that matter are: which data was this built from, what
was included and excluded, and would the same inputs produce it again. A file that cannot
answer those is a screenshot of a number.

So every export carries a Metadata sheet: the sources it was built from and their
hashes, the FX evidence behind any converted amount, how much of the period was reviewed
and closed, and the definitions that decide what "expenses" means — particularly the
exclusions, because a total is as much about what it leaves out.

Two different promises get confused here, so they are kept apart (plan §IMP-12):

  audit reproducibility  — the metadata is enough to explain and rebuild the figures.
                           Always provided.
  binary reproducibility — the same inputs produce the same bytes. Only possible when
                           the generation timestamp is supplied rather than read from the
                           clock, so `as_of` is an argument and `SOURCE_DATE_EPOCH` is
                           honoured when set.

Nothing here is canonical. It describes a snapshot; it is never read back to compute one.
"""
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone

from . import fx, money, settle, store
from .util import DATA, ROOT, read_json

SCHEMA_VERSION = 1


def generation_time(as_of=None):
    """The timestamp the export declares.

    Explicit beats the clock: a workbook stamped with `datetime.now()` can never be
    byte-identical to itself, so a rebuild has no way to prove it matches. SOURCE_DATE_EPOCH
    is the conventional way for a build to say "pretend it is this moment", and honouring
    it costs nothing.
    """
    if as_of:
        return as_of if isinstance(as_of, str) else as_of.isoformat()
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch and epoch.isdigit():
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).replace(
            microsecond=0).isoformat()
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def app_version():
    """The build this came from. `unknown` is a fine answer; a wrong one is not."""
    declared = os.environ.get("UNPAIN_VERSION")
    if declared:
        return declared
    try:
        result = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:                       # noqa: BLE001 - no git, no network, no problem
        pass
    return "unknown"


def source_digest(year):
    """A hash over the canonical inputs the export was built from.

    Computed from the stored files rather than from the rendered rows, so it identifies
    the *input* state. Change a transaction, a decision or a rule and this moves; reorder
    the sheet and it does not.

    It hashes the parsed content re-serialised canonically, never the file bytes. Saving
    a file back unchanged can still rewrite its key order or whitespace, and a digest that
    called that a change would cry stale at exports that are perfectly current — which is
    the failure mode that gets integrity warnings ignored.
    """
    digest = hashlib.sha256()

    def feed(label, value):
        digest.update(label.encode())
        digest.update(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")

    directory = DATA / str(year) / "transactions"
    if directory.exists():
        for path in sorted(directory.glob("*.jsonl")):
            rows = []
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    rows.append({"unparseable": line})   # still a difference worth hashing
            feed(path.name, rows)
    # Decisions and merchant rules are inputs too: categorisation is derived on read, so a
    # rule edit changes what the export says without touching a single stored transaction.
    feed("decisions.json", read_json(DATA / str(year) / "decisions.json", default={}))
    feed("merchant-rules.json",
         read_json(ROOT / "rules" / "merchant-rules.json", default={}).get("rules", []))
    return "sha256:" + digest.hexdigest()[:32]


def row_digest(rows, columns):
    """A hash over exactly what was written, so the sheet can be tied to this metadata."""
    digest = hashlib.sha256()
    for row in rows:
        digest.update("|".join(str(row.get(name, "")) for name in columns).encode("utf-8"))
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()[:32]


def fx_evidence(year):
    """Which rates the year's converted amounts rest on, and how fresh the cache is."""
    cache = fx.cache_info()
    dates, currencies = set(), set()
    try:
        for txn in store.effective_year(year):
            currency = (txn.get("currency") or "EUR").upper()
            if currency == "EUR":
                continue
            currencies.add(currency)
            if txn.get("fx_rate_date"):
                dates.add(txn["fx_rate_date"])
    except Exception:                       # noqa: BLE001 - metadata must not break an export
        pass
    return {
        "source": "ECB reference rates" if currencies else "not needed (euro only)",
        "currencies": sorted(currencies),
        "rate_dates": "%s – %s" % (min(dates), max(dates)) if dates else "-",
        "cache_newest": cache.get("newest_rate_date") or "-",
        "cache_read": (cache.get("modified_at") or "-")[:10],
    }


def review_state(year):
    """How settled the period was when this was exported.

    A total from a month still half in the review queue is a real number about an
    unfinished month, and the workbook should say which it is rather than let the reader
    assume.
    """
    try:
        summary = settle.year_summary(year)
        months = store.months_state(year)
    except Exception:                       # noqa: BLE001
        return {"needs_review": "-", "months_closed": "-", "annual_closed": "-"}
    closed = [key for key, state in months.items() if state == "closed"]
    return {
        "needs_review": summary.get("needs_review", 0),
        "months_closed": "%d of 12" % len([k for k in closed if not k.endswith("annual")]),
        "annual_closed": "yes" if len(closed) >= 12 else "no",
    }


def sources_used(year):
    """The statements behind the rows, newest first, with the hash of each upload."""
    uploads = read_json(DATA / "uploads.json", default={"uploads": []}).get("uploads", [])
    out = []
    for upload in uploads:
        if year not in (upload.get("years") or []):
            continue
        out.append({
            "name": upload.get("original_name") or upload.get("source_stem") or "-",
            "account": upload.get("account") or "-",
            "format": upload.get("format") or "-",
            "hash": (upload.get("file_hash") or "-")[:16],
            "processed_at": (upload.get("processed_at") or "-")[:19],
        })
    return sorted(out, key=lambda item: (item["processed_at"], item["name"]), reverse=True)


def build(year, *, export_type, rows, columns, as_of=None, filters=None):
    """Everything an export declares about itself. Deterministic for fixed inputs."""
    return {
        "schema_version": SCHEMA_VERSION,
        "export_type": export_type,
        "generated_at": generation_time(as_of),
        "app_version": app_version(),
        "reporting_year": int(year),
        "filters": filters or "none (the whole year)",
        "base_currency": "EUR",
        "rounding_policy": "half-to-even, version %d" % money.ROUNDING_POLICY_VERSION,
        "row_count": len(rows),
        "row_digest": row_digest(rows, columns),
        "source_digest": source_digest(year),
        "sources": sources_used(year),
        "fx": fx_evidence(year),
        "review": review_state(year),
    }


# What a total means, spelled out. Every one of these is an exclusion somebody could
# reasonably have expected to be included, which is exactly why it is written down.
DEFINITIONS = [
    ("Expenses", "Signed sums. A refund booked to a category reduces that category rather "
                 "than appearing as income."),
    ("Excluded — internal transfers", "Money moved between the household's own accounts. "
                                      "Counting it would double the spending."),
    ("Excluded — out of scope", "Rows marked out-of-scope are invisible to every total, by "
                                "instruction."),
    ("Splits", "A split transaction contributes its parts, never its parent. Filter "
               "in_expense_math = TRUE to get exactly the rows the app counts."),
    ("Year costs", "Excluded from the monthly picture, included in the annual one."),
    ("Fairness", "Deliberately absent per row: the income-proportional split is a yearly "
                 "figure computed from salary, never a per-transaction value."),
    ("Foreign amounts", "Converted at the ECB reference rate published for the rate date "
                        "shown, which is not always the booking date — the ECB publishes on "
                        "business days. This is bookkeeping, not what a card issuer charged."),
]


def write_sheet(workbook, metadata, *, bold):
    """Add the Metadata sheet. Ordering is fixed, so two identical exports match."""
    sheet = workbook.create_sheet("Metadata")
    sheet.append(["Field", "Value"])
    for cell in sheet[1]:
        cell.font = bold

    def section(title):
        sheet.append([])
        sheet.append([title, ""])
        sheet.cell(row=sheet.max_row, column=1).font = bold

    for field in ("export_type", "reporting_year", "generated_at", "app_version",
                  "schema_version", "filters", "base_currency", "rounding_policy",
                  "row_count", "row_digest", "source_digest"):
        sheet.append([field.replace("_", " ").capitalize(), str(metadata[field])])

    section("Sources")
    if metadata["sources"]:
        sheet.append(["Statement", "account · format · hash · processed"])
        for source in metadata["sources"]:
            sheet.append([source["name"], "%s · %s · %s · %s" % (
                source["account"], source["format"], source["hash"], source["processed_at"])])
    else:
        sheet.append(["Statement", "no tracked uploads for this year"])

    section("Exchange rates")
    for key, value in metadata["fx"].items():
        sheet.append([key.replace("_", " ").capitalize(), str(value)])

    section("Review state at export time")
    for key, value in metadata["review"].items():
        sheet.append([key.replace("_", " ").capitalize(), str(value)])

    section("What the figures mean")
    for name, meaning in DEFINITIONS:
        sheet.append([name, meaning])

    sheet.column_dimensions["A"].width = 34
    sheet.column_dimensions["B"].width = 104
    return sheet


def normalize(workbook, metadata):
    """Strip the things that would otherwise differ between two identical exports.

    openpyxl stamps the creating user and the current time into the document properties.
    Neither is information about the household's money, and both make a byte comparison
    impossible.
    """
    properties = workbook.properties
    properties.creator = "UnPAIN"
    properties.lastModifiedBy = "UnPAIN"
    stamp = datetime.fromisoformat(metadata["generated_at"]).replace(tzinfo=None)
    properties.created = stamp
    properties.modified = stamp
    properties.title = "%s %s" % (metadata["export_type"], metadata["reporting_year"])
    return workbook
