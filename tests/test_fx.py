"""Offline unit tests for pipeline.fx — cache parsing, EUR pass-through,
weekend/holiday fallback to the previous business day, and the no-rate error.

Hermetic and fully offline: a synthetic ECB cache is written under a temp FA_ROOT
and every queried date is <= the cache's newest date, so no download is attempted.
Usage: .venv/bin/python tests/test_fx.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

root = Path(tempfile.mkdtemp(prefix="fa-fx-test-"))
os.environ["FA_ROOT"] = str(root)
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from pipeline import fx

# Synthetic cache: 2026-06-13/14 is a weekend (no rows); 06-12 and 06-15 exist.
cache = root / "data" / "fx" / "eurofxref-hist.csv"
cache.parent.mkdir(parents=True, exist_ok=True)
cache.write_text(
    "Date,USD,BRL\n"
    "2026-06-15,1.1000,6.2500\n"
    "2026-06-12,1.0900,6.2000\n",
    encoding="utf-8",
)
fx._rates = None  # force a fresh load from our synthetic cache

failures = []
n = 0


def check(name, cond):
    global n
    n += 1
    print("  %s %s" % ("OK " if cond else "FAIL", name))
    if not cond:
        failures.append(name)


check("EUR is always 1.0 (no cache lookup)", fx.rate("EUR", "2026-06-15") == 1.0)
check("exact-day USD rate", fx.rate("USD", "2026-06-15") == 1.10)
check("case-insensitive currency", fx.rate("usd", "2026-06-15") == 1.10)
check("BRL rate", fx.rate("BRL", "2026-06-15") == 6.25)

# weekend fallback: 06-14 and 06-13 are missing → walk back to 06-12
check("weekend falls back to previous business day", fx.rate("USD", "2026-06-14") == 1.09)

# to_eur converts and returns the rate used
eur, r = fx.to_eur(110.0, "USD", "2026-06-15")
check("to_eur converts amount", eur == 100.0 and r == 1.10)
eur2, _ = fx.to_eur(625.0, "BRL", "2026-06-15")
check("to_eur BRL", eur2 == 100.0)

# a currency the cache never lists → LookupError (offline, no fallback found)
try:
    fx.rate("GBP", "2026-06-15")
    check("missing currency raises LookupError", False)
except LookupError:
    check("missing currency raises LookupError", True)

# --- conversions that do not divide evenly ---------------------------------
# The five-year oracle pins BRL at 6.25, which divides exactly, so nothing there ever
# lands between two cents. Real rates almost never divide exactly, and a conversion is
# the one place in the pipeline where a stored amount is *computed* rather than read.
from pipeline.util import cents  # noqa: E402

check("a rate that does not divide evenly still yields a 2-decimal amount",
      cents(fx.to_eur(100.00, "BRL", "2026-06-12")[0]) == cents(round(100.0 / 6.20, 2)))
for amount in (0.01, 0.02, 0.03, 0.07, 1.00, 33.33, 999.99, 123456.78):
    converted, rate = fx.to_eur(amount, "BRL", "2026-06-12")
    check("%.2f BRL converts to a whole number of cents" % amount,
          cents(converted) == round(converted * 100))
    check("%.2f BRL is within half a cent of the exact quotient" % amount,
          abs(converted - amount / 6.20) <= 0.005)

check("one cent never converts to a negative amount", fx.to_eur(0.01, "BRL", "2026-06-12")[0] >= 0)
check("a negative amount keeps its sign", fx.to_eur(-100.00, "BRL", "2026-06-12")[0] < 0)
check("and converts to the same magnitude as its positive twin",
      cents(fx.to_eur(-100.00, "BRL", "2026-06-12")[0])
      == -cents(fx.to_eur(100.00, "BRL", "2026-06-12")[0]))

# --- the rate follows the transaction date, not "now" -----------------------
# Every stored amount keeps the rate of its own day, so a re-import years later
# reproduces the same euros. Two dates with different rates must not agree.
check("the rate used is the transaction date's, not the newest one",
      fx.to_eur(100.0, "BRL", "2026-06-12")[1] == 6.20
      and fx.to_eur(100.0, "BRL", "2026-06-15")[1] == 6.25)
check("so the same amount on two dates converts differently",
      cents(fx.to_eur(100.0, "BRL", "2026-06-12")[0]) != cents(fx.to_eur(100.0, "BRL", "2026-06-15")[0]))
check("a weekend booking uses the Friday rate it fell back to",
      fx.to_eur(100.0, "BRL", "2026-06-14")[1] == 6.20)
check("and a Sunday falls back to the same Friday as the Saturday",
      fx.to_eur(100.0, "BRL", "2026-06-14")[0] == fx.to_eur(100.0, "BRL", "2026-06-13")[0])

MIN_CHECKS = 30
check("suite did not shrink", n >= MIN_CHECKS)

shutil.rmtree(root, ignore_errors=True)
if failures:
    print("\nFAILED: %s" % ", ".join(failures))
    sys.exit(1)
print("\nfx passed: %d checks" % n)
