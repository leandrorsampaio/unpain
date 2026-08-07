"""Recurring-payment detection (subscriptions, contracts, rent...).

A merchant is 'recurring' when it charged in >= 3 distinct months of the year
with consistent amounts. Cadence is inferred from average spacing; everything
is normalized to a monthly-equivalent cost so the dashboard can show the
household's fixed base cost.

Detection is heuristic and sometimes wrong, so it is overridable per merchant
via rules/recurring-overrides.json: a merchant key in 'never' is always dropped,
a key in 'force' is always kept (even with < 3 months). Overrides are edited
from the dashboard Recurring box; nothing is stored on the transactions.
"""
import re
import statistics
from collections import defaultdict
from datetime import date

from .util import RULES, read_json, write_json
from . import settle, store

OVERRIDES_PATH = RULES / "recurring-overrides.json"


def _merchant_key(t):
    name = (t.get("counterparty") or t.get("purpose") or "").upper()
    name = re.sub(r"[0-9]", "", name)
    return re.sub(r"\s+", " ", name).strip()[:32]


def load_overrides():
    o = read_json(OVERRIDES_PATH, default={"force": [], "never": []})
    return {"force": list(o.get("force", [])), "never": list(o.get("never", []))}


def set_override(key, state):
    """state in {'force','never','auto'}; 'auto' clears any override for key."""
    o = load_overrides()
    o["force"] = [k for k in o["force"] if k != key]
    o["never"] = [k for k in o["never"] if k != key]
    if state in ("force", "never"):
        o[state].append(key)
    write_json(OVERRIDES_PATH, o)
    return o


def _sharing_in_scope(sharing, scope):
    if scope == "all":
        return True
    return sharing == "shared" if scope == "shared" else sharing == "personal:" + scope


def _countable_amount(t, scope):
    """What this transaction actually contributes to the fixed base, splits respected.

    A split is still one charge from one merchant, so it stays one occurrence — but
    its size is only the parts that count. Reading the parent's amount instead made a
    100 EUR bill split 60 shared / 40 out of scope look like a 100 EUR fixed cost, so
    the planner overstated the household's committed spending.
    """
    if t.get("sharing") == "out-of-scope":
        return 0.0
    total = 0.0
    for _, part in settle.money_lines(t):
        view = settle.part_view(t, part)
        if view["sharing"] == "out-of-scope" or not _sharing_in_scope(view["sharing"], scope):
            continue
        total += part["amount"] if part else t["amount_eur"]
    return total


def _occurrences(year, scope):
    """One entry per charge, carrying the amount that counts rather than the raw one."""
    out = []
    for t in store.effective_year(year):
        amount = _countable_amount(t, scope)
        if amount < 0:
            out.append(dict(t, amount_eur=amount))
    return out


def detect(year, scope="all"):
    txns = _occurrences(year, scope)
    groups = defaultdict(list)
    for t in txns:
        key = _merchant_key(t)
        if key:
            groups[key].append(t)

    ov = load_overrides()
    forced, never = set(ov["force"]), set(ov["never"])

    out = []
    candidates = []  # excluded-but-plausible merchants the user can force on
    for key, ts in groups.items():
        if key in never:
            continue
        months = sorted({t["date"][:7] for t in ts})
        is_forced = key in forced
        amounts = sorted(abs(t["amount_eur"]) for t in ts)
        median = statistics.median(amounts)
        spread = (amounts[-1] - amounts[0]) / median if median else 1.0
        if not is_forced and (len(months) < 3 or median == 0 or spread > 0.6):
            if median and len(ts) >= 2:
                candidates.append({
                    "key": key,
                    "merchant": ts[0].get("counterparty") or key,
                    "category": ts[0].get("category"),
                    "occurrences": len(ts),
                    "months": len(months),
                    "median_amount": round(median, 2),
                })
            continue
        if median == 0:
            continue
        # spacing in days between consecutive charges
        dates = sorted(t["date"] for t in ts)
        gaps = [(_to_ord(dates[i + 1]) - _to_ord(dates[i])) for i in range(len(dates) - 1)]
        avg_gap = sum(gaps) / len(gaps) if gaps else 30
        if avg_gap <= 45:
            cadence, monthly = "monthly", median
        elif avg_gap <= 135:
            cadence, monthly = "quarterly", median / 3
        else:
            cadence, monthly = "yearly", median / 12
        out.append({
            "key": key,
            "override": "force" if is_forced else "auto",
            "merchant": ts[0].get("counterparty") or key,
            "category": ts[0].get("category"),
            "occurrences": len(ts),
            "months": len(months),
            "median_amount": round(median, 2),
            "cadence": cadence,
            "monthly_equivalent": round(monthly, 2),
            "last_date": dates[-1],
        })
    out.sort(key=lambda r: -r["monthly_equivalent"])
    recurring_keys = {r["key"] for r in out}
    candidates.sort(key=lambda r: -r["median_amount"])
    return {
        "items": out,
        "fixed_monthly_base": round(sum(r["monthly_equivalent"] for r in out), 2),
        "history": _history(year, recurring_keys, scope),
        "candidates": candidates,
        "overrides": ov,
    }


def _history(year, recurring_keys, scope="all"):
    """Actual monthly spend of the recurring merchants across `year`, Jan up to
    the last month that has any data (so we never draw a line dropping to zero
    for months that simply haven't happened yet). Returns [{ym, total}]."""
    if not recurring_keys:
        return []
    per_month = defaultdict(float)
    for t in _occurrences(year, scope):
        if _merchant_key(t) in recurring_keys:
            per_month[int(t["date"][5:7])] += abs(t["amount_eur"])
    if not per_month:
        return []
    last = max(per_month)
    return [{"ym": "%04d-%02d" % (year, m), "total": round(per_month.get(m, 0.0), 2)}
            for m in range(1, last + 1)]


def _to_ord(iso):
    return date.fromisoformat(iso).toordinal()
