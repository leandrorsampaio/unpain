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
from datetime import datetime, timezone

from . import settle, store
from .util import cents, read_json, write_json, year_dir

FIELDS = ("income", "expenses", "transactions")


def path(year):
    return year_dir(year) / "closings.json"


def load(year):
    return read_json(path(year), default={})


def save(year, obj):
    write_json(path(year), obj)


def figures(year, month):
    """The monthly picture as the app reports it today."""
    summary = settle.month_summary(year, month)
    return {name: summary[name] for name in FIELDS}


def record(year, month, when=None):
    """Store the month's current figures as the baseline to watch."""
    key = "%d-%02d" % (int(year), int(month))
    rows = load(year)
    rows[key] = dict(figures(year, month),
                     closed_at=(when or datetime.now(timezone.utc)).isoformat(timespec="seconds"))
    save(year, rows)
    return rows[key]


def drop(year, month):
    """Forget the baseline. Reopening a month withdraws the claim that it is settled,
    so there is nothing left to hold it to."""
    key = "%d-%02d" % (int(year), int(month))
    rows = load(year)
    if key in rows:
        del rows[key]
        save(year, rows)


def _differs(stored, current):
    for name in FIELDS:
        left, right = stored.get(name), current.get(name)
        if name == "transactions":
            if int(left or 0) != int(right or 0):
                return True
        elif cents(left or 0) != cents(right or 0):
            return True
    return False


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
        current = figures(year, month)
        out.append({"month": key, "status": "drifted" if _differs(stored, current) else "ok",
                    "stored": {name: stored.get(name) for name in FIELDS},
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
        if states.get(key) == "closed" and key not in rows:
            record(year, month)
            adopted.append(key)
    return adopted
