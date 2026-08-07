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

from . import settle, store
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


def _line_digest(year, month=None):
    """A fingerprint of every money line the month contains, as the totals see them.

    Enumerating the fields settlement depends on would be a list that goes stale the
    moment one is added. Instead every effective line is fingerprinted through the same
    resolver the totals use, so a change anywhere in it — a sharing flip, a category
    reallocation that nets to zero, a payer correction, two rows moving in compensating
    directions — shows up, whether or not anybody predicted it.
    """
    rows = []
    for txn in sorted(store.effective_year(year), key=lambda t: str(t.get("id"))):
        if month is not None and int(txn["date"][5:7]) != month:
            continue
        for index, (_, part) in enumerate(settle.money_lines(txn)):
            view = settle.part_view(txn, part)
            amount = part["amount"] if part else txn.get("amount_eur")
            rows.append("|".join(str(value) for value in (
                txn.get("id"), index, txn.get("date"), txn.get("account"),
                txn.get("kind") or "normal", cents(amount or 0),
                view["category"] or "", view["sharing"] or "", view["year_cost"],
                view["tax_bucket"] or "", txn.get("income_owner") or "")))
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()[:16]


def _settlement_snapshot(year, month=None):
    result = settle.settlement(year, month)
    return {name: result.get(name) for name in SETTLEMENT_FIELDS}


def path(year):
    return year_dir(year) / "closings.json"


def load(year):
    return read_json(path(year), default={})


def save(year, obj):
    write_json(path(year), obj)


def figures(year, month):
    """Everything watched for one month: the readable totals, the settlement it
    produces, and a digest of the lines both are computed from."""
    summary = settle.month_summary(year, month)
    out = {name: summary[name] for name in FIELDS}
    out["settlement"] = _settlement_snapshot(year, month)
    out["digest"] = _line_digest(year, month)
    return out


def year_figures(year):
    """The annual settlement, which is the binding one. A ratio override applied to
    the year changes it without changing any month's contents, so it is watched in
    its own right rather than inferred from the twelve months."""
    return {"settlement": _settlement_snapshot(year, None), "digest": _line_digest(year, None)}


def record(year, month, when=None):
    """Store the month's current state as the baseline to watch."""
    key = "%d-%02d" % (int(year), int(month))
    rows = load(year)
    rows[key] = dict(figures(year, month),
                     closed_at=(when or datetime.now(timezone.utc)).isoformat(timespec="seconds"))
    save(year, rows)
    return rows[key]


def record_year(year, when=None):
    rows = load(year)
    rows["annual"] = dict(year_figures(year),
                          closed_at=(when or datetime.now(timezone.utc)).isoformat(timespec="seconds"))
    save(year, rows)
    return rows["annual"]


def drop_year(year):
    rows = load(year)
    if rows.pop("annual", None) is not None:
        save(year, rows)


def drop(year, month):
    """Forget the baseline. Reopening a month withdraws the claim that it is settled,
    so there is nothing left to hold it to."""
    key = "%d-%02d" % (int(year), int(month))
    rows = load(year)
    if key in rows:
        del rows[key]
        save(year, rows)


def changes(stored, current):
    """What moved, in words a person can act on. Empty means nothing moved."""
    out = []
    for name in FIELDS:
        left, right = stored.get(name), current.get(name)
        same = (int(left or 0) == int(right or 0) if name == "transactions"
                else cents(left or 0) == cents(right or 0))
        if not same:
            out.append("%s %s -> %s" % (name, left, right))
    left, right = stored.get("settlement") or {}, current.get("settlement") or {}
    for name in SETTLEMENT_FIELDS:
        if left.get(name) != right.get(name):
            out.append("settlement %s %s -> %s" % (name, left.get(name), right.get(name)))
    # Last, and only if nothing above named itself: the digest proves something in the
    # month changed even when every figure it produces happens to land the same way.
    if not out and stored.get("digest") and stored.get("digest") != current.get("digest"):
        out.append("the transactions changed, though the totals it produces did not")
    return out


def _differs(stored, current):
    return bool(changes(stored, current))


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
            out.append({"month": key, "status": "unwatched"})
            continue
        if "digest" not in stored:
            # Recorded before this file watched settlement. It cannot detect the
            # changes that matter most, and comparing what it does hold against a
            # richer snapshot would manufacture drift that never happened.
            out.append({"month": key, "status": "stale-baseline"})
            continue
        current = figures(year, month)
        moved = changes(stored, current)
        out.append({"month": key, "status": "drifted" if moved else "ok", "changes": moved,
                    "stored": stored, "current": current, "closed_at": stored.get("closed_at")})
    # The annual settlement is the binding figure and does not follow from the months:
    # a ratio override applied to the year moves it while every month stays put.
    if all(states.get("%d-%02d" % (int(year), m)) == "closed" for m in range(1, 13)):
        stored = rows.get("annual")
        if not stored:
            out.append({"month": "annual", "status": "unwatched"})
        elif "digest" not in stored:
            out.append({"month": "annual", "status": "stale-baseline"})
        else:
            current = year_figures(year)
            moved = changes(stored, current)
            out.append({"month": "annual", "status": "drifted" if moved else "ok",
                        "changes": moved, "stored": stored, "current": current,
                        "closed_at": stored.get("closed_at")})
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
        if states.get(key) == "closed" and "digest" not in rows.get(key, {}):
            record(year, month)
            adopted.append(key)
    if (all(states.get("%d-%02d" % (int(year), m)) == "closed" for m in range(1, 13))
            and "digest" not in load(year).get("annual", {})):
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
