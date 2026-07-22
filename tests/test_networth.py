"""Net worth reconstruction: anchor+ledger levels, FX, exclusions, coverage, anchor delete."""
import csv
import json
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
root = Path(tempfile.mkdtemp(prefix="fa-networth-test-"))
os.environ["FA_ROOT"] = str(root)
shutil.copy(PROJECT / "examples" / "config.json", root / "config.json")
(root / "rules").mkdir()
for name in ("categories.json", "merchant-rules.json", "tax-buckets.json"):
    shutil.copy(PROJECT / "examples" / name, root / "rules" / name)
(root / "data" / "fx").mkdir(parents=True)
# Synthetic ECB rate cache (pinned) so this test is self-contained: the real cache under
# data/fx/ is gitignored, so it is absent in fresh clones and CI.
with open(root / "data" / "fx" / "eurofxref-hist.csv", "w", newline="", encoding="utf-8") as fxf:
    fxw = csv.writer(fxf)
    fxw.writerow(["Date", "USD", "BRL"])
    fxday, fxend = date(2023, 12, 1), date(2025, 12, 31)
    while fxday <= fxend:
        fxw.writerow([fxday.isoformat(), "1.1000", "6.2500"])
        fxday += timedelta(days=1)

# Accounts: an EUR giro, a BRL giro, a credit card (excluded), a cash account (excluded),
# and a savings account with NO anchor (uncovered).
json.dump({"accounts": [
    {"id": "giro", "owner": "person1", "bank": "B", "type": "giro", "currency": "EUR"},
    {"id": "nubank", "owner": "person1", "bank": "Nu", "type": "giro", "currency": "BRL"},
    {"id": "card", "owner": "person1", "bank": "B", "type": "credit-card", "currency": "EUR"},
    {"id": "wallet", "owner": "person1", "bank": "Cash", "type": "cash", "currency": "EUR"},
    {"id": "spar", "owner": "person1", "bank": "B", "type": "savings", "currency": "EUR"},
    {"id": "manual", "owner": "person1", "bank": "B", "type": "savings", "currency": "EUR"},
]}, open(root / "data" / "accounts.json", "w"))


def write_txns(year, rows):
    d = root / "data" / str(year) / "transactions"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "t.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def txn(id, account, date, amt, cur="EUR", eur=None):
    return {"id": id, "account": account, "date": date, "amount_original": amt, "currency": cur,
            "amount_eur": eur if eur is not None else amt, "fx_rate": None, "counterparty": "x",
            "purpose": "x", "kind": "normal", "source": {"file": "t.jsonl", "format": "x"}}


# giro: anchor 1000 on 2024-01-01; +200 and -50 during Jan -> balance 1150 by end of Jan.
# Cross-year: a Dec-2024 -100 and a Jan-2025 +25 should carry across the year boundary.
write_txns(2024, [
    txn("g1", "giro", "2024-01-10", 200.0),
    txn("g2", "giro", "2024-01-20", -50.0),
    txn("g3", "giro", "2024-12-15", -100.0),
    txn("n1", "nubank", "2024-01-15", -300.0, cur="BRL"),   # BRL account moves
    txn("c1", "card", "2024-01-05", -80.0),                 # excluded account
    txn("w1", "wallet", "2024-01-05", -5.0),                # excluded account
])
write_txns(2025, [txn("g4", "giro", "2025-01-05", 25.0)])

from app import server            # noqa: E402
from pipeline import anchors, networth  # noqa: E402

anchors.add_manual("giro", "2024-01-01", 1000.0)
anchors.add_manual("nubank", "2024-01-01", 1000.0)   # BRL 1000
anchors.add_manual("card", "2024-01-01", -80.0)      # excluded even though it has an anchor
anchors.add_manual("wallet", "2024-01-01", 50.0)     # excluded
# manual-only account: two recorded balances, NO imported transactions to explain the change.
anchors.add_manual("manual", "2024-01-01", 100.0)
anchors.add_manual("manual", "2024-06-01", 150.0)

s = networth.series(today="2025-06-30")

# ---- Exclusions: cash + credit card never appear, even with anchors.
ids = {a["id"] for a in s["accounts"]}
assert "card" not in ids and "wallet" not in ids, "cash/credit-card must be excluded"
assert set(s["excluded"]) == {"card", "wallet"}

# ---- Uncovered: savings has no anchor -> reported, not silently summed.
assert s["uncovered"] == ["spar"], s["uncovered"]

# ---- EUR giro reconstruction: 1000 +200 -50 -100 +25 = 1075 by 2025-06-30.
giro_now = next(a for a in s["current"]["accounts"] if a["id"] == "giro")
assert giro_now["native"] == 1075.0, giro_now
assert giro_now["eur"] == 1075.0

# ---- BRL account: native 1000-300 = 700 BRL, converted to EUR at 2025-06-30 rate (< 700).
nu_now = next(a for a in s["current"]["accounts"] if a["id"] == "nubank")
assert nu_now["native"] == 700.0, nu_now
assert 0 < nu_now["eur"] < 700.0, "BRL should convert to a smaller EUR amount"

# ---- Manual-only account: its changed balance is NOT flagged as a reconciliation failure
# (no transactions exist to reconcile), so has_txns is False on the failing span.
man = next(a for a in s["accounts"] if a["id"] == "manual")
man_now = next(a for a in s["current"]["accounts"] if a["id"] == "manual")
assert man_now["native"] == 150.0 and man_now["reconciled"], man_now
bad_span = next(sp for sp in man["spans"] if sp["ok"] is False)
assert bad_span["has_txns"] is False, "a manual-only span must not count as untrusted"

# ---- Total = giro + nubank(EUR) + manual.
assert round(s["current"]["total_eur"], 2) == round(giro_now["eur"] + nu_now["eur"] + man_now["eur"], 2)

# ---- Cross-year carry: the 2024-01-31 point reflects Jan activity (1150), not reset at year end.
giro_series = next(a for a in s["accounts"] if a["id"] == "giro")["points"]
jan_native = next(p for p in giro_series if p["date"] == "2024-01-31")["native"]
assert jan_native == 1150.0, jan_native

# ---- Anchor delete round-trips.
before = len(anchors.list_for("giro"))
assert server.anchor_delete(server.AnchorDelete(account="giro", date="2024-01-01"))["ok"]
assert len(anchors.list_for("giro")) == before - 1
try:
    server.anchor_delete(server.AnchorDelete(account="giro", date="2024-01-01"))
    assert False, "deleting a missing anchor must 404"
except server.HTTPException as e:
    assert e.status_code == 404

shutil.rmtree(root, ignore_errors=True)
print("Net worth passed: exclusions, uncovered, EUR+BRL reconstruction, cross-year carry, anchor delete")
