"""Inbox ingestion.

File naming convention: <account-id>__anything.csv|xlsx  (double underscore).
Exception: cash.csv carries the account per row and stays in the inbox forever.
Processed files move to inbox/processed/. Re-ingesting the same file is a no-op
(content-hash dedupe), so overlapping exports are safe.
"""
import json
import shutil
from collections import Counter

from . import anchors, audit, extraction, formats, fx, store, transfers
from .mutation_lock import mutation_lock
from .util import DATA, INBOX, load_accounts, read_json, txn_hash, year_dir

# A normalized ``generic-extracted`` CSV is not a bank export: it is the output of an
# extractor or a hand-written transformation.  Its shape carries no bank-authored totals,
# so every production ingestion boundary requires the reconciliation sidecar.  Filename
# suffixes are kept for a useful error message, but are not the trust boundary: renaming an
# extracted file must never turn it into an ordinary, trusted bank statement.
EXTRACTED_SUFFIX = ".extracted"
REPORT_SUFFIX = ".report.json"


def preview_file(path):
    """Validate a tabular upload without writing canonical data."""
    cfg = formats.detect(path)
    # Preview and Process must make the same promise. In particular, do not show a
    # green transaction count for normalized extractor output that Process will later
    # refuse because the reconciliation sidecar is absent or invalid.
    _admit_extracted(path, cfg)
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
            # How well this format is actually known. A definition written from a
            # screenshot and one checked against a real export are different claims, and
            # the preview is where somebody decides whether to trust the numbers below.
            "verification_status": cfg.get("verification_status"),
            "anchors": len(stats["anchors"]),
            "date_min": min(dates) if dates else None,
            "date_max": max(dates) if dates else None,
            "currencies": currencies}


def mutation_years(path, anchor_rows=None):
    """Years whose canonical files one upload may rewrite.

    Transfer detection reads and may rewrite the imported year plus an existing
    adjacent year for Dec/Jan pairs. Balance anchors can sit on the day before the
    first transaction, which may be in another year. Returning this set before the
    first write lets the web workflow roll back only relevant year directories.
    """
    cfg = formats.detect(path)
    rows, stats = formats.parse(path, cfg, with_stats=True)
    transaction_years = {int(row["date"][:4]) for row in rows}
    existing = set(store.years())
    affected = set(transaction_years)
    for year in transaction_years:
        affected.update(candidate for candidate in (year - 1, year + 1)
                        if candidate in existing)
    anchors_to_record = stats.get("anchors", []) if anchor_rows is None else anchor_rows
    affected.update(int(row["date"][:4]) for row in anchors_to_record or []
                    if isinstance(row.get("date"), str) and len(row["date"]) >= 4)
    return sorted(affected)


def run(verbose=True):
    """Process the inbox while excluding web and other CLI mutations."""
    with mutation_lock():
        return _run_locked(verbose)


def _run_locked(verbose=True):
    accounts, _ = load_accounts()
    results = []
    files = sorted(p for p in INBOX.iterdir() if p.is_file() and not p.name.startswith(".")
                   and not p.name.endswith(REPORT_SUFFIX))   # sidecars travel with their CSV
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
                sidecar = report_path_for(path)
                if sidecar.is_file():
                    shutil.move(str(sidecar), str(INBOX / "processed" / sidecar.name))
        except Exception as e:
            results.append((path.name, "ERROR: %s" % e))
    for y in sorted(years_touched):
        transfers.mark_internal(y)
    # One checkpoint per run, not per file: transfer detection finishes after the file
    # loop, so a per-file checkpoint would record figures that the very next line of
    # this function changes. Labelled with everything the run processed.
    processed = sorted(name for name, message in results
                       if not message.startswith(("ERROR", "SKIPPED")))
    for y in sorted(years_touched):
        audit.checkpoint(y, "import", label=", ".join(processed) or "inbox ingest",
                         metadata={"source": "cli", "files": processed})
    if verbose:
        for name, msg in results:
            print("  %-40s %s" % (name, msg))
    return results


def converted(amount, currency, day):
    """The FX fields every stored transaction carries, from one helper.

    Three ingestion paths used to each write `fx_rate` from their own `fx.to_eur`
    call, which was fine while the rate was the only fact recorded. It is not: the
    ECB publishes on business days, so the rate that converted a Saturday booking
    belongs to the Friday before it, and without that date an audit cannot reproduce
    the conversion — it can only guess which day was used. EUR rows carry no rate,
    no date and no source, because nothing converted them.
    """
    if (currency or "EUR").upper() == "EUR":
        return {"amount_eur": round(float(amount), 2), "fx_rate": None,
                "fx_rate_date": None, "fx_rate_source": None}
    details = fx.to_eur_details(amount, currency, day)
    return {"amount_eur": details["eur"], "fx_rate": details["rate"],
            "fx_rate_date": details["rate_date"], "fx_rate_source": "ECB"}


def report_path_for(path):
    """Where the reconciliation report for an extracted CSV lives."""
    return path.with_name(path.stem + REPORT_SUFFIX)


def _admit_extracted(path, cfg=None):
    """Hold an extracted statement to the reconciliation it claims to have passed.

    The skill's instructions have always called reconciliation mandatory, and nothing
    ever checked: the pipeline read the CSV, and the format file's own note asserted
    that reconciliation "happens in the skill". A claim nobody verifies is not a gate.
    Any file detected as ``generic-extracted`` must arrive with its report, and any
    file that brings a report has that report re-checked here — against the CSV that
    is actually about to be imported, not against whatever the producer had in front
    of it.  The deterministic PDF path passes ``admitted=True`` only after calling
    :func:`pipeline.extraction.admit` itself.
    """
    report_file = report_path_for(path)
    if not report_file.is_file():
        cfg = cfg or formats.detect(path)
        if path.stem.endswith(EXTRACTED_SUFFIX) or cfg.get("name") == "generic-extracted":
            raise ValueError(
                "%s uses the normalized extracted-statement format, which may only be imported together with its "
                "reconciliation report. Write %s beside it, carrying the statement's own "
                "opening_balance and closing_balance, as skills/extract-statement/instructions.md "
                "describes." % (path.name, report_file.name))
        return None
    return extraction.admit(read_json(report_file), path)


def _ingest_file(path, account_id, accounts):
    cfg = formats.detect(path)
    _admit_extracted(path, cfg)
    rows, stats = formats.parse(path, cfg, with_stats=True)
    by_year = {}
    occurrence = Counter()
    for r in rows:
        acct = r.get("account") or account_id
        if acct not in accounts:
            raise ValueError("row references unknown account '%s'" % acct)
        currency = (r["currency"] or "EUR").upper()
        money = converted(r["amount"], currency, r["date"])
        base = txn_hash(acct, r["date"], r["amount"], currency, r["counterparty"], r["purpose"])
        occurrence[base] += 1
        txn = {
            "id": "%s#%d" % (base, occurrence[base]),
            "account": acct,
            "date": r["date"],
            "amount_original": r["amount"],
            "currency": currency,
            **money,
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
    if not result.get("found"):
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
        money = converted(r["amount"], currency, r["date"])
        txns.append({
                "id": txn_id,
                "account": acct, "date": r["date"], "amount_original": r["amount"],
                "currency": currency, **money,
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


def ingest_upload(path, account_id, source_stem, original_name=None, admitted=False):
    """Ingest ONE uploaded file under a unique `source_stem`, so it can later be
    deleted as a unit (see delete_upload). Explicit account (no filename rule).
    Entries fall into the correct year automatically. Returns a stats dict.

    `admitted=True` means the caller already put this file through
    extraction.admit — the PDF extractors do, on the CSV they just wrote. Anything
    else claiming to be an extracted statement has to bring its report."""
    accounts, _ = load_accounts()
    if account_id not in accounts:
        raise ValueError("unknown account '%s'" % account_id)
    if not admitted:
        cfg = formats.detect(path)
        _admit_extracted(path, cfg)
    else:
        cfg = formats.detect(path)
    rows, stats = formats.parse(path, cfg, with_stats=True)
    by_year = {}
    occurrence = Counter()
    for r in rows:
        acct = r.get("account") or account_id
        if acct not in accounts:
            raise ValueError("row references unknown account '%s'" % acct)
        currency = (r["currency"] or "EUR").upper()
        money = converted(r["amount"], currency, r["date"])
        base = txn_hash(acct, r["date"], r["amount"], currency, r["counterparty"], r["purpose"])
        occurrence[base] += 1
        txn = {
            "id": "%s#%d" % (base, occurrence[base]),
            "account": acct,
            "date": r["date"],
            "amount_original": r["amount"],
            "currency": currency,
            **money,
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
