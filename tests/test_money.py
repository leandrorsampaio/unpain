"""The one place a number becomes money, held to the properties that make it safe.

Settlement already lost a cent once: money was summed as floats and each share rounded
alone, so a shared cent from the joint account became two cents of "paid". This module
exists so the next module to multiply, allocate or convert cannot rediscover that in its
own corner — and this file is what says the module actually does its job.

The load-bearing property is conservation: `sum(allocate(total, weights)) == total`, for
positive totals, negative totals, zero weights and ties. Everything else is a boundary:
what counts as a number, what does not, and where rounding is allowed to happen.

It also pins the rounding *policy*, deliberately. Preserving the application's existing
banker's rounding rather than adopting the more natural-looking half-up is a decision
with consequences for historical figures, and a decision nobody can see is a decision
that gets reversed by accident.

Usage: .venv/bin/python tests/test_money.py
"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(prefix="fa-money-"))
os.environ["FA_ROOT"] = str(tmp)
sys.path.insert(0, str(PROJECT))
from pipeline import money  # noqa: E402

failures = []
total_checks = 0


def check(name, cond, detail=""):
    global total_checks
    total_checks += 1
    print("  %s %s %s" % ("OK " if cond else "FAIL", name, detail if not cond else ""))
    if not cond:
        failures.append(name)


def _sum_refuses():
    try:
        money.sum_cents([1.0, float("nan")])
    except ValueError:
        return True
    return False


def refuses(value):
    try:
        money.to_cents(value)
    except ValueError:
        return True
    return False


print("== what is a number, and what only looks like one")
check("a float becomes cents", money.to_cents(12.34) == 1234)
check("a negative float keeps its sign", money.to_cents(-12.34) == -1234)
check("an int is whole units, not cents", money.to_cents(12) == 1200)
check("a decimal string parses", money.to_cents("12.34") == 1234)
check("a Decimal parses", money.to_cents(Decimal("12.34")) == 1234)
check("negative zero is zero", money.to_cents(-0.0) == 0)
check("float error does not leak: 0.1 + 0.2", money.to_cents(0.1 + 0.2) == 30)
check("nor a third of ten", money.to_cents(10 / 3) == 333)
check("a very large amount survives", money.to_cents(99999999.99) == 9999999999)

check("True is refused, though Python says it is an int", refuses(True))
check("False is refused too", refuses(False))
check("NaN is refused", refuses(float("nan")))
check("Infinity is refused", refuses(float("inf")))
check("-Infinity is refused", refuses(float("-inf")))
check("text that is not a number is refused", refuses("lots"))
check("an empty string is refused", refuses(""))
check("None is refused", refuses(None))
check("a list is refused", refuses([1, 2]))
try:
    money.to_cents("nope", field="split amount")
except ValueError as exc:
    check("the error names the field it was asked about", "split amount" in str(exc), str(exc))


print("== the rounding policy, pinned on purpose")
# Banker's rounding: .5 goes to the nearest EVEN cent. This is what the application has
# always done, because Python's built-in round() does it. It is preserved rather than
# improved to half-up, which would move historical half-cent figures.
check("0.005 rounds to the even cent (0)", money.to_cents(Decimal("0.005")) == 0)
check("0.015 rounds to the even cent (2)", money.to_cents(Decimal("0.015")) == 2)
check("0.025 rounds to the even cent (2)", money.to_cents(Decimal("0.025")) == 2)
check("-0.005 rounds to the even cent (0)", money.to_cents(Decimal("-0.005")) == 0)
check("-0.015 rounds to the even cent (-2)", money.to_cents(Decimal("-0.015")) == -2)
check("the policy is named, not implicit", money.ROUNDING == "ROUND_HALF_EVEN")
check("and versioned, so a change is a migration", money.ROUNDING_POLICY_VERSION == 1)

# The reason this file exists at all: the old and new conversions must agree on every
# amount the store can actually hold.
random.seed(20260808)
two_dp = [round(random.uniform(-99999, 99999), 2) for _ in range(50000)]
from pipeline.util import cents as legacy  # noqa: E402
check("every 2-decimal amount converts exactly as it always did",
      all(legacy(v) == money.to_cents(v) for v in two_dp),
      str([v for v in two_dp if legacy(v) != money.to_cents(v)][:3]))


print("== sums are of integers, never of floats")
rows = [0.1, 0.2, 0.3] * 1000
check("three thousand thirds add up exactly",
      money.sum_cents(rows) == 60000,
      str(money.sum_cents(rows)))
check("a sum refuses a member that is not money", _sum_refuses())
check("an empty sum is zero", money.sum_cents([]) == 0)
check("an empty sum of a generator is zero", money.sum_cents(v for v in []) == 0)


print("== allocation conserves, always")
CASES = [
    (1, [1, 1]), (1, [1, 1, 1]), (-1, [1, 1]), (0, [1, 1]),
    (101, [1, 1]), (1000, [1, 2]), (12345, [1, 1, 1]),
    (-5, [1, 1]), (-999999, [3, 7]), (100, [0, 1]), (100, [1, 0]),
    (7, [1, 1, 1, 1, 1, 1, 1]), (10 ** 9, [Fraction(1, 3), Fraction(2, 3)]),
]
for total, weights in CASES:
    parts = money.allocate_cents(total, weights)
    check("allocate %d by %s conserves" % (total, weights), sum(parts) == total,
          "%s sums to %d" % (parts, sum(parts)))

random.seed(11)
bad = 0
for _ in range(3000):
    total = random.randint(-10 ** 7, 10 ** 7)
    weights = [random.randint(0, 100) for _ in range(random.randint(2, 5))]
    if sum(weights) == 0:
        continue
    parts = money.allocate_cents(total, weights)
    if sum(parts) != total:
        bad += 1
check("3000 random allocations all conserve", bad == 0, "%d failed" % bad)

check("all-zero weights allocate nothing rather than dividing by zero",
      money.allocate_cents(100, [0, 0]) == [0, 0])
check("no weights at all is an empty split", money.allocate_cents(100, []) == [])
check("allocation is deterministic",
      money.allocate_cents(101, [1, 1]) == money.allocate_cents(101, [1, 1]))
check("the odd cent goes to exactly one side",
      sorted(money.allocate_cents(101, [1, 1])) == [50, 51])
check("every part of a fair split is within one cent of exact",
      all(abs(part - 1000 / 3) <= 1 for part in money.allocate_cents(1000, [1, 1, 1])),
      str(money.allocate_cents(1000, [1, 1, 1])))


print("== rates and proportions are not money")
check("a conversion rounds once, at the end",
      money.convert_minor_units(10000, Decimal("6.25")) == 1600)
check("and does not accumulate over many rows",
      sum(money.convert_minor_units(100, Decimal("3")) for _ in range(3)) == 99)
check("a proportion stays exact", money.percentage(1, 3) == Decimal(1) / Decimal(3))
check("a proportion of nothing is undefined, not zero", money.percentage(1, 0) is None)


print("== the browser agrees with the backend")
# app.js has its own cents(); the two are used on the same numbers on the same screen,
# so a disagreement shows up as a total that changes when you reload.
fixture = [0.0, -0.0, 0.01, -0.01, 12.34, -12.34, 0.1 + 0.2, 1234.56, -99999.99, 10 / 3]
script = r"""
// The browser's cents() is a one-line arrow function in app.js. Extract and evaluate it
// as an EXPRESSION: `eval("const cents = ...")` declares the binding inside the eval's
// own scope and nothing outside can see it.
const fs = require('fs');
const app = fs.readFileSync(process.argv[2], 'utf8');
const line = app.match(/const cents = ([^\n]+);/);
if (!line) { console.error('cents() not found in app.js'); process.exit(1); }
const cents = eval('(' + line[1] + ')');
console.log(JSON.stringify(JSON.parse(process.argv[3]).map(cents)));
"""
script_path = tmp / "cents.js"
script_path.write_text(script, encoding="utf-8")
result = subprocess.run(["node", str(script_path), str(PROJECT / "app" / "static" / "app.js"),
                         json.dumps(fixture)], capture_output=True, text=True)
if result.returncode != 0:
    check("the browser cents helper could be read", False, (result.stderr or result.stdout)[-600:])
else:
    js = json.loads(result.stdout)
    py = [money.to_cents(v) for v in fixture]
    check("Python and JavaScript agree on the same fixture", js == py,
          "js=%s py=%s" % (js, py))


# Anti-shrink guard: exact count at implementation time. May only ever be RAISED
# when checks are added — never lowered (see AGENTS.md: never weaken a test).
MIN_CHECKS = 55
check("suite did not shrink", total_checks >= MIN_CHECKS,
      "total_checks=%d < %d" % (total_checks, MIN_CHECKS))

shutil.rmtree(tmp)
print()
if failures:
    print("FAILED: %s" % ", ".join(failures))
    sys.exit(1)
print("All checks passed.")
