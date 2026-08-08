"""Canonical transaction store.

Layout per year: data/<year>/transactions/*.jsonl (pure bank data, one file per
source statement), decisions.json (manual review outcomes, keyed by txn id),
months.json (open/closed state). Categorization is DERIVED on read:
decision > merchant rule > needs_review — so rule changes apply to history
automatically and nothing goes stale.
"""
import json
import os
import re
import copy
import tempfile
from pathlib import Path

from . import rules_engine
from .util import DATA, ROOT, RULES, load_accounts, load_config, read_json, write_json, year_dir


class StoreCorrupt(ValueError):
    """A stored file cannot be read. The message names the file and the line."""


_EFFECTIVE_CACHE = {}


def _effective_fingerprint(year):
    paths = list((DATA / str(year) / "transactions").glob("*.jsonl"))
    paths += [DATA / str(year) / "decisions.json", DATA / "accounts.json",
              RULES / "merchant-rules.json", RULES / "categories.json",
              RULES / "tax-buckets.json", ROOT / "config.json"]
    return tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size)
                 for path in sorted(paths, key=lambda item: str(item)) if path.exists())


def years():
    # A store with no data/ directory at all is a fresh install, not an error: every
    # caller here means "which years exist", and the answer is none.
    if not DATA.exists():
        return []
    return sorted(int(p.name) for p in DATA.iterdir() if p.is_dir() and re.match(r"^\d{4}$", p.name))


def read_jsonl(path):
    """Read one transactions file, naming the line when it cannot be read.

    A line that is not JSON is a damaged store, and a damaged store is exactly when
    the doctor has to run. Letting the raw JSONDecodeError out took the diagnostic
    tool down with the data it was sent to diagnose, and said nothing about which
    file or line to look at."""
    rows = []
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise StoreCorrupt("%s line %d is not readable JSON: %s"
                                   % (Path(path).name, number, exc)) from exc
            if not isinstance(row, dict):
                raise StoreCorrupt("%s line %d is JSON but is not a transaction object"
                                   % (Path(path).name, number))
            rows.append(row)
    return rows


def load_year_raw(year):
    txns = []
    tdir = DATA / str(year) / "transactions"
    if not tdir.exists():
        return txns
    for f in sorted(tdir.glob("*.jsonl")):
        txns.extend(read_jsonl(f))
    return txns


def known_ids(year):
    return {t["id"] for t in load_year_raw(year)}


def append_transactions(year, source_name, txns):
    """Append new records to this source's jsonl for the year."""
    if not txns:
        return
    tdir = year_dir(year) / "transactions"
    path = tdir / (source_name + ".jsonl")
    with open(path, "a", encoding="utf-8") as f:
        for t in txns:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")


def rewrite_year(year, txns_by_file):
    """Used by the transfer pass to persist 'kind' updates (idempotent).

    This rewrites the canonical ledger, so it publishes the same way write_json does:
    a temporary name unique to this writer (a shared '<name>.tmp' lets two writers
    interleave into one scratch file and publish the mixture), fsynced before the
    replace so the file that becomes the ledger is on the disk and not only in the
    page cache."""
    tdir = year_dir(year) / "transactions"
    for fname, txns in txns_by_file.items():
        path = tdir / fname
        fd, tmp_name = tempfile.mkstemp(dir=str(tdir), prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for t in txns:
                    f.write(json.dumps(t, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise


EDITABLE_RAW_FIELDS = ("date", "counterparty", "amount_eur", "amount_original", "account")


def edit_transaction(year, txn_id, changes):
    """Manually correct a raw transaction's values (e.g. the bank restated it).
    Snapshots the original fields once so the edit is reversible, applies the new
    values in place, and flags the record. All math reads these raw fields via the
    effective view, so nothing downstream needs to change. Raises KeyError if the
    id is not found."""
    files = load_year_by_file(year)
    for fname, txns in files.items():
        for txn in txns:
            if txn.get("id") == txn_id:
                if not txn.get("manual_edit"):
                    txn["original"] = {key: txn.get(key) for key in EDITABLE_RAW_FIELDS}
                txn.update(changes)
                txn["manual_edit"] = True
                rewrite_year(year, {fname: txns})
                return txn
    raise KeyError(txn_id)


def reset_transaction(year, txn_id):
    """Undo a manual edit, restoring the snapshotted original values. Raises
    KeyError if the id is not found or was never manually edited."""
    files = load_year_by_file(year)
    for fname, txns in files.items():
        for txn in txns:
            if txn.get("id") == txn_id and txn.get("manual_edit"):
                for key, value in (txn.get("original") or {}).items():
                    if value is None:
                        txn.pop(key, None)
                    else:
                        txn[key] = value
                txn.pop("original", None)
                txn.pop("manual_edit", None)
                rewrite_year(year, {fname: txns})
                return txn
    raise KeyError(txn_id)


def load_year_by_file(year):
    out = {}
    tdir = DATA / str(year) / "transactions"
    if not tdir.exists():
        return out
    for f in sorted(tdir.glob("*.jsonl")):
        out[f.name] = read_jsonl(f)
    return out


def decisions(year):
    return read_json(DATA / str(year) / "decisions.json", default={})


def save_decisions(year, obj):
    write_json(year_dir(year) / "decisions.json", obj)


def months_state(year):
    return read_json(DATA / str(year) / "months.json", default={})


def save_months_state(year, obj):
    write_json(year_dir(year) / "months.json", obj)


def effective_year(year):
    """Merged view: canonical data + rules + decisions. This is what all math uses."""
    fingerprint = _effective_fingerprint(year)
    cached = _EFFECTIVE_CACHE.get(year)
    if cached and cached[0] == fingerprint:
        return copy.deepcopy(cached[1])
    raw = load_year_raw(year)
    decs = decisions(year)
    rules = rules_engine.load_rules()
    config = load_config()
    tax_buckets = read_json(RULES / "tax-buckets.json")["buckets"]
    accounts, _ = load_accounts()
    out = []
    for t in raw:
        d = decs.get(t.get("id"))
        # a decision can reassign the account; owner then follows the new account.
        # A row with no account at all is damaged data the doctor reports — but only if
        # building this view survives long enough to be asked.
        acct = (d or {}).get("account") or t.get("account")
        out.append(rules_engine.effective(t, d, rules, owner=accounts.get(acct, {}).get("owner"),
                                          config=config, tax_buckets=tax_buckets))
    _EFFECTIVE_CACHE[year] = (fingerprint, copy.deepcopy(out))
    return out
