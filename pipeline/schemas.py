"""One authoritative shape for every persisted object.

Validation used to live wherever a reader happened to need it: `load_config` checked
config, `doctor` re-checked a different subset from a different angle, and restore
checked a third. Three approximations of the same truth drift, and the one that
matters is always the one that was not run — a decision file with a NaN amount passes
the loader that never looks at amounts and fails, much later, inside a total.

So this module answers one question per persisted family, and everything that reads or
writes that family asks it. Two design rules follow from what this data is:

  a validator names the exact place. "decisions.json is invalid" costs an afternoon;
  "decisions.json → abc123#1.splits[1].amount is not finite money" costs a minute. Every
  error carries a machine code, the file, a JSON path, a sentence and a fix.

  legacy shapes are adapted, never rewritten on sight. This is somebody's financial
  history sitting in files they own. A reader may understand an older shape and hand
  back the canonical one; nothing here edits the disk to make itself happy.

Structural only. That a split's parts sum to its parent belongs here — it is a property
of the record. That the household's settlement conserves cents does not: it is a
property of a calculation over many records, and it lives in doctor.
"""
import json
import math
import re
from datetime import date
from pathlib import Path

from .util import MAX_YEAR, MIN_YEAR, cents

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
CURRENCY = re.compile(r"^[A-Z]{3}$")

SHARING_VALUES = ("shared", "out-of-scope")          # plus personal:<person>, checked live
KIND_VALUES = ("normal", "internal-transfer")
ACCOUNT_TYPES = ("giro", "credit-card", "cash", "savings", "broker", "other")


class SchemaError(ValueError):
    """A persisted record does not match its schema, and says exactly where.

    Carries a stable `code` so callers and tests can branch on the kind of problem
    without matching on prose, which changes.
    """

    def __init__(self, code, message, *, file=None, path=None, fix=None):
        self.code = code
        self.file = str(file) if file else None
        self.path = path
        self.fix = fix
        detail = " ".join(filter(None, [
            "%s:" % self.file if self.file else None,
            "%s —" % path if path else None,
            message,
            "(%s)" % fix if fix else None,
        ]))
        super().__init__(detail)

    def as_finding(self):
        return {"code": self.code, "file": self.file, "path": self.path,
                "message": str(self), "fix": self.fix}


def _fail(code, message, path, file, fix=None):
    raise SchemaError(code, message, file=file, path=path, fix=fix)


# ---------------------------------------------------------------- primitives

def _require(value, path, file):
    if value is None:
        _fail("missing-field", "is required but absent", path, file)
    return value


def text(value, path, file, *, allow_empty=False, pattern=None, name="value"):
    if not isinstance(value, str):
        _fail("wrong-type", "must be text, got %s" % type(value).__name__, path, file)
    if not allow_empty and not value.strip():
        _fail("empty-field", "must not be empty", path, file)
    if pattern and value and not pattern.match(value):
        _fail("bad-format", "%r is not a valid %s" % (value[:40], name), path, file)
    return value


def number(value, path, file):
    """A finite number, and not a bool.

    `isinstance(True, int)` is True in Python, so a boolean sails through every naive
    numeric check and then behaves as 1 in arithmetic. A `True` where an amount belongs
    is corrupt data, not a one-euro transaction.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("wrong-type", "must be a number, got %s" % type(value).__name__, path, file)
    if not math.isfinite(value):
        _fail("not-finite", "must be a finite number, got %r" % (value,), path, file,
              fix="a NaN or Infinity here poisons every total it reaches")
    return value


def flag(value, path, file):
    if not isinstance(value, bool):
        _fail("wrong-type", "must be true or false, got %s" % type(value).__name__, path, file)
    return value


def iso_date(value, path, file):
    text(value, path, file, pattern=ISO_DATE, name="ISO date (YYYY-MM-DD)")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _fail("bad-format", "%r is not a real calendar date" % (value,), path, file)
    if not MIN_YEAR <= parsed.year <= MAX_YEAR:
        _fail("out-of-range", "year %d is outside %d-%d" % (parsed.year, MIN_YEAR, MAX_YEAR),
              path, file, fix="a date outside this range means the source was mis-read")
    return value


def one_of(value, allowed, path, file):
    if value not in allowed:
        _fail("bad-enum", "%r is not one of %s" % (value, ", ".join(map(str, allowed))), path, file)
    return value


def mapping(value, path, file):
    if not isinstance(value, dict):
        _fail("wrong-type", "must be an object, got %s" % type(value).__name__, path, file)
    return value


def sequence(value, path, file):
    if not isinstance(value, list):
        _fail("wrong-type", "must be a list, got %s" % type(value).__name__, path, file)
    return value


def no_unknown(value, allowed, path, file):
    """Canonical financial records reject fields nobody defined (plan Decision B).

    A misspelled key is silent data loss: `shraing: "out-of-scope"` reads as no sharing
    at all, and the money quietly joins the totals. Envelopes that must stay
    forward-compatible opt out by simply not calling this.
    """
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        _fail("unknown-field", "has unexpected field(s): %s" % ", ".join(unknown), path, file,
              fix="a misspelled field is ignored silently, which is how data goes missing")
    return value


def cents_of(value, path, file):
    # util.cents is the one money conversion today; Phase 2 makes it delegate to
    # pipeline/money.py, and this call site inherits that without changing.
    try:
        return cents(value)
    except (TypeError, ValueError) as exc:
        _fail("not-money", "is not an amount of money: %s" % exc, path, file)


# ---------------------------------------------------------------- records

RAW_TRANSACTION_FIELDS = {
    "id", "account", "date", "amount_original", "currency", "amount_eur", "fx_rate",
    "fx_rate_date", "fx_rate_source", "counterparty", "purpose", "counterparty_iban",
    "force_review", "kind", "source", "transfer_partner", "transfer_reason",
    "possible_transfer", "possible_transfer_reason", "manual_edit", "original",
    "income_owner",
}


def raw_transaction(row, path="", file=None):
    """One canonical imported row — the record every total is ultimately built from."""
    mapping(row, path, file)
    no_unknown(row, RAW_TRANSACTION_FIELDS, path, file)
    text(_require(row.get("id"), path + ".id", file), path + ".id", file)
    text(_require(row.get("account"), path + ".account", file), path + ".account", file)
    iso_date(_require(row.get("date"), path + ".date", file), path + ".date", file)
    number(_require(row.get("amount_eur"), path + ".amount_eur", file), path + ".amount_eur", file)
    number(_require(row.get("amount_original"), path + ".amount_original", file),
           path + ".amount_original", file)
    text(_require(row.get("currency"), path + ".currency", file), path + ".currency", file,
         pattern=CURRENCY, name="ISO 4217 currency code")
    one_of(row.get("kind", "normal"), KIND_VALUES, path + ".kind", file)
    if row.get("fx_rate") is not None:
        rate = number(row["fx_rate"], path + ".fx_rate", file)
        if rate <= 0:
            _fail("out-of-range", "an exchange rate must be positive", path + ".fx_rate", file)
    if row.get("fx_rate_date") is not None:
        iso_date(row["fx_rate_date"], path + ".fx_rate_date", file)
    if not isinstance(row.get("source"), dict):
        _fail("wrong-type", "source metadata must be an object", path + ".source", file,
              fix="a row with no source cannot be traced back to a statement")
    return row


DECISION_FIELDS = {
    "category", "sharing", "income_owner", "tax_bucket", "tax_confirmed", "tax_owner",
    "year_cost", "splits", "note", "receipt", "attachments", "account", "kind",
    "force_review",
}
# "purpose" is a per-part description the split editor writes and splitChildren renders.
# It is real, and running these validators over the real store is how this list found
# out it was incomplete — which is the argument for validating against real data rather
# than against the fixtures the schema was written from.
SPLIT_FIELDS = {"amount", "category", "sharing", "tax_bucket", "year_cost", "note", "purpose"}


def decision(record, path="", file=None, *, people=()):
    """One manual review outcome."""
    mapping(record, path, file)
    no_unknown(record, DECISION_FIELDS, path, file)
    valid_sharing = set(SHARING_VALUES) | {"personal:" + person for person in people}
    if record.get("sharing") is not None and people:
        one_of(record["sharing"], sorted(valid_sharing), path + ".sharing", file)
    if record.get("kind") is not None:
        one_of(record["kind"], KIND_VALUES, path + ".kind", file)
    for name in ("tax_confirmed", "year_cost", "force_review"):
        if record.get(name) is not None:
            flag(record[name], "%s.%s" % (path, name), file)
    splits = record.get("splits")
    if splits is not None:
        sequence(splits, path + ".splits", file)
        for index, part in enumerate(splits):
            where = "%s.splits[%d]" % (path, index)
            mapping(part, where, file)
            no_unknown(part, SPLIT_FIELDS, where, file)
            cents_of(_require(part.get("amount"), where + ".amount", file), where + ".amount", file)
            if part.get("sharing") is not None and people:
                one_of(part["sharing"], sorted(valid_sharing), where + ".sharing", file)
    return record


def decisions_file(document, file=None, *, people=()):
    mapping(document, "", file)
    for txn_id, record in document.items():
        text(txn_id, "[key]", file)
        decision(record, txn_id, file, people=people)
    return document


def split_sum_matches(record, parent_cents, path="", file=None):
    """A split's parts must add up to the transaction they came from.

    This is structural — a property of the record itself — so it belongs here rather
    than in doctor. Parts that do not sum are money invented or destroyed at rest.
    """
    splits = record.get("splits")
    if not splits:
        return record
    total = sum(cents_of(part.get("amount"), "%s.splits" % path, file) for part in splits)
    if total != parent_cents:
        _fail("split-sum", "parts add up to %.2f but the transaction is %.2f"
              % (total / 100.0, parent_cents / 100.0), path + ".splits", file,
              fix="every part of a split must come from the original amount")
    return record


ACCOUNT_FIELDS = {"id", "owner", "bank", "type", "currency", "iban", "label", "low_activity"}


def account(record, path="", file=None, *, people=()):
    mapping(record, path, file)
    no_unknown(record, ACCOUNT_FIELDS, path, file)
    text(_require(record.get("id"), path + ".id", file), path + ".id", file,
         pattern=SLUG, name="account id")
    owner = text(_require(record.get("owner"), path + ".owner", file), path + ".owner", file)
    if people:
        one_of(owner, sorted(set(people) | {"couple"}), path + ".owner", file)
    text(_require(record.get("currency"), path + ".currency", file), path + ".currency", file,
         pattern=CURRENCY, name="ISO 4217 currency code")
    if record.get("type") is not None:
        one_of(record["type"], ACCOUNT_TYPES, path + ".type", file)
    return record


ANCHOR_FIELDS = {"account", "date", "balance", "currency", "kind", "source", "captured_at",
                 "upload"}


def balance_anchor(record, path="", file=None):
    mapping(record, path, file)
    no_unknown(record, ANCHOR_FIELDS, path, file)
    text(_require(record.get("account"), path + ".account", file), path + ".account", file)
    iso_date(_require(record.get("date"), path + ".date", file), path + ".date", file)
    number(_require(record.get("balance"), path + ".balance", file), path + ".balance", file)
    if record.get("currency") is not None:
        text(record["currency"], path + ".currency", file, pattern=CURRENCY,
             name="ISO 4217 currency code")
    return record


RULE_FIELDS = {"id", "match", "category", "sharing", "tax_bucket", "note", "scope", "action"}


def merchant_rule(record, path="", file=None, *, people=()):
    mapping(record, path, file)
    no_unknown(record, RULE_FIELDS, path, file)
    text(_require(record.get("id"), path + ".id", file), path + ".id", file)
    match = mapping(_require(record.get("match"), path + ".match", file), path + ".match", file)
    text(_require(match.get("contains"), path + ".match.contains", file),
         path + ".match.contains", file)
    if match.get("field") is not None:
        one_of(match["field"], ("counterparty", "purpose", "any"), path + ".match.field", file)
    if record.get("action") is not None:
        one_of(record["action"], ("review",), path + ".action", file)
    if record.get("scope") is not None and people:
        one_of(record["scope"], sorted({"family"} | set(people)), path + ".scope", file)
    return record


MONTH_KEY = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
MONTH_STATES = ("open", "closed")


def months_file(document, file=None):
    """`{"2025-01": "closed", ...}` — which periods are locked.

    A month state decides whether a decision is refused, so a key of "2026-13" or a state
    of "banana" is not a cosmetic problem: it is a lock that silently does not lock.
    """
    mapping(document, "", file)
    for key, value in document.items():
        if key == "annual":
            one_of(value, MONTH_STATES, key, file)
            continue
        if not MONTH_KEY.match(str(key)):
            _fail("bad-key", "%r is not a month (YYYY-MM) or 'annual'" % key, key, file,
                  fix="use YYYY-MM with a month between 01 and 12")
        one_of(value, MONTH_STATES, key, file)
    return document


def closings_file(document, file=None):
    """The recorded figures of a closed period. Read by nothing that computes money."""
    mapping(document, "", file)
    for key, record in document.items():
        if key != "annual" and not MONTH_KEY.match(str(key)):
            _fail("bad-key", "%r is not a month (YYYY-MM) or 'annual'" % key, key, file)
        mapping(record, key, file)
        for field in ("income", "expenses"):
            if field in record:
                number(record[field], "%s.%s" % (key, field), file)
        if "transactions" in record and not isinstance(record["transactions"], int):
            _fail("bad-type", "must be a whole number of transactions",
                  "%s.transactions" % key, file)
        if record.get("closed_at") is not None:
            text(record["closed_at"], "%s.closed_at" % key, file)
        acceptance = record.get("acceptance")
        if acceptance is not None:
            mapping(acceptance, "%s.acceptance" % key, file)
            for field in ("accepted_at", "reason", "classification"):
                text(_require(acceptance.get(field), "%s.acceptance.%s" % (key, field), file),
                     "%s.acceptance.%s" % (key, field), file)
            one_of(acceptance["classification"], ("minor", "material"),
                   "%s.acceptance.classification" % key, file)
            if acceptance.get("previous_closed_at") is not None:
                text(acceptance["previous_closed_at"],
                     "%s.acceptance.previous_closed_at" % key, file)
            delta = acceptance.get("max_delta_cents")
            if delta is not None and (isinstance(delta, bool) or not isinstance(delta, int)
                                      or delta < 0):
                _fail("wrong-type", "must be a non-negative whole number of cents",
                      "%s.acceptance.max_delta_cents" % key, file)
            changes = acceptance.get("changes", [])
            sequence(changes, "%s.acceptance.changes" % key, file)
            for index, change in enumerate(changes):
                text(change, "%s.acceptance.changes[%d]" % (key, index), file)
        if record.get("digest") is not None:
            text(record["digest"], "%s.digest" % key, file)
        if record.get("digest_version") is not None and \
                (isinstance(record["digest_version"], bool)
                 or not isinstance(record["digest_version"], int)):
            _fail("wrong-type", "must be a whole-number schema version",
                  "%s.digest_version" % key, file)
        settlement = record.get("settlement")
        if settlement is not None:
            mapping(settlement, "%s.settlement" % key, file)
            for field in ("total_shared_expenses",):
                if settlement.get(field) is not None:
                    number(settlement[field], "%s.settlement.%s" % (key, field), file)
            for field in ("ratio", "paid", "balances", "fair_share"):
                values = settlement.get(field)
                if values is not None:
                    mapping(values, "%s.settlement.%s" % (key, field), file)
                    for person, value in values.items():
                        number(value, "%s.settlement.%s.%s" % (key, field, person), file)
    return document


def uploads_file(document, file=None):
    """`{"uploads": [...]}` — the statements that were imported, and their hashes."""
    mapping(document, "", file)
    uploads = document.get("uploads", [])
    sequence(uploads, "uploads", file)
    for index, record in enumerate(uploads):
        where = "uploads[%d]" % index
        mapping(record, where, file)
        text(record.get("id"), where + ".id", file)
        for field in ("original_name", "account", "format", "file_hash"):
            if record.get(field) is not None:
                text(record[field], "%s.%s" % (where, field), file, allow_empty=True)
        years = record.get("years")
        if years is not None:
            sequence(years, where + ".years", file)
            for position, year in enumerate(years):
                if not isinstance(year, int):
                    _fail("bad-type", "must be a year as a number",
                          "%s.years[%d]" % (where, position), file)
    return document


def categories_file(document, file=None):
    """The category tree. Slugs are stable ids, so a duplicate one is a silent merge."""
    mapping(document, "", file)
    groups = document.get("categories")
    sequence(groups, "categories", file)
    seen = set()
    for index, group in enumerate(groups):
        where = "categories[%d]" % index
        mapping(group, where, file)
        slug = text(group.get("slug"), where + ".slug", file, pattern=SLUG)
        text(group.get("name"), where + ".name", file)
        if slug in seen:
            _fail("duplicate", "slug %r is used twice; slugs are stable ids" % slug,
                  where + ".slug", file, fix="rename one of them")
        seen.add(slug)
        subs = group.get("subs") or []
        sequence(subs, where + ".subs", file)
        sub_slugs = set()
        for position, sub in enumerate(subs):
            spot = "%s.subs[%d]" % (where, position)
            mapping(sub, spot, file)
            sub_slug = text(sub.get("slug"), spot + ".slug", file, pattern=SLUG)
            text(sub.get("name"), spot + ".name", file)
            if sub_slug in sub_slugs:
                _fail("duplicate", "slug %r is used twice inside %s" % (sub_slug, slug),
                      spot + ".slug", file)
            sub_slugs.add(sub_slug)
    return document


def tax_buckets_file(document, file=None):
    """`{"buckets": [...]}` — the tax categories a transaction can be filed under."""
    mapping(document, "", file)
    buckets = document.get("buckets")
    sequence(buckets, "buckets", file)
    seen = set()
    for index, record in enumerate(buckets):
        where = "buckets[%d]" % index
        mapping(record, where, file)
        slug = text(record.get("slug"), where + ".slug", file, pattern=SLUG)
        text(record.get("name"), where + ".name", file)
        if slug in seen:
            _fail("duplicate", "bucket slug %r is used twice" % slug, where + ".slug", file)
        seen.add(slug)
    return document


def ratio_overrides_file(document, file=None, *, people=()):
    """Manual settlement ratios. A ratio outside 0–1 moves money for no stated reason."""
    mapping(document, "", file)
    for key, record in document.items():
        if key != "annual" and not str(key).isdigit():
            _fail("bad-key", "%r is not a month number or 'annual'" % key, key, file)
        mapping(record, key, file)
        for person, share in record.items():
            if people and person not in people:
                _fail("dangling-reference", "%r is not one of the household" % person,
                      "%s.%s" % (key, person), file)
            value = number(share, "%s.%s" % (key, person), file)
            if not 0 <= value <= 1:
                _fail("out-of-range", "a ratio is a fraction between 0 and 1, not %r" % share,
                      "%s.%s" % (key, person), file,
                      fix="use 0.6 for 60%, not 60")
    return document


def metadata_map_file(document, file=None):
    """Dismissals and checkpoints: metadata about suggestions, never about money.

    Held to a shape and nothing more. These files describe the ledger and are never read
    to compute a figure, so an odd value here cannot move a total — but a file that is not
    even a mapping would still crash the page that reads it.
    """
    mapping(document, "", file)
    return document


def balance_anchors_file(document, file=None):
    sequence(document, "", file)
    for index, record in enumerate(document):
        balance_anchor(record, "[%d]" % index, file)
    return document


def budgets_file(document, file=None):
    """Monthly targets by category. A string here crashes every budget consumer."""
    mapping(document, "", file)
    budgets = document.get("budgets", {})
    mapping(budgets, "budgets", file)
    for category, amount in budgets.items():
        text(category, "budgets.[key]", file)
        value = number(amount, "budgets.%s" % category, file)
        if value < 0:
            _fail("out-of-range", "a monthly target cannot be negative",
                  "budgets.%s" % category, file)
    return document


def recurring_overrides_file(document, file=None):
    mapping(document, "", file)
    values = {}
    for field in ("force", "never"):
        rows = document.get(field, [])
        sequence(rows, field, file)
        values[field] = []
        for index, merchant in enumerate(rows):
            values[field].append(text(merchant, "%s[%d]" % (field, index), file))
        if len(values[field]) != len(set(values[field])):
            _fail("duplicate", "%s contains the same merchant more than once" % field,
                  field, file)
    overlap = sorted(set(values.get("force", ())) & set(values.get("never", ())))
    if overlap:
        _fail("contradiction", "merchant %r is both forced and forbidden" % overlap[0],
              "force", file)
    return document


def anomaly_dismissals_file(document, file=None):
    mapping(document, "", file)
    version = document.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        _fail("wrong-type", "version must be a whole number", "version", file)
    dismissed = document.get("dismissed", {})
    mapping(dismissed, "dismissed", file)
    for anomaly_id, record in dismissed.items():
        text(anomaly_id, "dismissed.[key]", file)
        mapping(record, "dismissed.%s" % anomaly_id, file)
        text(_require(record.get("fingerprint"), "dismissed.%s.fingerprint" % anomaly_id, file),
             "dismissed.%s.fingerprint" % anomaly_id, file)
        text(_require(record.get("dismissed_at"), "dismissed.%s.dismissed_at" % anomaly_id, file),
             "dismissed.%s.dismissed_at" % anomaly_id, file)
    return document


def audit_checkpoints_file(document, file=None):
    mapping(document, "", file)
    version = document.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        _fail("wrong-type", "version must be a whole number", "version", file)
    checkpoints = document.get("checkpoints", {})
    mapping(checkpoints, "checkpoints", file)
    for slot, record in checkpoints.items():
        where = "checkpoints.%s" % slot
        text(slot, "checkpoints.[key]", file)
        mapping(record, where, file)
        for field in ("id", "kind", "created_at", "label"):
            text(_require(record.get(field), "%s.%s" % (where, field), file),
                 "%s.%s" % (where, field), file)
        mapping(_require(record.get("metadata"), where + ".metadata", file),
                where + ".metadata", file)
        snapshot = mapping(_require(record.get("snapshot"), where + ".snapshot", file),
                           where + ".snapshot", file)
        mapping(_require(snapshot.get("lines"), where + ".snapshot.lines", file),
                where + ".snapshot.lines", file)
        mapping(_require(snapshot.get("figures"), where + ".snapshot.figures", file),
                where + ".snapshot.figures", file)
        mapping(_require(snapshot.get("settlement"), where + ".snapshot.settlement", file),
                where + ".snapshot.settlement", file)
    return document


# ---------------------------------------------------------------- files

def load_and_validate(path, validator, **kwargs):
    """Read a JSON file and hand back the canonical value, or say exactly what is wrong."""
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        raise SchemaError("missing-file", "does not exist", file=path)
    except ValueError as exc:
        raise SchemaError("unreadable", "is not readable JSON: %s" % exc, file=path,
                          fix="restore it from a backup, or repair the syntax")
    return validator(document, file=path, **kwargs)


def validate_for_write(value, validator, *, file=None, **kwargs):
    """Check a value on the way OUT, before it replaces a good file.

    Validating only on read means the corruption is already on disk by the time anyone
    notices, and the good copy it replaced is gone.
    """
    return validator(value, file=file, **kwargs)


def findings_for(path, validator, **kwargs):
    """Validate without raising — for the doctor, which must report and keep going."""
    try:
        load_and_validate(path, validator, **kwargs)
        return []
    except SchemaError as exc:
        return [exc.as_finding()]


# ---------------------------------------------------------------- the whole tree

def validate_graph(root, *, selected_parts=None):
    """Every persisted file under one root, validated together.

    Takes an explicit root rather than reading the module-level DATA, because its most
    important caller is restore: a candidate tree has to be judged complete and
    self-consistent *before* it is allowed anywhere near the live one, and a validator
    that can only look at the live tree cannot do that.

    Failures are isolated per file. One unreadable year must not make the other years
    vanish from the report — the moment an audit stops early is the moment it stops
    being an audit.
    """
    root = Path(root)
    findings = []
    people = []

    def add(exc):
        findings.append(exc.as_finding())

    config_path = root / "config.json"
    if config_path.exists():
        try:
            document = load_and_validate(config_path, lambda d, file=None: mapping(d, "", file))
            people = [p for p in (document.get("people") or []) if isinstance(p, str)]
        except SchemaError as exc:
            add(exc)

    accounts_path = root / "data" / "accounts.json"
    known_accounts = set()
    if accounts_path.exists():
        try:
            document = load_and_validate(accounts_path, lambda d, file=None: mapping(d, "", file))
            for index, record in enumerate(document.get("accounts") or []):
                try:
                    account(record, "accounts[%d]" % index, accounts_path, people=people)
                    known_accounts.add(record.get("id"))
                except SchemaError as exc:
                    add(exc)
        except SchemaError as exc:
            add(exc)

    categories = set()
    categories_path = root / "rules" / "categories.json"
    if categories_path.exists():
        try:
            document = load_and_validate(categories_path, categories_file)
            for group in document.get("categories") or []:
                for sub in group.get("subs") or []:
                    categories.add("%s/%s" % (group.get("slug"), sub.get("slug")))
        except SchemaError as exc:
            add(exc)

    rules_path = root / "rules" / "merchant-rules.json"
    if rules_path.exists():
        try:
            document = load_and_validate(rules_path, lambda d, file=None: mapping(d, "", file))
            for index, record in enumerate(document.get("rules") or []):
                try:
                    merchant_rule(record, "rules[%d]" % index, rules_path, people=people)
                except SchemaError as exc:
                    add(exc)
        except SchemaError as exc:
            add(exc)

    data = root / "data"
    for year_dir in sorted(p for p in data.glob("*") if p.is_dir() and p.name.isdigit()) \
            if data.exists() else []:
        amounts = {}
        for jsonl in sorted((year_dir / "transactions").glob("*.jsonl")):
            try:
                with open(jsonl, encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if not line.strip():
                            continue
                        try:
                            row = json.loads(line)
                        except ValueError as exc:
                            add(SchemaError("unreadable", "line %d is not JSON: %s"
                                            % (line_number, exc), file=jsonl))
                            continue
                        try:
                            raw_transaction(row, "line %d" % line_number, jsonl)
                            amounts[row["id"]] = cents_of(row["amount_eur"],
                                                          "line %d" % line_number, jsonl)
                        except SchemaError as exc:
                            add(exc)
            except OSError as exc:
                add(SchemaError("unreadable", str(exc), file=jsonl))

        decisions_path = year_dir / "decisions.json"
        if decisions_path.exists():
            try:
                document = load_and_validate(decisions_path,
                                             lambda d, file=None: mapping(d, "", file))
                for txn_id, record in document.items():
                    try:
                        decision(record, txn_id, decisions_path, people=people)
                        if txn_id in amounts:
                            split_sum_matches(record, amounts[txn_id], txn_id, decisions_path)
                        else:
                            add(SchemaError("dangling-reference",
                                            "decides a transaction that does not exist in %s"
                                            % year_dir.name, file=decisions_path, path=txn_id,
                                            fix="delete the decision, or restore the transaction"))
                        chosen = record.get("category")
                        if chosen and categories and chosen not in categories | {"auto:items"}:
                            add(SchemaError("dangling-reference",
                                            "references category %r, which does not exist" % chosen,
                                            file=decisions_path, path=txn_id + ".category"))
                        moved = record.get("account")
                        if moved and known_accounts and moved not in known_accounts:
                            add(SchemaError("dangling-reference",
                                            "reassigns to account %r, which does not exist" % moved,
                                            file=decisions_path, path=txn_id + ".account"))
                    except SchemaError as exc:
                        add(exc)
            except SchemaError as exc:
                add(exc)

        anchors_path = year_dir / "balance-anchors.json"
        if anchors_path.exists():
            try:
                rows = load_and_validate(anchors_path, lambda d, file=None: sequence(d, "", file))
                for index, record in enumerate(rows):
                    try:
                        balance_anchor(record, "[%d]" % index, anchors_path)
                        if known_accounts and record.get("account") not in known_accounts:
                            add(SchemaError("dangling-reference",
                                            "anchors an account that does not exist",
                                            file=anchors_path, path="[%d].account" % index))
                    except SchemaError as exc:
                        add(exc)
            except SchemaError as exc:
                add(exc)

        # The rest of the year's documents. Each was previously unvalidated, which meant a
        # restore could stage `{"2026-13": "banana"}` as the month lock and be told the
        # candidate was fine — the gate is only as wide as its inventory.
        for name, validator in (("months.json", months_file),
                                ("closings.json", closings_file),
                                ("audit-checkpoints.json", audit_checkpoints_file)):
            path = year_dir / name
            if path.exists():
                try:
                    load_and_validate(path, validator)
                except SchemaError as exc:
                    add(exc)
        overrides_path = year_dir / "ratio-overrides.json"
        if overrides_path.exists():
            try:
                load_and_validate(overrides_path, ratio_overrides_file, people=people)
            except SchemaError as exc:
                add(exc)

    for path, validator in ((root / "data" / "uploads.json", uploads_file),
                            (root / "data" / "anomaly-dismissals.json", anomaly_dismissals_file),
                            (root / "rules" / "tax-buckets.json", tax_buckets_file),
                            (root / "rules" / "recurring-overrides.json", recurring_overrides_file),
                            (root / "rules" / "budgets.json", budgets_file)):
        if path.exists():
            try:
                load_and_validate(path, validator)
            except SchemaError as exc:
                add(exc)

    return {"root": str(root), "findings": findings, "ok": not findings,
            "checked_parts": sorted(selected_parts) if selected_parts else None}
