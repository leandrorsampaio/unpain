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

MIN_CHECKS = 8
check("suite did not shrink", n >= MIN_CHECKS)

shutil.rmtree(root, ignore_errors=True)
if failures:
    print("\nFAILED: %s" % ", ".join(failures))
    sys.exit(1)
print("\nfx passed: %d checks" % n)
