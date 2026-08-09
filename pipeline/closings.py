"""What a month's figures were when it was closed.

Closing a month is meant to settle it. The lock that enforces this only rejects
*decisions*, and decisions are one of at least three things that move a closed
month's numbers: a merchant rule sets category and sharing for every transaction it
matches, transfer detection adds and releases exclusions, and re-ingesting a
statement can change the rows themselves. None of those ask the lock for permission,
and none of them leave a trace.

So the figures are recorded at the moment of closing and compared afterwards. This
does not prevent a closed month from changing — blocking that would mean refusing
corrections, including the release of an exclusion already known to be wrong. It
makes the change *visible*, which is what the lock was always trying to buy.

A snapshot is evidence, never a source of truth: nothing reads these numbers to
compute anything. They are only ever compared against a fresh recomputation.
"""
import hashlib
from datetime import datetime, timezone

from . import audit, schemas, settle, store
from .util import cents, read_json, write_json, year_dir

# The figures a person recognises. They are what the drift report quotes, but they are
# not what it relies on: income, expenses and a count can all sit still while the
# amount one person owes the other moves, because settlement depends on sharing, on
# which account paid, on who owns the income and on the ratio — none of which is a
# total. A snapshot of totals alone reported no drift while a settlement moved by 50
# EUR, so the digest below carries the burden of detection and these carry the burden
# of explanation.
FIELDS = ("income", "expenses", "transactions")

# The settlement outputs themselves, so a moved figure names itself in the report.
SETTLEMENT_FIELDS = ("ratio", "total_shared_expenses", "paid", "balances", "transfer")

# A convenience label, never a reconciliation tolerance. A drift earns this label only
# when the money lines are byte-for-byte the same and the settlement algorithm moved no
# displayed figure by more than ten cents. Statement admission and every conservation
# identity remain exact to the cent.
MINOR_DRIFT_LIMIT_CENTS = 10

# Bumped whenever the digest covers different fields. A digest computed under one
# version says nothing about one computed under another, so a mismatch means reduced
# coverage to be upgraded — never drift. Without this, widening the digest would
# accuse every watched period at once, and an alarm that fires on everything is one
# nobody reads.
# v3: the line representation moved to pipeline.audit so the drift alarm and the
# "What changed?" explanation share one definition. The bytes hashed are the same facts
# in a different order, so every v2 digest is incomparable — which is precisely what
# this version number is for. Old snapshots drop to partial coverage until they are
# re-closed or accepted; none of them is accused of drifting.
DIGEST_VERSION = 3


def _line_digest(year, month=None):
    """A fingerprint of every money line the month contains, as the totals see them.

    Enumerating the fields settlement depends on would be a list that goes stale the
    moment one is added. Instead every effective line is fingerprinted through the same
    resolver the totals use, so a change anywhere in it — a sharing flip, a category
    reallocation that nets to zero, a payer correction, two rows moving in compensating
    directions — shows up, whether or not anybody predicted it.

    The line representation itself now lives in `pipeline.audit`, which also stores it
    so a drift can be *explained* and not merely announced. Two definitions of "the
    watched money line" would be one too many: they would drift apart, and then the
    alarm and the explanation of the alarm would disagree about whether anything moved.
    """
    return audit.line_digest(audit.semantic_lines(year, month))


def _line_digest_v2(year, month=None):
    """Reproduce the immediately previous digest so an existing close can prove that
    its lines are still unchanged.

    Version 3 reordered the same facts into the shared audit representation. The hashes
    are therefore not directly comparable, but v2 is still fully reproducible. Older
    versions covered fewer facts and deliberately remain partial-only.
    """
    rows = []
    for txn in sorted(store.effective_year(year), key=lambda item: str(item.get("id"))):
        if month is not None and int(txn["date"][5:7]) != int(month):
            continue
        for index, (_, part) in enumerate(settle.money_lines(txn)):
            view = settle.part_view(txn, part)
            amount = part["amount"] if part else txn.get("amount_eur")
            rows.append("|".join(str(value) for value in (
                txn.get("id"), index, txn.get("date"), txn.get("account"),
                txn.get("kind") or "normal", cents(amount or 0),
                view["category"] or "", view["sharing"] or "", view["year_cost"],
                view["tax_bucket"] or "", txn.get("income_owner") or "",
                txn.get("counterparty") or "")))
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()[:16]


def _settlement_snapshot(year, month=None):
    result = settle.settlement(year, month)
    return {name: result.get(name) for name in SETTLEMENT_FIELDS}


def path(year):
    return year_dir(year) / "closings.json"


def load(year):
    return read_json(path(year), default={})


def save(year, obj):
    schemas.validate_for_write(obj, schemas.closings_file, file=path(year))
    write_json(path(year), obj)


def figures(year, month):
    """Everything watched for one month: the readable totals, the settlement it
    produces, and a digest of the lines both are computed from."""
    summary = settle.month_summary(year, month)
    out = {name: summary[name] for name in FIELDS}
    out["settlement"] = _settlement_snapshot(year, month)
    out["digest"] = _line_digest(year, month)
    out["digest_version"] = DIGEST_VERSION
    return out


def year_figures(year):
    """The annual settlement, which is the binding one. A ratio override applied to
    the year changes it without changing any month's contents, so it is watched in
    its own right rather than inferred from the twelve months."""
    return {"settlement": _settlement_snapshot(year, None), "digest": _line_digest(year, None),
            "digest_version": DIGEST_VERSION}


def record(year, month, when=None, acceptance=None):
    """Store the month's current state as the baseline to watch.

    The comparison checkpoint is written in the same breath, and deliberately BEFORE
    the baseline: a period that says it is settled but cannot explain what has happened
    to it since is the state this whole feature exists to end. If the checkpoint cannot
    be written, the close fails and nothing claims to be settled.
    """
    key = "%d-%02d" % (int(year), int(month))
    timestamp = (when or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    metadata = {"month": int(month)}
    if acceptance:
        metadata["acceptance"] = acceptance
    audit.checkpoint(year, "close", period=key, month=int(month),
                     label="%s close" % key, metadata=metadata, when=timestamp)
    rows = load(year)
    rows[key] = dict(figures(year, month), closed_at=timestamp)
    if acceptance:
        rows[key]["acceptance"] = acceptance
    save(year, rows)
    return rows[key]


def record_year(year, when=None, acceptance=None):
    timestamp = (when or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    metadata = {"annual": True}
    if acceptance:
        metadata["acceptance"] = acceptance
    audit.checkpoint(year, "close", period="annual", month=None,
                     label="%d annual close" % int(year), metadata=metadata, when=timestamp)
    rows = load(year)
    rows["annual"] = dict(year_figures(year), closed_at=timestamp)
    if acceptance:
        rows["annual"]["acceptance"] = acceptance
    save(year, rows)
    return rows["annual"]


def drop_year(year):
    # Reopening withdraws the claim that the year was settled, so the thing it would be
    # compared against goes too.
    audit.drop(year, "close:annual")
    rows = load(year)
    if rows.pop("annual", None) is not None:
        save(year, rows)


def drop(year, month):
    """Forget the baseline. Reopening a month withdraws the claim that it is settled,
    so there is nothing left to hold it to."""
    key = "%d-%02d" % (int(year), int(month))
    audit.drop(year, "close:%s" % key)
    rows = load(year)
    if key in rows:
        del rows[key]
        save(year, rows)


def coverage(stored):
    """How much of a stored snapshot can still be checked.

    'full' watches settlement and every money line. 'partial' is an older snapshot that
    holds only totals: it cannot see a sharing flip or a ratio change, but it can still
    see the totals move. Comparing what it does hold is strictly better than skipping
    it — skipping meant a period silently had no protection at all.
    """
    if not stored:
        return "none"
    if "settlement" not in stored or stored.get("digest_version") != DIGEST_VERSION:
        return "partial"
    return "full"


def changes(stored, current):
    """What moved, in words a person can act on. Empty means nothing moved.

    Only fields the stored snapshot actually carries are compared. A thin snapshot
    judged against a rich one would report drift that never happened, which is how an
    upgrade turns into a false alarm on every watched period at once.
    """
    out = []
    for name in FIELDS:
        left, right = stored.get(name), current.get(name)
        same = (int(left or 0) == int(right or 0) if name == "transactions"
                else cents(left or 0) == cents(right or 0))
        if not same:
            out.append("%s %s -> %s" % (name, left, right))
    if "settlement" in stored:
        left, right = stored.get("settlement") or {}, current.get("settlement") or {}
        for name in SETTLEMENT_FIELDS:
            if left.get(name) != right.get(name):
                out.append("settlement %s %s -> %s" % (name, left.get(name), right.get(name)))
    # Last, and only if nothing above named itself: the digest proves something in the
    # month changed even when every figure it produces happens to land the same way.
    # A digest from another version is not comparable and says nothing either way.
    if (not out and stored.get("digest_version") == DIGEST_VERSION
            and stored.get("digest") != current.get("digest")):
        out.append("the transactions changed, though the totals it produces did not")
    return out


def _differs(stored, current):
    return bool(changes(stored, current))


def drift_assessment(stored, current, *, digest_matches=None):
    """Classify a drift without forgiving one cent of it.

    ``minor`` means a calculation-only settlement adjustment: the exact same semantic
    money lines, the same ratio, shared total and transfer direction, with no displayed
    monetary figure moving by more than ``MINOR_DRIFT_LIMIT_CENTS``. A changed digest
    is always material even when its totals cancel, because it may hide two large and
    opposite transaction changes.
    """
    moved = changes(stored, current)
    material = {"classification": "material", "max_delta_cents": None}
    if not moved or "settlement" not in stored:
        return material
    if digest_matches is None:
        digest_matches = (stored.get("digest_version") == DIGEST_VERSION
                          and stored.get("digest") == current.get("digest"))
    if not digest_matches:
        return material
    for name in FIELDS:
        left, right = stored.get(name), current.get(name)
        same = (int(left or 0) == int(right or 0) if name == "transactions"
                else cents(left or 0) == cents(right or 0))
        if not same:
            return material

    left = stored.get("settlement") or {}
    right = current.get("settlement") or {}
    if left.get("ratio") != right.get("ratio"):
        return material
    if cents(left.get("total_shared_expenses") or 0) != \
            cents(right.get("total_shared_expenses") or 0):
        return material

    deltas = []
    for field in ("paid", "balances"):
        old_values, new_values = left.get(field), right.get(field)
        if not isinstance(old_values, dict) or not isinstance(new_values, dict) \
                or set(old_values) != set(new_values):
            return material
        deltas.extend(cents(new_values[key] or 0) - cents(old_values[key] or 0)
                      for key in old_values)

    old_transfer, new_transfer = left.get("transfer"), right.get("transfer")
    if bool(old_transfer) != bool(new_transfer):
        return material
    if old_transfer:
        if old_transfer.get("from") != new_transfer.get("from") \
                or old_transfer.get("to") != new_transfer.get("to"):
            return material
        deltas.append(cents(new_transfer.get("amount") or 0)
                      - cents(old_transfer.get("amount") or 0))

    maximum = max((abs(delta) for delta in deltas), default=0)
    if maximum <= MINOR_DRIFT_LIMIT_CENTS:
        return {"classification": "minor", "max_delta_cents": maximum}
    return {"classification": "material", "max_delta_cents": maximum}


def verify(year):
    """Every closed month, and whether its figures still match the baseline.

    A closed month with no baseline is reported as unwatched rather than passing:
    months closed before this existed cannot be checked, and silently treating
    'nothing recorded' as 'nothing changed' is the failure this module exists to end.
    """
    states = store.months_state(year)
    rows = load(year)
    out = []
    for month in range(1, 13):
        key = "%d-%02d" % (int(year), month)
        if states.get(key) != "closed":
            continue
        stored = rows.get(key)
        if not stored:
            out.append({"month": key, "status": "unwatched", "coverage": "none"})
            continue
        current = figures(year, month)
        moved = changes(stored, current)
        digest_matches = (stored.get("digest_version") == DIGEST_VERSION
                          and stored.get("digest") == current.get("digest"))
        if stored.get("digest_version") == 2:
            digest_matches = stored.get("digest") == _line_digest_v2(year, month)
        assessment = drift_assessment(stored, current, digest_matches=digest_matches) if moved else None
        out.append({"month": key, "status": "drifted" if moved else "ok", "changes": moved,
                    "classification": assessment["classification"] if assessment else None,
                    "max_delta_cents": assessment["max_delta_cents"] if assessment else None,
                    "coverage": coverage(stored), "stored": stored, "current": current,
                    "closed_at": stored.get("closed_at")})
    # The annual settlement is the binding figure and does not follow from the months:
    # a ratio override applied to the year moves it while every month stays put.
    if all(states.get("%d-%02d" % (int(year), m)) == "closed" for m in range(1, 13)):
        stored = rows.get("annual")
        if not stored:
            out.append({"month": "annual", "status": "unwatched", "coverage": "none"})
        else:
            current = year_figures(year)
            moved = changes(stored, current)
            digest_matches = (stored.get("digest_version") == DIGEST_VERSION
                              and stored.get("digest") == current.get("digest"))
            if stored.get("digest_version") == 2:
                digest_matches = stored.get("digest") == _line_digest_v2(year)
            assessment = drift_assessment(stored, current, digest_matches=digest_matches) if moved else None
            out.append({"month": "annual", "status": "drifted" if moved else "ok",
                        "changes": moved,
                        "classification": assessment["classification"] if assessment else None,
                        "max_delta_cents": assessment["max_delta_cents"] if assessment else None,
                        "coverage": coverage(stored), "stored": stored,
                        "current": current, "closed_at": stored.get("closed_at")})
    return out


def baseline(year):
    """Adopt the current figures for every closed month that has no baseline.

    This asserts nothing about whether those months were right when they were closed —
    that evidence was never recorded and cannot be recovered. It starts watching them
    from now, which is the most that is honestly available.
    """
    states = store.months_state(year)
    rows = load(year)
    adopted = []
    for month in range(1, 13):
        key = "%d-%02d" % (int(year), month)
        if states.get(key) == "closed" and coverage(rows.get(key)) != "full":
            record(year, month)
            adopted.append(key)
    if (all(states.get("%d-%02d" % (int(year), m)) == "closed" for m in range(1, 13))
            and coverage(load(year).get("annual")) != "full"):
        record_year(year)
        adopted.append("annual")
    return adopted


def rebaseline(year, month=None):
    """Re-record a watched period, accepting its current state as the new baseline.

    A snapshot taken before this file knew about settlement carries only totals, so it
    cannot detect the changes that matter most. Re-recording is how an existing
    baseline is upgraded, and how a reviewed and accepted change is adopted without
    reopening and reclosing the period.
    """
    if month == "annual":
        return record_year(year)
    if month is not None:
        return record(year, month)
    states = store.months_state(year)
    for m in range(1, 13):
        if states.get("%d-%02d" % (int(year), m)) == "closed":
            record(year, m)
    if all(states.get("%d-%02d" % (int(year), m)) == "closed" for m in range(1, 13)):
        record_year(year)
    return load(year)
