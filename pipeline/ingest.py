"""Inbox ingestion.

File naming convention: <account-id>__anything.csv|xlsx  (double underscore).
Exception: cash.csv carries the account per row and stays in the inbox forever.
Processed files move to inbox/processed/. Re-ingesting the same file is a no-op
(content-hash dedupe), so overlapping exports are safe.
"""
import json
import shutil
from collections import Counter

from . import anchors, formats, fx, store, transfers
from .util import DATA, INBOX, load_accounts, txn_hash, year_dir


def preview_file(path):
    """Validate a tabular upload without writing canonical data."""
    cfg = formats.detect(path)
    rows, stats = formats.parse(path, cfg, with_stats=True)
    # A recognised export with no rows is a real statement for a month with no
    # activity — credit cards issue these routinely. Only call it an error when the
    # file also held rows we could not read, which means it is not what it claims.
    # (formats.parse already raises when rows exist but none of them parse.)
    if not rows and stats["skipped"]:
        raise ValueError("No transactions found. Check that this is a supported bank export and not an empty report.")
    dates = [r["date"] for r in rows]
    currencies = sorted({(r.get("currency") or "EUR").upper() for r in rows})
    return {"format": cfg["name"], "transactions": len(rows), "skipped": stats["skipped"],
            "anchors": len(stats["anchors"]),
            "date_min": min(dates) if dates else None,
            "date_max": max(dates) if dates else None,
            "currencies": currencies}


def run(verbose=True):
    accounts, _ = load_accounts()
    results = []
    files = sorted(p for p in INBOX.iterdir() if p.is_file() and not p.name.startswith("."))
    years_touched = set()
    for path in files:
        if path.name == "cash.csv":
            try:
                previous_years = {y for y in store.years()
                                  if "cash.jsonl" in store.load_year_by_file(y)}
                cash_txns = regenerate_cash(mark_transfers=False)
                years_touched.update(previous_years)
                years_touched.update(int(t["date"][:4]) for t in cash_txns)
                results.append((path.name, "%d cash transactions regenerated" % len(cash_txns)))
            except Exception as e:
                results.append((path.name, "ERROR: %s" % e))
            continue
        else:
            if "__" not in path.stem:
                results.append((path.name, "SKIPPED: name must be <account-id>__something.ext"))
                continue
            account_id = path.stem.split("__")[0]
            if account_id not in accounts:
                results.append((path.name, "SKIPPED: unknown account '%s' (see data/accounts.json)" % account_id))
                continue
        try:
            added, years, skipped, anchor_stats = _ingest_file(path, account_id, accounts)
            years_touched.update(years)
            message = "%d new transactions%s" % (
                added, " (%d non-transaction rows skipped)" % skipped if skipped else "")
            anchor_message = _anchor_message(anchor_stats)
            results.append((path.name, message + ("; " + anchor_message if anchor_message else "")))
            if path.name != "cash.csv":
                shutil.move(str(path), str(INBOX / "processed" / path.name))
        except Exception as e:
            results.append((path.name, "ERROR: %s" % e))
    for y in sorted(years_touched):
        transfers.mark_internal(y)
    if verbose:
        for name, msg in results:
            print("  %-40s %s" % (name, msg))
    return results


def _ingest_file(path, account_id, accounts):
    cfg = formats.detect(path)
    rows, stats = formats.parse(path, cfg, with_stats=True)
    by_year = {}
    occurrence = Counter()
    for r in rows:
        acct = r.get("account") or account_id
        if acct not in accounts:
            raise ValueError("row references unknown account '%s'" % acct)
        currency = (r["currency"] or "EUR").upper()
        eur, rate = (r["amount"], None) if currency == "EUR" else fx.to_eur(r["amount"], currency, r["date"])
        base = txn_hash(acct, r["date"], r["amount"], currency, r["counterparty"], r["purpose"])
        occurrence[base] += 1
        txn = {
            "id": "%s#%d" % (base, occurrence[base]),
            "account": acct,
            "date": r["date"],
            "amount_original": r["amount"],
            "currency": currency,
            "amount_eur": eur,
            "fx_rate": rate,
            "counterparty": r["counterparty"],
            "purpose": r["purpose"],
            "counterparty_iban": r.get("counterparty_iban", ""),
            "force_review": r.get("force_review", False),
            "kind": "normal",
            "source": {"file": path.name, "format": cfg["name"]},
        }
        year = int(r["date"][:4])
        by_year.setdefault(year, []).append(txn)

    added = 0
    for year, txns in by_year.items():
        existing = store.known_ids(year)
        fresh = [t for t in txns if t["id"] not in existing]
        store.append_transactions(year, path.stem, fresh)
        added += len(fresh)
    anchor_stats = _record_parsed_anchors(cfg, account_id, stats, path.name)
    return added, sorted(by_year), stats["skipped"], anchor_stats


def _record_parsed_anchors(cfg, account_id, parse_stats, source, upload=None):
    if not cfg.get("balance_row"):
        return None
    parsed = parse_stats.get("anchors") or []
    result = anchors.record(account_id, parsed, source, upload=upload) if parsed else {
        "added": 0, "duplicates": 0, "conflicts": 0,
    }
    result["found"] = len(parsed)
    return result


def _anchor_message(result):
    if result is None:
        return ""
    if not result["found"]:
        return "no balance anchor found"
    if result["conflicts"]:
        return "%d balance anchor conflict%s recorded" % (
            result["conflicts"], "" if result["conflicts"] == 1 else "s")
    if result["added"]:
        return "%d balance anchor%s captured" % (
            result["added"], "" if result["added"] == 1 else "s")
    return "balance anchor already recorded"


def regenerate_cash(mark_transfers=True):
    """Rebuild every cash.jsonl from cash.csv, returning rows in CSV order.

    cash.csv is the source of truth. Rebuilding occurrence-suffixed ids avoids
    resurrecting a deleted duplicate on the next ingest.
    """
    txns = []
    for r, acct, txn_id in cash_rows_with_ids():
        currency = (r["currency"] or "EUR").upper()
        eur, rate = (r["amount"], None) if currency == "EUR" else fx.to_eur(r["amount"], currency, r["date"])
        txns.append({
                "id": txn_id,
                "account": acct, "date": r["date"], "amount_original": r["amount"],
                "currency": currency, "amount_eur": eur, "fx_rate": rate,
                "counterparty": r["counterparty"], "purpose": r["purpose"],
                "counterparty_iban": r.get("counterparty_iban", ""),
                "force_review": r.get("force_review", False), "kind": "normal",
                "source": {"file": "cash.csv", "format": "cash"},
            })

    previous_years = {y for y in store.years() if "cash.jsonl" in store.load_year_by_file(y)}
    by_year = {}
    for txn in txns:
        by_year.setdefault(int(txn["date"][:4]), []).append(txn)
    affected = previous_years | set(by_year)
    for y in affected:
        year_dir(y)
        store.rewrite_year(y, {"cash.jsonl": by_year.get(y, [])})
    if mark_transfers:
        for y in sorted(affected):
            transfers.mark_internal(y)
    return txns


def cash_rows_with_ids(path=None, accounts=None):
    """Purely derive cash rows and occurrence-safe ids; never write canonical data."""
    path = path or (INBOX / "cash.csv")
    if not path.exists():
        return []
    accounts = accounts or load_accounts()[0]
    cfg = formats.detect(path)
    rows = formats.parse(path, cfg)
    occurrence = Counter()
    derived = []
    for row in rows:
        account = row.get("account")
        if account not in accounts:
            raise ValueError("row references unknown account '%s'" % account)
        currency = (row["currency"] or "EUR").upper()
        base = txn_hash(account, row["date"], row["amount"], currency,
                        row["counterparty"], row["purpose"])
        occurrence[base] += 1
        derived.append((row, account, "%s#%d" % (base, occurrence[base])))
    return derived


def ingest_upload(path, account_id, source_stem, original_name=None):
    """Ingest ONE uploaded file under a unique `source_stem`, so it can later be
    deleted as a unit (see delete_upload). Explicit account (no filename rule).
    Entries fall into the correct year automatically. Returns a stats dict."""
    accounts, _ = load_accounts()
    if account_id not in accounts:
        raise ValueError("unknown account '%s'" % account_id)
    cfg = formats.detect(path)
    rows, stats = formats.parse(path, cfg, with_stats=True)
    by_year = {}
    occurrence = Counter()
    for r in rows:
        acct = r.get("account") or account_id
        if acct not in accounts:
            raise ValueError("row references unknown account '%s'" % acct)
        currency = (r["currency"] or "EUR").upper()
        eur, rate = (r["amount"], None) if currency == "EUR" else fx.to_eur(r["amount"], currency, r["date"])
        base = txn_hash(acct, r["date"], r["amount"], currency, r["counterparty"], r["purpose"])
        occurrence[base] += 1
        txn = {
            "id": "%s#%d" % (base, occurrence[base]),
            "account": acct,
            "date": r["date"],
            "amount_original": r["amount"],
            "currency": currency,
            "amount_eur": eur,
            "fx_rate": rate,
            "counterparty": r["counterparty"],
            "purpose": r["purpose"],
            "counterparty_iban": r.get("counterparty_iban", ""),
            "force_review": r.get("force_review", False),
            "kind": "normal",
            "source": {"file": original_name or path.name, "format": cfg["name"], "upload": source_stem},
        }
        by_year.setdefault(int(r["date"][:4]), []).append(txn)

    added = dup = 0
    dates = []
    for year, txns in by_year.items():
        existing = store.known_ids(year)
        fresh = [t for t in txns if t["id"] not in existing]
        dup += len(txns) - len(fresh)
        store.append_transactions(year, source_stem, fresh)
        added += len(fresh)
        dates += [t["date"] for t in fresh]
    anchor_stats = _record_parsed_anchors(cfg, account_id, stats, original_name or path.name,
                                          upload=source_stem)
    years = sorted(by_year.keys())
    for y in years:
        transfers.mark_internal(y)
    return {"added": added, "duplicates": dup, "total": added + dup, "years": years,
            "skipped": stats["skipped"], "period": stats.get("period"),
            "anchors": anchor_stats, "anchor_message": _anchor_message(anchor_stats),
            "date_min": min(dates) if dates else None, "date_max": max(dates) if dates else None,
            "format": cfg["name"]}


def upload_contents(source_stem):
    """What one upload put in the store: rows and decisions per year, and which of
    its months are closed. Read-only, so a confirmation can state the damage before
    anything is removed."""
    detail = {"years": {}, "transactions": 0, "decisions": 0, "closed_months": []}
    for y in store.years():
        path = DATA / str(y) / "transactions" / (source_stem + ".jsonl")
        if not path.exists():
            continue
        rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
        ids = {row["id"] for row in rows}
        decs = store.decisions(y)
        months = store.months_state(y)
        closed = sorted({row["date"][:7] for row in rows if months.get(row["date"][:7]) == "closed"})
        detail["years"][y] = {"transactions": len(rows),
                              "decisions": sum(1 for i in ids if i in decs)}
        detail["transactions"] += len(rows)
        detail["decisions"] += detail["years"][y]["decisions"]
        detail["closed_months"] += closed
    detail["closed_months"] = sorted(set(detail["closed_months"]))
    return detail


def delete_upload(source_stem):
    """Delete every transaction produced by one upload (across years), prune their
    decisions and drop the balance anchors it recorded. Merchant rules are kept.
    Returns the years touched.

    Refuses when any of those transactions sit in a closed month: everything else
    in the app rejects edits there, and deleting the data wholesale would be a far
    bigger change than the edit that is forbidden.
    """
    closed = upload_contents(source_stem)["closed_months"]
    if closed:
        raise ValueError("Month %s is closed. Reopen it first." % ", ".join(closed))
    touched = []
    for y in store.years():
        path = DATA / str(y) / "transactions" / (source_stem + ".jsonl")
        if not path.exists():
            continue
        ids = {json.loads(line)["id"] for line in open(path, encoding="utf-8") if line.strip()}
        path.unlink()
        decs = store.decisions(y)
        remaining = {k: v for k, v in decs.items() if k not in ids}
        if len(remaining) != len(decs):
            store.save_decisions(y, remaining)
        touched.append(y)
    anchors.remove_for_upload(source_stem)
    for y in touched:
        transfers.mark_internal(y)
    return touched
