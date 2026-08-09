"""Transactions worth a second look — suggestions, never verdicts.

This is a review assistant, not a fraud detector, and the difference decides every
design choice here. It says `Possible duplicate`, not `Duplicate`; `Unusual amount`,
not `Wrong amount`; `Expected charge not seen`, not `Missing payment`. It cannot know
that a transaction is wrong. It can only notice that one looks unlike its neighbours
and hand the judgement to a person.

Three rules it never breaks:

  it writes nothing to the ledger. No categorization, no merge, no delete, no transfer
  mark. The only thing it persists is a dismissal, which is metadata about a suggestion
  and never about money;

  its output is never an input. No total, settlement, tax figure or net-worth point is
  computed from anything in this module;

  it is deterministic. Same store, same `as_of`, same suggestions in the same order —
  time-sensitive checks take `as_of` explicitly so a test never depends on the clock.

False positives are the real risk. A financial tool that calls ordinary purchases
suspicious gets ignored within a week, and an ignored suggestion list is worse than no
list because it also hides the real ones. So every threshold below is deliberately
conservative, every minimum sample size is stated, and the plan's Decision 1 default
holds: only high-confidence suggestions are shown unless the user asks for more.
"""
import hashlib
import json
import statistics
from collections import defaultdict
from datetime import date, timedelta

from . import coverage, recurring, schemas, settle, store
from .util import DATA, cents, load_accounts, read_json, write_json

DISMISSALS_PATH = DATA / "anomaly-dismissals.json"

# Bumping this retires every stored dismissal, because a changed detector no longer
# means the same thing by the same finding.
DETECTOR_VERSION = 1

# Thresholds, all in one place so the false-positive controls can be read at a glance
# rather than hunted for. Every one of them is a floor, not a tuning knob.
NEAR_DUPLICATE_DAYS = 3          # beyond this, two equal charges are just two charges
SPIKE_MIN_HISTORY = 6            # fewer charges than this cannot establish "normal"
SPIKE_MAD_MULTIPLE = 4           # how many median-absolute-deviations is unusual
SPIKE_MIN_RATIO = 0.5            # ... and at least 50% away from the median
SPIKE_MIN_CENTS = 2000           # ... and at least 20 EUR away, so small sums are quiet
SIGN_MIN_HISTORY = 5             # a merchant needs a habit before breaking it is notable
NEW_CURRENCY_MIN_HISTORY = 20    # an account needs a history before a currency is "new"
RECURRING_MIN_MONTHS = 4         # the recurring detector's own evidence, plus:
RECURRING_GRACE_DAYS = 7         # ... a week past the expected window before asking


def stable_id(check, transaction_ids, discriminator=""):
    """A suggestion's identity: what it is about, not what it says.

    Display text, category names and timestamps are deliberately excluded. A dismissal
    has to survive rewording the message or recategorizing the transaction — if the id
    moved for either, every dismissal would silently come undone.
    """
    payload = "|".join([check, ",".join(sorted(transaction_ids)), discriminator,
                        str(DETECTOR_VERSION)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def fingerprint(evidence):
    """What the suggestion was *based on*, so a dismissal expires when that changes.

    Dismissing "these two 12.99 charges on the 3rd are fine" should not also dismiss a
    later state where one of them became 129.90. The id says which finding; this says
    which facts, and both must match for a dismissal to hold.
    """
    return hashlib.sha256(
        json.dumps(evidence, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- persistence

def load_dismissals():
    stored = read_json(DISMISSALS_PATH, default={"version": 1, "dismissed": {}})
    dismissed = stored.get("dismissed")
    return dismissed if isinstance(dismissed, dict) else {}


def dismiss(anomaly_id, evidence_fingerprint, when=None):
    """Hide one suggestion while its evidence stays as it is."""
    stored = read_json(DISMISSALS_PATH, default={"version": 1, "dismissed": {}})
    dismissed = stored.get("dismissed")
    if not isinstance(dismissed, dict):
        dismissed = {}
    dismissed[anomaly_id] = {
        "fingerprint": evidence_fingerprint,
        "dismissed_at": (when or _utc_now()),
    }
    document = {"version": 1, "dismissed": dismissed}
    schemas.validate_for_write(document, schemas.anomaly_dismissals_file,
                               file=DISMISSALS_PATH)
    write_json(DISMISSALS_PATH, document)
    return dismissed[anomaly_id]


def _utc_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- context

def _context(year, scope, as_of):
    """Everything every check needs, read once.

    Rereading the store per check would be seven passes over the same year and, worse,
    seven chances for two checks to disagree about what the data says.
    """
    accounts, _ = load_accounts()
    effective = store.effective_year(year)
    income_cats = settle.income_categories()

    # Charges only, and only the money that counts. An out-of-scope line is invisible
    # to every total, so calling it an unusual expense would be a suggestion about
    # money the app has already been told to ignore. A transfer is not spending.
    charges = []
    for txn in effective:
        if txn.get("kind") == "internal-transfer":
            continue
        countable = 0.0
        for _, part in settle.money_lines(txn):
            view = settle.part_view(txn, part)
            if view["sharing"] == "out-of-scope":
                continue
            if scope != "all" and not settle.in_scope(
                    dict(view, txn=txn, amount=part["amount"] if part else txn["amount_eur"]),
                    scope, income_cats, accounts):
                continue
            countable += part["amount"] if part else txn["amount_eur"]
        if countable == 0:
            continue
        # A split is one charge from one merchant, carrying the amount that counts —
        # the same rule recurring uses, so the two agree on what a charge is worth.
        charges.append(dict(txn, countable_cents=cents(countable),
                            merchant=recurring.merchant_key(txn)))

    return {
        "year": int(year), "scope": scope, "as_of": as_of,
        "accounts": accounts,
        "effective": effective,
        "charges": charges,
        "spend": [c for c in charges if c["countable_cents"] < 0],
        "by_id": {t["id"]: t for t in effective},
        "uploads": read_json(DATA / "uploads.json", default={"uploads": []}).get("uploads", []),
    }


def _suggestion(check, ids, message, evidence, confidence="high", impact_cents=0,
                discriminator=""):
    return {
        "id": stable_id(check, ids, discriminator),
        "check": check,
        "severity": "suggestion",
        "confidence": confidence,
        "transaction_ids": sorted(ids),
        "message": message,
        "evidence": evidence,
        "math_impact_cents": impact_cents,
        "fingerprint": fingerprint(evidence),
        "dismissed": False,
    }


# ---------------------------------------------------------------- the checks

def exact_duplicates(ctx):
    """The same charge, booked twice.

    Import already drops byte-identical rows by content hash, so anything reaching
    here differed enough to survive that — a re-issued statement with a new reference,
    or the same purchase genuinely charged twice. Which of those it is, is exactly the
    judgement being handed to a person: the rows are never collapsed.
    """
    out = []
    groups = defaultdict(list)
    for charge in ctx["spend"]:
        if not charge["merchant"]:
            continue
        # Grouped on the RAW counterparty text, not the normalized merchant key. The key
        # strips digits so a merchant's charges group across time, which is right for
        # spikes and cadence and wrong here: on a Brazilian card statement "Dell -
        # Parcela 10/12" and "Parcela 11/12" are different instalments of one purchase,
        # and normalizing them together reported ten separate instalments as ten
        # identical charges — with high confidence. Two rows that really are the same
        # charge twice carry identical text, so raw is both correct and stricter.
        groups[(charge["account"], charge["date"], charge.get("currency"),
                cents(charge.get("amount_original") or 0),
                (charge.get("counterparty") or "").strip(),
                (charge.get("purpose") or "").strip())].append(charge)
    for key, rows in sorted(groups.items()):
        if len(rows) < 2:
            continue
        ids = [row["id"] for row in rows]
        evidence = {
            "date": key[1], "account": key[0], "currency": key[2],
            "original_cents": key[3], "merchant": rows[0].get("counterparty") or key[4],
            "count": len(rows),
            "sources": sorted({(row.get("source") or {}).get("file") or "" for row in rows}),
            "uploads": sorted({(row.get("source") or {}).get("upload") or "" for row in rows}),
        }
        out.append(_suggestion(
            "exact-duplicate", ids,
            "Possible duplicate: %d identical charges from %s on %s."
            % (len(rows), evidence["merchant"], key[1]),
            evidence, confidence="high",
            impact_cents=sum(row["countable_cents"] for row in rows[1:])))
    return out


def near_duplicates(ctx):
    """The same amount from the same merchant, a day or two apart."""
    out = []
    seen_exact = {tuple(sorted(item["transaction_ids"])) for item in exact_duplicates(ctx)}
    groups = defaultdict(list)
    for charge in ctx["spend"]:
        if not charge["merchant"]:
            continue
        # Raw text again, for the same reason as above: an instalment number inside the
        # counterparty makes two unrelated charges look like one merchant.
        groups[(charge["account"], (charge.get("counterparty") or "").strip(),
                (charge.get("purpose") or "").strip(),
                cents(charge.get("amount_original") or 0))].append(charge)
    for key, rows in sorted(groups.items()):
        rows.sort(key=lambda row: (row["date"], row["id"]))
        for first, second in zip(rows, rows[1:]):
            if first["date"] == second["date"]:
                continue        # exact_duplicates owns the same-day case
            gap = (date.fromisoformat(second["date"]) - date.fromisoformat(first["date"])).days
            if gap > NEAR_DUPLICATE_DAYS:
                continue
            ids = [first["id"], second["id"]]
            if tuple(sorted(ids)) in seen_exact:
                continue
            evidence = {"dates": [first["date"], second["date"]], "days_apart": gap,
                        # key is (account, counterparty, purpose, cents) — key[2] is the
                        # purpose text, which was being reported as the amount.
                        "account": key[0], "original_cents": key[3],
                        "merchant": first.get("counterparty") or key[1]}
            out.append(_suggestion(
                "near-duplicate", ids,
                "Possible duplicate: %s charged the same amount twice, %d day(s) apart."
                % (evidence["merchant"], gap),
                evidence, confidence="medium",
                impact_cents=second["countable_cents"]))
    return out


def unexpected_signs(ctx):
    """A merchant that has only ever taken money just gave some back (or the reverse).

    Refunds are ordinary and legitimate, which is why this is medium confidence and
    worded as an observation about the merchant's habit rather than a problem.
    """
    out = []
    groups = defaultdict(list)
    for charge in ctx["charges"]:
        if charge["merchant"]:
            groups[charge["merchant"]].append(charge)
    for merchant, rows in sorted(groups.items()):
        rows.sort(key=lambda row: (row["date"], row["id"]))
        for index, row in enumerate(rows):
            history = rows[:index]
            if len(history) < SIGN_MIN_HISTORY:
                continue
            signs = {1 if item["countable_cents"] > 0 else -1 for item in history}
            if len(signs) != 1:
                continue
            usual = signs.pop()
            if (1 if row["countable_cents"] > 0 else -1) == usual:
                continue
            evidence = {"merchant": row.get("counterparty") or merchant,
                        "date": row["date"], "prior_occurrences": len(history),
                        "usual_sign": "expense" if usual < 0 else "income",
                        "amount_cents": row["countable_cents"]}
            out.append(_suggestion(
                "unexpected-sign", [row["id"]],
                "%s is normally an %s; this entry is the other way round."
                % (evidence["merchant"], evidence["usual_sign"]),
                evidence, confidence="medium", impact_cents=row["countable_cents"],
                discriminator=row["date"]))
    return out


def amount_spikes(ctx):
    """A charge unlike the ones this merchant usually makes.

    Median and median-absolute-deviation, never mean and standard deviation: one
    genuine outlier drags a mean far enough to hide the next one, which is the failure
    mode a spike detector cannot afford. Three floors must all be cleared, so a
    merchant whose charges are identical (MAD zero) does not flag on a few cents.
    """
    out = []
    groups = defaultdict(list)
    for charge in ctx["spend"]:
        if charge["merchant"]:
            groups[charge["merchant"]].append(charge)
    for merchant, rows in sorted(groups.items()):
        rows.sort(key=lambda row: (row["date"], row["id"]))
        for index, row in enumerate(rows):
            history = [abs(item["countable_cents"]) for item in rows[:index]]
            if len(history) < SPIKE_MIN_HISTORY:
                continue
            median = statistics.median(history)
            mad = statistics.median([abs(value - median) for value in history])
            value = abs(row["countable_cents"])
            distance = abs(value - median)
            threshold = max(SPIKE_MAD_MULTIPLE * mad, SPIKE_MIN_RATIO * median, SPIKE_MIN_CENTS)
            if distance <= threshold:
                continue
            evidence = {"merchant": row.get("counterparty") or merchant, "date": row["date"],
                        "amount_cents": value, "median_cents": int(median),
                        "mad_cents": int(mad), "sample_size": len(history),
                        "normal_low_cents": int(max(0, median - threshold)),
                        "normal_high_cents": int(median + threshold)}
            out.append(_suggestion(
                "amount-spike", [row["id"]],
                "Unusual amount: %s is normally around %.2f, this one is %.2f."
                % (evidence["merchant"], median / 100.0, value / 100.0),
                evidence, confidence="high" if distance > 2 * threshold else "medium",
                impact_cents=row["countable_cents"], discriminator=row["date"]))
    return out


def new_account_currency(ctx):
    """The first time an account is used in a currency it has never seen.

    Only the *first* one: a multi-currency card that warns on every later transaction
    is a card you stop reading warnings about.
    """
    out = []
    per_account = defaultdict(list)
    for charge in ctx["charges"]:
        per_account[charge["account"]].append(charge)
    for account_id, rows in sorted(per_account.items()):
        declared = (ctx["accounts"].get(account_id, {}).get("currency") or "EUR").upper()
        rows.sort(key=lambda row: (row["date"], row["id"]))
        seen = set()
        for index, row in enumerate(rows):
            currency = (row.get("currency") or "EUR").upper()
            if currency == declared or currency in seen:
                continue
            seen.add(currency)
            if index < NEW_CURRENCY_MIN_HISTORY:
                continue
            evidence = {"account": account_id, "declared_currency": declared,
                        "currency": currency, "date": row["date"],
                        "prior_transactions": index}
            out.append(_suggestion(
                "new-account-currency", [row["id"]],
                "First %s transaction on an account that has only used %s."
                % (currency, declared),
                evidence, confidence="medium", impact_cents=row["countable_cents"]))
    return out


def missing_recurring(ctx):
    """A charge that normally arrives every month, and this month did not.

    Gated three ways, because "you did not pay something" is the most alarming thing
    this module can say and the easiest to say wrongly: the recurring detector must
    already have four months of history, the month in question must be over, and the
    statement coverage must show the account actually has data for it. A month nobody
    has imported yet is not a month with a missing payment.
    """
    out = []
    as_of = ctx["as_of"]
    year = ctx["year"]
    detected = recurring.detect(year, scope=ctx["scope"])
    covered = coverage.coverage(year, today=as_of)
    per_account_months = {row["id"]: row["months"] for row in covered.get("accounts", [])}

    for item in detected.get("items", []):
        if item.get("cadence") != "monthly" or len(item.get("months_seen") or []) < RECURRING_MIN_MONTHS:
            continue
        last_seen = item.get("last_date")
        if not last_seen:
            continue
        expected_by = date.fromisoformat(last_seen) + timedelta(
            days=int(round(item.get("average_gap_days") or 30)) + RECURRING_GRACE_DAYS)
        if expected_by > as_of:
            continue        # not late yet
        expected_month = expected_by.month
        if expected_month > 12 or expected_by.year != year:
            continue
        # Every account this merchant has ever charged must have statement data for
        # the month in question; otherwise the charge may simply not be imported.
        gap = [account for account in (item.get("accounts") or [])
               if not (per_account_months.get(account) or [0] * 12)[expected_month - 1]]
        if gap:
            continue
        evidence = {"merchant": item.get("merchant"), "key": item.get("key"),
                    "last_seen": last_seen, "cadence": item.get("cadence"),
                    "average_gap_days": item.get("average_gap_days"),
                    "median_cents": cents(item.get("median_amount") or 0),
                    "expected_by": expected_by.isoformat(),
                    "months_seen": len(item.get("months_seen") or []),
                    "accounts_checked": sorted(item.get("accounts") or [])}
        out.append(_suggestion(
            "missing-recurring", [],
            "Expected recurring charge not seen: %s normally arrives about every %d days, "
            "last seen %s." % (evidence["merchant"],
                               int(round(item.get("average_gap_days") or 30)), last_seen),
            evidence, confidence="medium",
            impact_cents=-abs(evidence["median_cents"]),
            discriminator="%s:%s" % (item.get("key"), expected_by.isoformat())))
    return out


def outside_statement_period(ctx):
    """A row dated outside the period its own statement says it covers.

    Only runs where the upload declared a machine-readable period. Inferring the
    period from the rows and then accusing those rows of being outside it would be a
    check that can only ever agree with itself.
    """
    out = []
    periods = {}
    for upload in ctx["uploads"]:
        period = upload.get("period_range") or {}
        start, end = period.get("start"), period.get("end")
        if upload.get("source_stem") and start and end:
            periods[upload["source_stem"]] = (start, end, upload.get("original_name"))
    for charge in ctx["charges"]:
        stem = (charge.get("source") or {}).get("upload")
        window = periods.get(stem)
        if not window:
            continue
        start, end, label = window
        if start <= charge["date"] <= end:
            continue
        evidence = {"date": charge["date"], "period_start": start, "period_end": end,
                    "statement": label or stem}
        out.append(_suggestion(
            "outside-statement-period", [charge["id"]],
            "Dated %s, but its statement covers %s to %s." % (charge["date"], start, end),
            evidence, confidence="high", impact_cents=charge["countable_cents"]))
    return out


CHECKS = (exact_duplicates, near_duplicates, amount_spikes, missing_recurring,
          unexpected_signs, new_account_currency, outside_statement_period)


def scan(year, scope="all", as_of=None, include_dismissed=False):
    """Every suggestion for one year, in a stable order.

    `as_of` is explicit so a time-sensitive check ("is this month over?") is
    reproducible — a detector whose answer depends on the wall clock cannot be tested
    and cannot be trusted to say the same thing twice.
    """
    as_of = as_of or date.today()
    if isinstance(as_of, str):
        as_of = date.fromisoformat(as_of)
    ctx = _context(year, scope, as_of)
    found = [item for check in CHECKS for item in check(ctx)]

    dismissals = load_dismissals()
    for item in found:
        record = dismissals.get(item["id"])
        item["dismissed"] = bool(record and record.get("fingerprint") == item["fingerprint"])
    if not include_dismissed:
        found = [item for item in found if not item["dismissed"]]
    found.sort(key=lambda item: (item["check"], item["evidence"].get("date") or "",
                                 item["id"]))
    counts = defaultdict(int)
    for item in found:
        counts[item["confidence"]] += 1
    return {
        "year": int(year), "scope": scope, "as_of": as_of.isoformat(),
        "items": found,
        "counts": {"total": len(found), "high": counts["high"], "medium": counts["medium"],
                   "low": counts["low"]},
        "detector_version": DETECTOR_VERSION,
    }
