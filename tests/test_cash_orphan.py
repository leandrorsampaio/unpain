"""Cash ledger vs deleted accounts: usage guard counts cash.csv, orphan rows fail soft.

Regression for the 500 when a cash account is deleted (its years already removed) while
inbox/cash.csv still references it — the next cash add crashed regeneration.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
root = Path(tempfile.mkdtemp(prefix="fa-cash-orphan-"))
os.environ["FA_ROOT"] = str(root)
shutil.copy(PROJECT / "examples" / "config.json", root / "config.json")
(root / "rules").mkdir()
for name in ("categories.json", "merchant-rules.json", "tax-buckets.json"):
    shutil.copy(PROJECT / "examples" / name, root / "rules" / name)
(root / "data").mkdir()
(root / "inbox").mkdir()
json.dump({"accounts": [
    {"id": "test", "owner": "person1", "bank": "XX", "type": "cash", "currency": "EUR"},
]}, open(root / "data" / "accounts.json", "w"))

from app import server  # noqa: E402
from fastapi import HTTPException  # noqa: E402

CASH = root / "inbox" / "cash.csv"


def add(desc, account="test"):
    return server.cash(server.CashEntry(date="2026-07-22", account=account, amount=-9.0,
                                        currency="EUR", description=desc, category=""))


# ---- Healthy ledger: a cash account with rows counts as in use and blocks deletion.
CASH.write_text("date,account,amount,currency,description,category\n"
                "2026-07-16,test,-4.0,EUR,coffee,core-living/groceries\n", encoding="utf-8")
assert server.account_usage()["counts"].get("test") == 1, "cash.csv rows must count toward usage"
try:
    server.account_delete(server.AccountDelete(id="test"))
    assert False, "must not delete an account that still has cash entries"
except HTTPException as e:
    assert e.status_code == 409

# ---- Happy path still works: adding a cash entry to a valid ledger succeeds.
r = add("valid new item")
assert r["ok"] and r["result"] == "1 new transaction"

# ---- Orphaned ledger: a row references a removed account -> clean 409, not a 500.
CASH.write_text("date,account,amount,currency,description,category\n"
                "2026-07-16,cash-person1,222.0,EUR,bonus,to-receive/salary\n"
                "2026-07-16,test,-4.0,EUR,coffee,core-living/groceries\n", encoding="utf-8")
try:
    add("blocked by orphan")
    assert False, "add must be blocked while cash.csv references a removed account"
except HTTPException as e:
    assert e.status_code == 409 and "cash-person1" in e.detail
except Exception as e:  # the pre-fix behavior: a raw ValueError -> 500
    assert False, "orphan ledger must fail soft, got %s: %s" % (type(e).__name__, e)

# ---- Re-adding the missing account fixes it: add works again.
doc = json.load(open(root / "data" / "accounts.json"))
doc["accounts"].append({"id": "cash-person1", "owner": "person1", "bank": "Cash", "type": "cash", "currency": "EUR"})
json.dump(doc, open(root / "data" / "accounts.json", "w"))
r = add("works after re-add")
assert r["ok"], "re-adding the removed account must unblock cash writes"

shutil.rmtree(root, ignore_errors=True)
print("Cash-orphan passed: usage counts cash.csv, delete blocked, orphan fails soft (409), re-add recovers")
