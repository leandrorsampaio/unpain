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
import re
import shutil
import subprocess
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from . import fx, money, settle, store
from .util import DATA, ROOT, read_json

SCHEMA_VERSION = 2
METADATA_SHEET = "Metadata"


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
    for label, path in _input_files(year):
        value, problem = _read_input(path)
        feed(label, {"unreadable": problem} if problem else value)
    feed("config.json", _config_inputs())
    return "sha256:" + digest.hexdigest()[:32]


# The configuration keys that can move a figure or a label in an export. `security` is
# excluded on purpose — it holds credentials, it cannot change a number, and an audit
# artifact should not carry so much as a hash of it. Cosmetic keys are excluded for the
# same reason the FX cache is: changing a colour must not mark every workbook stale.
CONFIG_INPUT_KEYS = ("people", "person_labels", "base_currency", "currencies",
                     "reference_ratio", "items_threshold_eur",
                     "transfer_match_tolerance_cents", "transfer_match_window_days")


def _read_input(path):
    """One stored input, and whether it could be read. Returns (value, problem).

    A file that will not parse is still a fact about the state this export was built
    from, so it is hashed as an unreadable file rather than crashing the export or —
    worse — being skipped, which would make a corrupt document and an absent one produce
    the same digest.

    An absent file and an empty one *do* hash the same, deliberately: no decisions and an
    empty decisions document say the same thing, and marking a workbook stale because
    some unrelated code path wrote `{}` is a false alarm, not a change.
    """
    if not path.exists():
        return None, None
    try:
        value = read_json(path, default=None)
    except ValueError as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)
    return (None if value in ({}, []) else value), None


def input_problems(year):
    """The stored inputs that could not be read, by name."""
    return sorted(label for label, path in _input_files(year) if _read_input(path)[1])


def _config_inputs():
    document = read_json(ROOT / "config.json", default={})
    return {key: document.get(key) for key in CONFIG_INPUT_KEYS if key in document}


def _input_files(year):
    """Every stored document that can change what an export says.

    Enumerated rather than inferred, because "whatever the export happens to read" is not
    a definition anybody can check. Categorisation is derived on read, so a rule, a
    category rename or a tax-bucket edit changes the workbook without touching a single
    transaction — each of those is an input and belongs in the digest.

    Deliberately absent: the FX rate cache. Stored rows already carry the rate that
    converted them, so refreshing the cache moves no figure in any export — hashing it
    would mark every workbook stale after an `fx-update` that changed nothing, which is
    the false alarm that teaches people to ignore the true ones.
    """
    year_dir = DATA / str(year)
    return [
        ("decisions.json", year_dir / "decisions.json"),
        ("months.json", year_dir / "months.json"),
        ("closings.json", year_dir / "closings.json"),
        ("ratio-overrides.json", year_dir / "ratio-overrides.json"),
        ("recurring-overrides.json", year_dir / "recurring-overrides.json"),
        ("anchors.json", year_dir / "anchors.json"),
        ("accounts.json", DATA / "accounts.json"),
        ("uploads.json", DATA / "uploads.json"),
        ("tax-buckets.json", DATA / "tax-buckets.json"),
        ("merchant-rules.json", ROOT / "rules" / "merchant-rules.json"),
        ("categories.json", ROOT / "rules" / "categories.json"),
    ]


def canonical_cell(value):
    """One spelling for a cell, whether it is on its way into a sheet or back out.

    A digest is only checkable if both sides serialise identically, and a round trip
    through xlsx does not preserve Python types: 5 and 5.0 come back indistinguishable,
    an empty cell becomes None, a date may arrive as a datetime. Numbers therefore
    normalise through Decimal — `5`, `5.0` and `5.00` are all "5" — and everything else
    is pinned to one textual form.
    """
    if value is None:
        return ""
    if isinstance(value, bool):                 # bool before int: it is a subclass
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return "nan"                        # never silently hash a non-finite number
        if isinstance(value, float):
            # xlsx stores a float as text and the round trip is not bit-exact: a ratio
            # written as -0.23801913086401916 comes back -0.2380191308640192. Twelve
            # significant digits is far past anything this app means — money has two
            # decimals — and it is stable in both directions.
            value = float("%.12g" % value)
        normalized = Decimal(str(value)).normalize()
        if normalized == normalized.to_integral_value():
            normalized = normalized.quantize(Decimal(1))
        return format(normalized, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def sheet_matrix(workbook):
    """Every cell of every sheet except Metadata, in workbook order.

    The digest covers the written workbook rather than the dicts it was built from,
    because the question it has to answer is "has this file been altered", and a hash of
    the inputs cannot answer that. Metadata is excluded for the obvious reason: it is
    where the digest is about to be written.
    """
    matrix = []
    for sheet in workbook.worksheets:
        if sheet.title == METADATA_SHEET:
            continue
        rows = [[canonical_cell(cell) for cell in row]
                for row in sheet.iter_rows(values_only=True)]
        while rows and not any(rows[-1]):        # trailing blank rows are not content
            rows.pop()
        matrix.append([sheet.title, rows])
    return matrix


def content_digest(workbook):
    """A hash over the workbook's own cells, recomputable from the saved file."""
    payload = json.dumps(sheet_matrix(workbook), sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def fx_evidence(year):
    """Which rates the year's converted amounts rest on, and how fresh the cache is."""
    dates, currencies = set(), set()
    try:
        cache = fx.cache_info()
        for txn in store.effective_year(year):
            currency = (txn.get("currency") or "EUR").upper()
            if currency == "EUR":
                continue
            currencies.add(currency)
            if txn.get("fx_rate_date"):
                dates.add(txn["fx_rate_date"])
    except Exception as exc:                # noqa: BLE001 - metadata must not break an export
        # Degrading silently to "-" is what makes an audit artifact untrustworthy: the
        # reader cannot tell "no foreign currency" from "this could not be determined".
        # So the failure is recorded as a failure, and `build` marks provenance
        # incomplete because of it.
        return {"error": "%s: %s" % (type(exc).__name__, exc),
                "source": "COULD NOT BE DETERMINED — see error"}
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
    except Exception as exc:                # noqa: BLE001
        return {"error": "%s: %s" % (type(exc).__name__, exc),
                "needs_review": "COULD NOT BE DETERMINED — see error"}
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


def build(year, workbook, *, export_type, row_count, as_of=None, filters=None):
    """Everything an export declares about itself. Deterministic for fixed inputs.

    Takes the finished `workbook` rather than the rows it was built from, because the
    content digest has to describe the file somebody will later open — a hash of the
    inputs cannot tell anyone whether the cells in front of them were edited.
    """
    fx_state = fx_evidence(year)
    review = review_state(year)
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "export_type": export_type,
        "generated_at": generation_time(as_of),
        "app_version": app_version(),
        "reporting_year": int(year),
        "filters": filters or "none (the whole year)",
        "base_currency": "EUR",
        "rounding_policy": "half-to-even, version %d" % money.ROUNDING_POLICY_VERSION,
        "row_count": row_count,
        "content_digest": content_digest(workbook),
        "source_digest": source_digest(year),
        "sources": sources_used(year),
        "fx": fx_state,
        "review": review,
    }
    # Provenance is either complete or it says so. A workbook that quietly dropped half
    # its evidence looks exactly like one that never needed any.
    problems = ["%s could not be determined" % part
                for part, state in (("exchange rates", fx_state), ("review state", review))
                if state.get("error")]
    unreadable = input_problems(year)
    if unreadable:
        problems.append("these inputs could not be read: %s" % ", ".join(unreadable))
    metadata["provenance"] = ("complete" if not problems
                              else "INCOMPLETE — %s" % "; ".join(problems))
    return metadata


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
                  "schema_version", "provenance", "filters", "base_currency",
                  "rounding_policy", "row_count", "content_digest", "source_digest"):
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
    impossible. This is only half the job: `modified` is re-stamped by openpyxl during
    `save()`, and the zip container carries its own clock, so `finalize` has to finish it
    on the written file.
    """
    properties = workbook.properties
    properties.creator = "UnPAIN"
    properties.lastModifiedBy = "UnPAIN"
    stamp = datetime.fromisoformat(metadata["generated_at"]).replace(tzinfo=None)
    properties.created = stamp
    properties.modified = stamp
    properties.title = "%s %s" % (metadata["export_type"], metadata["reporting_year"])
    return workbook


MODIFIED_TAG = re.compile(rb"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)")


def finalize(path, metadata):
    """Rewrite the saved workbook so identical inputs really do produce identical bytes.

    Two things survive `normalize` and defeat a byte comparison, and both are invisible
    until you export the same data twice more than a second apart — which is exactly what
    the first version of the test failed to do, so it reported a reproducibility that was
    not there:

      1. openpyxl re-stamps `dcterms:modified` with the wall clock inside `save()`,
         overwriting the value set on the properties object.
      2. every zip member carries its own modification time, and xlsx is a zip.

    So the file is rebuilt once: the timestamp corrected in `docProps/core.xml`, and every
    member rewritten with a fixed date, a fixed compression level and fixed permissions,
    in the order the members already had.
    """
    path = Path(path)
    stamp = datetime.fromisoformat(metadata["generated_at"])
    fixed = (max(stamp.year, 1980), stamp.month, stamp.day,   # zip epoch starts at 1980
             stamp.hour, stamp.minute, stamp.second)
    replacement = stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ").encode()

    with zipfile.ZipFile(path) as source:
        members = [(info.filename, source.read(info.filename)) for info in source.infolist()]

    temporary = path.with_suffix(".%d.tmp" % os.getpid())
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as target:
        for name, payload in members:
            if name == "docProps/core.xml":
                # \g<1>, not \1: the replacement starts with a year, and "\1" followed by
                # a digit is read as group 12 or as an octal escape. It silently ate the
                # opening tag and produced a workbook Excel would refuse to open.
                payload = MODIFIED_TAG.sub(rb"\g<1>" + replacement + rb"\g<2>", payload)
            info = zipfile.ZipInfo(name, date_time=fixed)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16      # not the umask of whoever ran the export
            info.create_system = 0                # nor which operating system it ran on
            target.writestr(info, payload)
    shutil.move(str(temporary), str(path))
    return path
