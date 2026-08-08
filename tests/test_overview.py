"""The all-years overview: totals sum the years, months hold year costs back, scope filters.

The overview widens the window and must not change the meaning of a euro while doing it, so
every assertion here compares it against settle.year_summary — the figures the Dashboard shows
for one year. If the two ever disagree, one of the two pages is lying about the same money.
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
root = Path(tempfile.mkdtemp(prefix="fa-overview-test-"))
os.environ["FA_ROOT"] = str(root)
shutil.copy(PROJECT / "examples" / "config.json", root / "config.json")
(root / "rules").mkdir()
for name in ("categories.json", "merchant-rules.json", "tax-buckets.json"):
    shutil.copy(PROJECT / "examples" / name, root / "rules" / name)
(root / "data").mkdir(parents=True, exist_ok=True)
# No merchant rules: every category here comes from an explicit decision, so the fixture says
# exactly what it means and cannot be reshaped by a rule shipped in examples/.
json.dump({"rules": []}, open(root / "rules" / "merchant-rules.json", "w"))

json.dump({"accounts": [
    {"id": "giro1", "owner": "person1", "bank": "B", "type": "giro", "currency": "EUR"},
    {"id": "giro2", "owner": "person2", "bank": "B", "type": "giro", "currency": "EUR"},
]}, open(root / "data" / "accounts.json", "w"))


def txn(tid, account, day, amt):
    return {"id": tid, "account": account, "date": day, "amount_original": amt, "currency": "EUR",
            "amount_eur": amt, "fx_rate": None, "counterparty": tid, "purpose": tid,
            "kind": "normal", "source": {"file": "t.jsonl", "format": "x"}}


def write_year(year, rows, decisions):
    d = root / "data" / str(year) / "transactions"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "t.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    json.dump(decisions, open(root / "data" / str(year) / "decisions.json", "w"))


SALARY = "to-receive/salary"
GROCERIES = "core-living/groceries"
BIKE = "transport/bike"

# 2024: salary 3000, groceries -200 (both shared), plus a -600 year cost.
write_year(2024, [
    txn("a1", "giro1", "2024-03-31", 3000.0),
    txn("a2", "giro1", "2024-03-15", -200.0),
    txn("a3", "giro1", "2024-06-10", -600.0),
], {
    "a1": {"category": SALARY, "income_owner": "person1"},
    "a2": {"category": GROCERIES, "sharing": "shared"},
    "a3": {"category": BIKE, "sharing": "shared", "year_cost": True},
})

# 2025: salary 1000, one personal cost, one out-of-scope cost that must be invisible.
write_year(2025, [
    txn("b1", "giro2", "2025-01-31", 1000.0),
    txn("b2", "giro2", "2025-02-05", -100.0),
    txn("b3", "giro2", "2025-02-06", -9999.0),
], {
    "b1": {"category": SALARY, "income_owner": "person2"},
    "b2": {"category": GROCERIES, "sharing": "personal:person2"},
    "b3": {"category": GROCERIES, "sharing": "out-of-scope"},
})

from pipeline import overview, settle  # noqa: E402

TODAY = date(2025, 5, 15)               # 2024 is complete; 2025 is five months in
o = overview.series(today=TODAY)

# ---- Shape: one entry per year on record, in order, with the ledger's span named.
assert [y["year"] for y in o["years"]] == [2024, 2025], o["years"]
assert o["first_year"] == 2024 and o["last_year"] == 2025

# ---- Totals are the year summaries added up — not a second, parallel computation.
expect = [settle.year_summary(y) for y in (2024, 2025)]
assert o["totals"]["income"] == round(sum(s["income"] for s in expect), 2)
assert o["totals"]["expenses"] == round(sum(s["expenses"] for s in expect), 2)
assert o["totals"]["savings"] == round(sum(s["savings"] for s in expect), 2)
assert o["totals"]["income"] == 4000.0, o["totals"]
# -200 groceries, -600 year cost, -100 personal. The out-of-scope -9999 is invisible to all math.
assert o["totals"]["expenses"] == -900.0, o["totals"]
assert o["totals"]["savings"] == 3100.0, o["totals"]

# ---- Savings rate is a rate of income, rounded to four places like /api/yoy.
assert o["totals"]["savings_rate"] == round(3100.0 / 4000.0, 4), o["totals"]

# ---- by_category sums across years, and the out-of-scope row never appears.
assert o["by_category"][GROCERIES] == -300.0, o["by_category"]
assert o["by_category"][BIKE] == -600.0, o["by_category"]
assert o["by_category"][SALARY] == 4000.0, o["by_category"]

# ---- The months deliberately EXCLUDE year costs, so they must fall short of the year total by
# exactly the year-cost bucket. This is the gap the UI's "spread" switch exists to explain; if
# the bucket ever stopped travelling with the months, the chart could not close it.
y2024 = next(y for y in o["years"] if y["year"] == 2024)
month_exp = sum(m["expenses"] for m in y2024["months"])
assert month_exp == -200.0, month_exp
assert y2024["year_costs"]["expenses"] == -600.0, y2024["year_costs"]
assert round(month_exp + y2024["year_costs"]["expenses"], 2) == y2024["expenses"]
assert len(y2024["months"]) == 12 and [m["month"] for m in y2024["months"]] == list(range(1, 13))
assert all(m["year"] == 2024 for m in y2024["months"])

# ---- Elapsed months: a finished year is twelve, a running one is only as long as it has been.
# Spreading a running year's costs over twelve would push cost into months that don't exist yet.
assert y2024["elapsed_months"] == 12
assert next(y for y in o["years"] if y["year"] == 2025)["elapsed_months"] == 5

# ---- Scope partitions the same money: shared + each person, no double counting of the personal.
shared = overview.series(scope="shared", today=TODAY)
assert shared["totals"]["expenses"] == -800.0, shared["totals"]     # personal -100 excluded
p2 = overview.series(scope="person2", today=TODAY)
assert p2["totals"]["expenses"] == -100.0, p2["totals"]             # only their personal cost
assert p2["totals"]["income"] == 1000.0, p2["totals"]

p1 = overview.series(scope="person1", today=TODAY)
# The three scopes are a partition of 'all', and widening the window must not break that.
for key in ("income", "expenses", "savings"):
    assert round(shared["totals"][key] + p1["totals"][key] + p2["totals"][key], 2) == o["totals"][key], key

# ---- No income means no rate. Printing 0 % would claim they saved nothing, not that we can't say.
# 'shared' income is couple-owned income only, and there is none here.
assert shared["totals"]["income"] == 0.0 and shared["totals"]["savings_rate"] is None, shared["totals"]

print("Overview passed")
