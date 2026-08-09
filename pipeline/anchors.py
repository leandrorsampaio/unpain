"""Balance anchors and raw-statement chain verification."""
from datetime import date as date_type, datetime, timezone
from math import isfinite

from . import schemas, store
from .util import DATA, cents, load_accounts, read_json, write_json, year_dir


ANCHORS_FILE = "balance-anchors.json"
CONFLICTS_FILE = "balance-anchor-conflicts.json"


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _path(year, filename=ANCHORS_FILE):
    return year_dir(int(year)) / filename


def _save(year, rows):
    target = _path(year)
    schemas.validate_for_write(rows, schemas.balance_anchors_file, file=target)
    write_json(target, rows)


def load(year):
    """Load anchors for one year in their stable on-disk list format."""
    return read_json(DATA / str(int(year)) / ANCHORS_FILE, default=[])


def load_conflicts(year):
    """Load recorded contradictions for the future doctor check."""
    return read_json(DATA / str(int(year)) / CONFLICTS_FILE, default=[])


def _valid_date(value):
    parsed = date_type.fromisoformat(str(value))
    if parsed.isoformat() != value:
        raise ValueError("anchor date must be YYYY-MM-DD")
    return parsed


def record(account, anchor_rows, source, upload=None):
    """Record parsed anchors without overwriting contradictory balances.

    `upload` is the source_stem of the upload that produced them, so deleting that
    upload can take its anchors with it. Anchors recorded without one (inbox runs,
    manual entries) are never touched by that cleanup.
    """
    accounts, _ = load_accounts()
    if account not in accounts:
        raise ValueError("unknown account '%s'" % account)
    currency = (accounts[account].get("currency") or "EUR").upper()
    result = {"added": 0, "duplicates": 0, "conflicts": 0}

    for incoming in anchor_rows or []:
        parsed_date = _valid_date(incoming.get("date"))
        try:
            balance = float(incoming.get("balance"))
        except (TypeError, ValueError):
            raise ValueError("anchor balance must be a number")
        if not isfinite(balance):
            raise ValueError("anchor balance must be finite")
        balance = cents(balance) / 100.0
        year = parsed_date.year
        existing_rows = load(year)
        existing = next((item for item in existing_rows
                         if item.get("account") == account and item.get("date") == parsed_date.isoformat()), None)
        if existing:
            if cents(existing["balance"]) == cents(balance):
                result["duplicates"] += 1
                continue
            conflicts = load_conflicts(year)
            conflict = {
                "account": account,
                "date": parsed_date.isoformat(),
                "existing_balance": existing["balance"],
                "incoming_balance": balance,
                "currency": currency,
                "source": source,
                "recorded_at": _now(),
            }
            same = any(item.get("account") == account and item.get("date") == conflict["date"]
                       and cents(item.get("existing_balance", 0)) == cents(conflict["existing_balance"])
                       and cents(item.get("incoming_balance", 0)) == cents(conflict["incoming_balance"])
                       for item in conflicts)
            if not same:
                conflicts.append(conflict)
                write_json(_path(year, CONFLICTS_FILE), conflicts)
            result["conflicts"] += 1
            continue

        row = {
            "account": account,
            "date": parsed_date.isoformat(),
            "balance": balance,
            "currency": currency,
            "source": source,
            "kind": incoming.get("kind") or "statement",
            "captured_at": _now(),
        }
        if upload:
            row["upload"] = upload
        existing_rows.append(row)
        existing_rows.sort(key=lambda item: (item.get("date", ""), item.get("account", "")))
        _save(year, existing_rows)
        result["added"] += 1
    return result


def add_manual(account, date, balance):
    """Record one user-entered account balance."""
    return record(account, [{"date": date, "balance": balance, "kind": "manual"}], "manual")


def _all_anchors(account_id):
    rows = []
    if not DATA.exists():
        return rows
    for directory in DATA.iterdir():
        if directory.is_dir() and directory.name.isdigit() and len(directory.name) == 4:
            rows.extend(item for item in load(int(directory.name)) if item.get("account") == account_id)
    return sorted(rows, key=lambda item: item["date"])


def list_for(account_id):
    """Every recorded anchor for one account, across years, oldest first."""
    return _all_anchors(account_id)


def remove(account, date):
    """Delete one anchor (and any conflict logged for that account+date). Returns count removed."""
    parsed = _valid_date(date)
    year = parsed.year
    rows = load(year)
    kept = [item for item in rows if not (item.get("account") == account and item.get("date") == parsed.isoformat())]
    removed = len(rows) - len(kept)
    if removed:
        _save(year, kept)
        conflicts = load_conflicts(year)
        kept_conflicts = [c for c in conflicts if not (c.get("account") == account and c.get("date") == parsed.isoformat())]
        if len(kept_conflicts) != len(conflicts):
            write_json(_path(year, CONFLICTS_FILE), kept_conflicts)
    return {"removed": removed}


def remove_for_upload(upload):
    """Drop every anchor an upload recorded, plus any conflict logged at the same
    account+date. Without this, deleting a statement leaves a balance anchor
    behind that asserts a balance for data no longer in the store, and the next
    chain verification fails against a statement nobody can find."""
    removed = 0
    for year in store.years():
        rows = load(year)
        kept = [item for item in rows if item.get("upload") != upload]
        if len(kept) == len(rows):
            continue
        dropped = [item for item in rows if item.get("upload") == upload]
        _save(year, kept)
        removed += len(dropped)
        conflicts = load_conflicts(year)
        stale = {(item["account"], item["date"]) for item in dropped}
        kept_conflicts = [c for c in conflicts if (c.get("account"), c.get("date")) not in stale]
        if len(kept_conflicts) != len(conflicts):
            write_json(_path(year, CONFLICTS_FILE), kept_conflicts)
    return {"removed": removed}


def verify(account_id, year=None):
    """Verify every consecutive balance-anchor span using raw original amounts."""
    accounts, _ = load_accounts()
    account = accounts.get(account_id)
    if not account:
        raise ValueError("unknown account '%s'" % account_id)
    anchor_rows = _all_anchors(account_id)
    anchor_years = {int(item["date"][:4]) for item in anchor_rows}
    years = range(min(anchor_years), max(anchor_years) + 1) if anchor_years else []
    raw_by_year = {txn_year: store.load_year_raw(txn_year) for txn_year in years}
    return verify_loaded(account_id, account, anchor_rows, raw_by_year, year=year)


def verify_loaded(account_id, account, anchor_rows, raw_by_year, year=None):
    """Verify from preloaded resources so whole-store audits stay single-pass."""
    account_currency = (account.get("currency") or "EUR").upper()
    anchor_rows = sorted((item for item in anchor_rows if item.get("account") == account_id),
                         key=lambda item: item["date"])
    spans = []
    for start, end in zip(anchor_rows, anchor_rows[1:]):
        if year is not None and int(end["date"][:4]) != int(year):
            continue
        first_year, last_year = int(start["date"][:4]), int(end["date"][:4])
        transactions = []
        for txn_year in range(first_year, last_year + 1):
            transactions.extend(txn for txn in raw_by_year.get(txn_year, [])
                                if txn.get("account") == account_id
                                and start["date"] < txn.get("date", "") <= end["date"])
        expected = cents(end["balance"]) - cents(start["balance"])
        actual = sum(cents(txn.get("amount_original", 0)) for txn in transactions)
        span = {"from": start["date"], "to": end["date"],
                "expected_cents": expected, "actual_cents": actual}
        anchor_currencies = {(start.get("currency") or "").upper(),
                             (end.get("currency") or "").upper()}
        txn_currencies = {(txn.get("currency") or account_currency).upper() for txn in transactions}
        if anchor_currencies != {account_currency} or any(currency != account_currency for currency in txn_currencies):
            span.update({"ok": None, "reason": "Account or transactions use more than one currency"})
        else:
            span["ok"] = actual == expected
        spans.append(span)
    return spans


def summary(account_id, year=None):
    """Compact status used by the one-fetch Dashboard coverage card."""
    spans = verify(account_id, year=year)
    failing = next((span for span in spans if span["ok"] is False), None)
    if failing:
        difference = failing["actual_cents"] - failing["expected_cents"]
        return {"status": "mismatch",
                "detail": "%s to %s: difference %s cents" %
                          (failing["from"], failing["to"], difference)}
    verified = [span for span in spans if span["ok"] is True]
    if spans and len(verified) == len(spans):
        return {"status": "ok", "detail": "%d span%s verified, last %s" %
                (len(spans), "" if len(spans) == 1 else "s", spans[-1]["to"])}
    unavailable = next((span for span in spans if span["ok"] is None), None)
    return {"status": "none", "detail": unavailable.get("reason") if unavailable else "No consecutive balance anchors"}
