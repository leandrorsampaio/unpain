"""Net worth over time, reconstructed from balance anchors + the raw ledger.

Unlike every other view in this app, net worth works with *levels*, not accountability
flows — so it uses the RAW ledger (all transaction kinds, incl. internal transfers and
out-of-scope, in each account's own currency), exactly like anchor verification does. An
account's balance at a date = the latest anchor on/before that date + the sum of raw
transactions since (snap-to-anchor, so a fresh anchor corrects any drift). Foreign balances
are converted to EUR at that date's ECB rate. Cash and credit-card accounts are excluded
(petty / liability); accounts without any anchor have an unknown level and are reported as
uncovered rather than silently dropped from the total.
"""
from datetime import date as date_type, timedelta

from . import anchors, fx, store
from .util import cents, load_accounts

# Account types excluded from net worth: cash is petty; credit-card is a liability we drop.
EXCLUDED_TYPES = {"cash", "credit-card"}


def _month_ends(first_iso, last_iso):
    """Month-end ISO dates from the month of first_iso through the month of last_iso."""
    y, m = int(first_iso[:4]), int(first_iso[5:7])
    ly, lm = int(last_iso[:4]), int(last_iso[5:7])
    out = []
    while (y, m) <= (ly, lm):
        nxt = date_type(y + 1, 1, 1) if m == 12 else date_type(y, m + 1, 1)
        out.append((nxt - timedelta(days=1)).isoformat())
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _native_at(day, anchor_rows, txns):
    """Balance in the account's own currency at `day`, or None if no anchor covers it yet."""
    ref = None
    for a in anchor_rows:                      # anchor_rows sorted ascending by date
        if a["date"] <= day:
            ref = a
        else:
            break
    if ref is None:
        return None
    total = cents(ref["balance"])
    for t in txns:                             # txns sorted ascending by date
        d = t.get("date", "")
        if ref["date"] < d <= day:
            total += cents(t.get("amount_original", 0))
        elif d > day:
            break
    return total / 100.0


def _eur_at(native, currency, day):
    """Convert a native balance to EUR at `day`'s ECB rate; None if unavailable."""
    if native is None:
        return None
    cur = (currency or "EUR").upper()
    if cur == "EUR":
        return round(native, 2)
    try:
        rate = fx.rate(cur, day)               # foreign units per EUR
    except Exception:
        return None
    return round(native / rate, 2)


def series(today=None):
    """Cross-year net-worth timeline for the household (all non-cash, non-credit-card accounts)."""
    accounts, _ = load_accounts()
    included = {aid: a for aid, a in accounts.items()
                if (a.get("type") or "").lower() not in EXCLUDED_TYPES}
    excluded = [aid for aid in accounts if aid not in included]
    today = today or date_type.today().isoformat()

    raw_by_year = {y: store.load_year_raw(y) for y in store.years()}
    per_acct, earliest = {}, None
    for aid, acct in included.items():
        arows = anchors._all_anchors(aid)      # cross-year, sorted by date
        txns = sorted((t for rows in raw_by_year.values() for t in rows if t.get("account") == aid),
                      key=lambda t: t.get("date", ""))
        per_acct[aid] = {"account": acct, "anchors": arows, "txns": txns}
        if arows and (earliest is None or arows[0]["date"] < earliest):
            earliest = arows[0]["date"]

    covered = [aid for aid, d in per_acct.items() if d["anchors"]]
    uncovered = [aid for aid, d in per_acct.items() if not d["anchors"]]

    empty = {"as_of": today, "currency": "EUR", "points": [], "accounts": [],
             "current": {"total_eur": 0.0, "accounts": []}, "excluded": excluded, "uncovered": uncovered}
    if earliest is None:
        return empty

    sample_dates = [d for d in _month_ends(earliest, today) if d <= today]
    if not sample_dates or sample_dates[-1] != today:
        sample_dates.append(today)             # always include "now"

    acct_points = {aid: [] for aid in covered}
    points = []
    for day in sample_dates:
        covered_here, uncovered_here, total = [], [], 0.0
        for aid in covered:
            data = per_acct[aid]
            native = _native_at(day, data["anchors"], data["txns"])
            eur = _eur_at(native, data["account"].get("currency"), day)
            if eur is None:
                uncovered_here.append(aid)
                continue
            acct_points[aid].append({"date": day, "native": round(native, 2), "eur": eur})
            total += eur
            covered_here.append(aid)
        points.append({"date": day, "total_eur": round(total, 2),
                       "covered": covered_here, "uncovered": uncovered_here})

    accounts_out, current_accounts, current_total = [], [], 0.0
    for aid in covered:
        data = per_acct[aid]
        spans = anchors.verify_loaded(aid, data["account"], data["anchors"], raw_by_year)
        # A span is only a real reconciliation failure if the account HAS transactions in that
        # window. A manual-only account (record balances, import nothing) legitimately jumps
        # between recorded points with no transactions to explain it — that's not "untrusted".
        for s in spans:
            s["has_txns"] = any(s["from"] < t.get("date", "") <= s["to"] for t in data["txns"])
        reconciled = not any(s["ok"] is False and s["has_txns"] for s in spans)
        accounts_out.append({
            "id": aid, "label": data["account"].get("label") or aid,
            "currency": (data["account"].get("currency") or "EUR").upper(),
            "type": data["account"].get("type"), "points": acct_points[aid],
            "spans": [{"from": s["from"], "to": s["to"], "ok": s["ok"], "has_txns": s["has_txns"]} for s in spans],
        })
        native_now = _native_at(today, data["anchors"], data["txns"])
        eur_now = _eur_at(native_now, data["account"].get("currency"), today)
        if eur_now is not None:
            current_total += eur_now
            current_accounts.append({
                "id": aid, "label": data["account"].get("label") or aid,
                "native": round(native_now, 2), "eur": eur_now,
                "currency": (data["account"].get("currency") or "EUR").upper(),
                "reconciled": reconciled,
            })

    return {"as_of": today, "currency": "EUR", "points": points, "accounts": accounts_out,
            "current": {"total_eur": round(current_total, 2), "accounts": current_accounts},
            "excluded": excluded, "uncovered": uncovered}
