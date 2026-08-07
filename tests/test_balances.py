"""Settings › Balances grid: recorded vs derived cells, off-month anchors, guarded totals."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
root = Path(tempfile.mkdtemp(prefix="fa-balances-test-"))
os.environ["FA_ROOT"] = str(root)
shutil.copy(PROJECT / "examples" / "config.json", root / "config.json")
(root / "rules").mkdir()
for name in ("categories.json", "merchant-rules.json", "tax-buckets.json"):
    shutil.copy(PROJECT / "examples" / name, root / "rules" / name)
(root / "data").mkdir(parents=True, exist_ok=True)

json.dump({"accounts": [
    {"id": "giro", "owner": "person1", "bank": "B", "type": "giro", "currency": "EUR"},
    {"id": "spar", "owner": "person1", "bank": "B", "type": "savings", "currency": "EUR"},
    {"id": "odd", "owner": "person1", "bank": "B", "type": "giro", "currency": "EUR"},
    {"id": "wallet", "owner": "person1", "bank": "Cash", "type": "cash", "currency": "EUR"},
]}, open(root / "data" / "accounts.json", "w"))


def write_txns(year, rows):
    d = root / "data" / str(year) / "transactions"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "t.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def txn(tid, account, day, amt):
    return {"id": tid, "account": account, "date": day, "amount_original": amt, "currency": "EUR",
            "amount_eur": amt, "fx_rate": None, "counterparty": "x", "purpose": "x",
            "kind": "normal", "source": {"file": "t.jsonl", "format": "x"}}


# giro: opens 2024 at 1000, +100 in Jan 2025, +50 in Feb 2025. The Feb anchor is deliberately
# wrong by 10 so the grid has a mismatch to report.
write_txns(2024, [txn("g0", "giro", "2024-06-01", 500.0)])
write_txns(2025, [
    txn("g1", "giro", "2025-01-10", 100.0),
    txn("g2", "giro", "2025-02-10", 50.0),
    txn("o1", "odd", "2025-03-05", -25.0),
])

from app import server                      # noqa: E402
from pipeline import anchors, balances      # noqa: E402

anchors.add_manual("giro", "2024-12-31", 1000.0)
anchors.add_manual("giro", "2025-01-31", 1100.0)     # correct
anchors.add_manual("giro", "2025-02-28", 1160.0)     # wrong: ledger says 1150
anchors.add_manual("odd", "2025-03-29", 475.0)       # off the month end, on purpose

g = balances.grid(2025, today="2025-12-31")

# ---- Shape: an opening column (last December) + twelve month ends.
keys = [p["key"] for p in g["periods"]]
assert keys[0] == "opening" and len(keys) == 13, keys
assert g["periods"][0]["date"] == "2024-12-31", g["periods"][0]
assert g["periods"][1]["date"] == "2025-01-31" and g["periods"][12]["date"] == "2025-12-31"

rows = {r["id"]: r for r in g["accounts"]}

# ---- Recorded and reconciling, recorded and wrong, and derived are three distinct answers.
giro = rows["giro"]["cells"]
assert giro[0]["status"] == "recorded" and giro[0]["balance"] == 1000.0, giro[0]
assert giro[1]["status"] == "ok" and giro[1]["balance"] == 1100.0, giro[1]
assert giro[2]["status"] == "mismatch" and giro[2]["balance"] == 1160.0, giro[2]
assert giro[2]["diff_cents"] == -1000, giro[2]           # ledger moved 50, the anchors claim 60
assert giro[2]["derived"] == 1150.0, giro[2]             # what the ledger actually computes
assert giro[3]["status"] == "derived" and giro[3]["balance"] is None and giro[3]["derived"] == 1160.0

# ---- A derived cell must never be presented as recorded: no anchor date on it.
assert "anchor_date" not in giro[3], giro[3]

# ---- No anchor anywhere = unknown, never zero. A zero balance is a claim; this is silence.
assert all(c["status"] == "unknown" and c["balance"] is None and c["derived"] is None
           for c in rows["spar"]["cells"]), rows["spar"]["cells"]

# ---- An anchor dated off the month end still belongs to its month, and says so.
march = rows["odd"]["cells"][3]
assert march["status"] == "recorded" and march["balance"] == 475.0, march
assert march["anchor_date"] == "2025-03-29" and march["date"] == "2025-03-31", march

# ---- Future months are "not yet", which is not the same as unknown.
future = balances.grid(2025, today="2025-06-15")
assert future["accounts"][0]["cells"][12]["status"] in ("future",), future["accounts"][0]["cells"][12]
assert rows["spar"]["cells"][12]["status"] == "unknown"   # same cell, but today is past it

# ---- A total that cannot see every account still shows, but says so. An unmarked partial
# total is the kind of number people plan around.
opening = g["totals"][0]
assert opening["total"] == 1000.0 and opening["complete"] is False, opening
assert (opening["covered"], opening["accounts"]) == (1, 3), opening   # cash account excluded
anchors.add_manual("spar", "2024-12-31", 200.0)
anchors.add_manual("odd", "2024-12-31", 500.0)
g2 = balances.grid(2025, today="2025-12-31")
assert g2["totals"][0]["total"] == 1700.0, g2["totals"][0]      # 1000 + 200 + 500, cash excluded
assert g2["totals"][0]["complete"] is True and g2["totals"][0]["accounts"] == 3, g2["totals"][0]

# ---- Correcting a typo: same date, new figure, only when the caller asks to replace.
try:
    server.anchor_add(server.AnchorAdd(account="giro", date="2025-02-28", balance=1150.0))
    assert False, "overwriting a recorded balance must conflict by default"
except server.HTTPException as e:
    assert e.status_code == 409, e.status_code
assert server.anchor_add(server.AnchorAdd(account="giro", date="2025-02-28", balance=1150.0, replace=True))["ok"]
fixed = balances.grid(2025, today="2025-12-31")
fixed_rows = {r["id"]: r for r in fixed["accounts"]}
assert fixed_rows["giro"]["cells"][2]["status"] == "ok", fixed_rows["giro"]["cells"][2]

# ---- The endpoint hands back the same grid.
assert server.balances_view(2025)["year"] == 2025

shutil.rmtree(root, ignore_errors=True)
print("Balances grid passed: recorded/derived/mismatch/unknown, off-month anchors, guarded totals, replace")
