"""A bank format definition is code. This is the compiler for it.

`pipeline/formats/*.json` decides how every row of a statement is read: which column
holds the money, whether a comma is a decimal point or a thousands separator, which
direction the sign runs. A mistake in one of these files is not a crash — it is a
statement that imports cleanly with every amount wrong by a factor of a hundred, or a
whole column silently unread.

Two failures this exists to make impossible:

  a manifest nobody has ever read a statement with. "It looks right" is not evidence.
  Every enabled format must name a fixture, and `tests/test_format_matrix.py` must
  actually parse a statement with it;

  two manifests that both match one file. Detection used to return the first match in
  directory order, so adding a format could silently change how an existing bank's
  statements were read — and the alphabet decided which parser won.

Manifests also declare how well they are known. A format written from a screenshot and
one verified against a real export are not the same claim, and the app should not
present them as though they were.
"""
import json
import re
from pathlib import Path

FORMATS_DIR = Path(__file__).parent / "formats"

# How much evidence stands behind a manifest (plan Decision D). Anything less than
# verified is usable but must be visible as such, rather than quietly enjoying the same
# confidence as a format checked against a real statement.
VERIFICATION_STATUSES = ("verified-real", "verified-sanitized", "best-guess")

TOP_LEVEL_FIELDS = {
    "name", "signature", "delimiter", "decimal", "date_format", "columns", "currency",
    "sign", "account_column", "balance_row", "statement_total", "statement_period",
    "verification_status", "verified_at", "fixture", "_note",
}
COLUMN_KEYS = {"date", "amount", "amount_debit", "amount_credit", "currency",
               "counterparty", "purpose", "iban", "account", "force_review"}
DECIMALS = ("comma", "dot")
SIGNS = (1, -1)
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ManifestError(ValueError):
    """A format manifest is malformed. Names the file and the field."""

    def __init__(self, file, field, message, fix=None):
        self.file, self.field, self.fix = str(file), field, fix
        super().__init__("%s: %s — %s%s" % (Path(file).name, field, message,
                                            " (%s)" % fix if fix else ""))


def _problem(problems, file, field, message, fix=None):
    problems.append(ManifestError(file, field, message, fix))


def lint_manifest(manifest, file, *, require_fixture=True):
    """Everything wrong with one manifest, rather than only the first thing."""
    problems = []
    if not isinstance(manifest, dict):
        return [ManifestError(file, "<root>", "must be a JSON object")]

    unknown = sorted(set(manifest) - TOP_LEVEL_FIELDS)
    if unknown:
        _problem(problems, file, ", ".join(unknown), "is not a manifest field",
                 "a misspelled key is ignored, and the setting it meant never applies")

    name = manifest.get("name")
    if not isinstance(name, str) or not name.strip():
        _problem(problems, file, "name", "must be a non-empty format name")

    signature = manifest.get("signature")
    if not isinstance(signature, list) or not signature:
        _problem(problems, file, "signature", "must list the header cells that identify this format",
                 "with no signature the format matches nothing, or everything")
    elif not all(isinstance(cell, str) and cell.strip() for cell in signature):
        _problem(problems, file, "signature", "must contain only non-empty text cells")

    delimiter = manifest.get("delimiter", ";")
    if not isinstance(delimiter, str) or len(delimiter) != 1:
        _problem(problems, file, "delimiter", "must be exactly one character")

    decimal = manifest.get("decimal", "comma")
    if decimal not in DECIMALS:
        _problem(problems, file, "decimal", "must be %s" % " or ".join(DECIMALS),
                 "reading 1.234,56 the wrong way is off by a factor of a thousand")

    sign = manifest.get("sign", 1)
    if sign not in SIGNS:
        _problem(problems, file, "sign", "must be 1 or -1",
                 "any other value scales every amount in the file")

    date_format = manifest.get("date_format", "%d.%m.%Y")
    if not isinstance(date_format, str) or "%" not in date_format:
        _problem(problems, file, "date_format", "must be a strptime pattern")
    else:
        from datetime import datetime
        try:
            datetime.strptime(datetime(2026, 3, 4).strftime(date_format), date_format)
        except ValueError:
            _problem(problems, file, "date_format", "%r is not a usable date pattern" % date_format)

    columns = manifest.get("columns")
    if not isinstance(columns, dict) or not columns:
        _problem(problems, file, "columns", "must map fields to statement columns")
    else:
        unknown_columns = sorted(set(columns) - COLUMN_KEYS)
        if unknown_columns:
            _problem(problems, file, "columns.%s" % ", ".join(unknown_columns),
                     "is not a field the parser reads",
                     "the column is mapped and then never used")
        for key, value in columns.items():
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                _problem(problems, file, "columns.%s" % key,
                         "must be a column name or a column index")
        if "date" not in columns:
            _problem(problems, file, "columns.date", "is required")
        # Exactly one amount model. Both would leave the parser choosing, and a parser
        # that chooses is a parser nobody can predict.
        has_single = "amount" in columns
        has_pair = "amount_debit" in columns or "amount_credit" in columns
        if has_single and has_pair:
            _problem(problems, file, "columns", "declares both an amount column and "
                     "debit/credit columns; use one model")
        if not has_single and not has_pair:
            _problem(problems, file, "columns", "declares no amount column")

    currency = manifest.get("currency")
    if currency is not None and (not isinstance(currency, str) or not re.match(r"^[A-Z]{3}$", currency)):
        _problem(problems, file, "currency", "must be an ISO 4217 code such as EUR")

    for block, required in (("balance_row", ("match",)), ("statement_total", ("match",)),
                            ("statement_period", ("match",))):
        value = manifest.get(block)
        if value is None:
            continue
        if not isinstance(value, dict):
            _problem(problems, file, block, "must be an object")
            continue
        for field in required:
            if not value.get(field):
                _problem(problems, file, "%s.%s" % (block, field), "is required")
        if block == "statement_total" and not isinstance(value.get("amount_column"), int):
            _problem(problems, file, "statement_total.amount_column",
                     "must be a column index, so the stated total is read from a known column")

    status = manifest.get("verification_status")
    if status is None:
        if require_fixture:
            _problem(problems, file, "verification_status",
                     "is required: say how well this format is known",
                     "one of %s" % ", ".join(VERIFICATION_STATUSES))
    elif status not in VERIFICATION_STATUSES:
        _problem(problems, file, "verification_status",
                 "%r is not one of %s" % (status, ", ".join(VERIFICATION_STATUSES)))
    verified_at = manifest.get("verified_at")
    if verified_at is not None and not ISO_DATE.match(str(verified_at)):
        _problem(problems, file, "verified_at", "must be a YYYY-MM-DD date")
    if require_fixture and not manifest.get("fixture"):
        _problem(problems, file, "fixture",
                 "is required: name the fixture that proves this format reads a statement",
                 "a format nobody has parsed a statement with is a guess")
    return problems


def load_manifests(directory=FORMATS_DIR):
    """Every manifest in the directory, paired with its path. Schemas are not manifests."""
    out = []
    for path in sorted(Path(directory).glob("*.json")):
        if path.name.endswith(".schema.json"):
            continue          # a schema describing manifests is not itself a manifest
        try:
            out.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except ValueError as exc:
            out.append((path, ManifestError(path, "<file>", "is not readable JSON: %s" % exc)))
    return out


def overlapping_signatures(manifests):
    """Pairs of formats where one file could satisfy both signatures.

    Detection compares a row's cells against every signature. If one manifest's cells
    are a subset of another's, any statement matching the larger also matches the
    smaller, and which one wins is decided by iteration order — that is, by filename.
    """
    clashes = []
    entries = [(path, set(m.get("signature") or []), m.get("name"))
               for path, m in manifests if isinstance(m, dict)]
    for index, (path_a, sig_a, name_a) in enumerate(entries):
        for path_b, sig_b, name_b in entries[index + 1:]:
            if not sig_a or not sig_b:
                continue
            if sig_a <= sig_b or sig_b <= sig_a:
                clashes.append((name_a, name_b, sorted(sig_a & sig_b)))
    return clashes


def lint(directory=FORMATS_DIR, *, require_fixture=True):
    """The whole catalogue. Returns a report; raises nothing."""
    manifests = load_manifests(directory)
    problems, names = [], {}
    for path, manifest in manifests:
        if isinstance(manifest, ManifestError):
            problems.append(manifest)
            continue
        problems.extend(lint_manifest(manifest, path, require_fixture=require_fixture))
        name = manifest.get("name")
        if isinstance(name, str):
            if name in names:
                problems.append(ManifestError(path, "name",
                                              "duplicates the format defined in %s"
                                              % Path(names[name]).name))
            names[name] = path
    clashes = overlapping_signatures(manifests)
    for name_a, name_b, shared in clashes:
        problems.append(ManifestError(FORMATS_DIR, "signature",
                                      "%r and %r can both match one file (shared cells: %s)"
                                      % (name_a, name_b, ", ".join(shared)),
                                      "detection would then depend on filename order"))
    return {
        "manifests": len(manifests),
        "problems": [str(problem) for problem in problems],
        "ok": not problems,
        "best_guess": sorted(m.get("name") for _, m in manifests
                             if isinstance(m, dict) and m.get("verification_status") == "best-guess"),
    }


def assert_clean(directory=FORMATS_DIR):
    """Fail loudly at startup rather than loading a half-valid catalogue."""
    report = lint(directory)
    if not report["ok"]:
        raise ManifestError(directory, "catalogue",
                            "%d problem(s):\n  - %s" % (len(report["problems"]),
                                                        "\n  - ".join(report["problems"])))
    return report
