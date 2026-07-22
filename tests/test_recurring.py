"""Unit tests for pipeline.recurring.detect — recurring-merchant detection,
candidate fallback, and force/never overrides.

Hermetic: store.effective_year is monkeypatched with synthetic transactions and
FA_ROOT points at a temp dir (so overrides read/write stay isolated).
Usage: .venv/bin/python tests/test_recurring.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

root = Path(tempfile.mkdtemp(prefix="fa-recurring-test-"))
os.environ["FA_ROOT"] = str(root)
(root / "rules").mkdir()
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from pipeline import recurring

failures = []
n = 0


def check(name, cond):
    global n
    n += 1
    print("  %s %s" % ("OK " if cond else "FAIL", name))
    if not cond:
        failures.append(name)


def series(cp, months, amount, sharing="shared", category="recreation/streaming"):
    return [{"counterparty": cp, "purpose": "", "amount_eur": amount,
             "date": "2026-%02d-15" % m, "sharing": sharing, "category": category, "kind": "normal"}
            for m in months]


ALL = list(range(1, 13))
TXNS = (
    series("NETFLIX", ALL, -12.99)                      # 12 months, steady → monthly recurring
    + series("GYM", [1, 2, 3, 4], -30.00)               # 4 months, steady → recurring
    + series("COFFEE", [1, 2], -4.00)                   # only 2 months → candidate, not recurring
    + [                                                  # 3 months but huge spread → candidate
        {"counterparty": "SPIKE", "purpose": "", "amount_eur": a, "date": "2026-%02d-10" % m,
         "sharing": "shared", "category": None, "kind": "normal"}
        for m, a in ((1, -10.0), (2, -10.0), (3, -100.0))]
    + series("BONUS", ALL, 500.0)                        # positive (income) → ignored
    + series("SECRET", ALL, -20.0, sharing="out-of-scope")  # out-of-scope → ignored
)

recurring.store.effective_year = lambda year: list(TXNS)

# --- baseline detection -----------------------------------------------------------------
res = recurring.detect(2026)
items = {r["key"]: r for r in res["items"]}
cands = {c["key"] for c in res["candidates"]}

check("NETFLIX detected as recurring", "NETFLIX" in items)
check("NETFLIX cadence monthly", items.get("NETFLIX", {}).get("cadence") == "monthly")
check("NETFLIX monthly_equivalent ~ 12.99", items.get("NETFLIX", {}).get("monthly_equivalent") == 12.99)
check("GYM detected as recurring", "GYM" in items)
check("COFFEE is a candidate, not recurring", "COFFEE" in cands and "COFFEE" not in items)
check("SPIKE (high spread) is a candidate, not recurring", "SPIKE" in cands and "SPIKE" not in items)
check("positive income merchant ignored", "BONUS" not in items and "BONUS" not in cands)
check("out-of-scope merchant ignored", "SECRET" not in items and "SECRET" not in cands)
check("fixed_monthly_base = sum of monthly equivalents",
      res["fixed_monthly_base"] == round(sum(r["monthly_equivalent"] for r in res["items"]), 2))
check("history is populated for recurring merchants", len(res["history"]) >= 1)

# --- overrides --------------------------------------------------------------------------
recurring.set_override("COFFEE", "force")
res2 = recurring.detect(2026)
items2 = {r["key"]: r for r in res2["items"]}
check("force override promotes COFFEE into recurring", "COFFEE" in items2 and items2["COFFEE"]["override"] == "force")

recurring.set_override("NETFLIX", "never")
res3 = recurring.detect(2026)
items3 = {r["key"] for r in res3["items"]}
check("never override drops NETFLIX", "NETFLIX" not in items3)

recurring.set_override("COFFEE", "auto")   # clear
recurring.set_override("NETFLIX", "auto")
res4 = recurring.detect(2026)
items4 = {r["key"] for r in res4["items"]}
check("clearing overrides restores auto detection", "NETFLIX" in items4 and "COFFEE" not in items4)

MIN_CHECKS = 13
check("suite did not shrink", n >= MIN_CHECKS)

shutil.rmtree(root, ignore_errors=True)
if failures:
    print("\nFAILED: %s" % ", ".join(failures))
    sys.exit(1)
print("\nrecurring passed: %d checks" % n)
