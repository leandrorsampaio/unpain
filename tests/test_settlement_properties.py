"""Settlement arithmetic under adversarial money, checked as properties.

The oracle suite proves the settlement against one fixture of comfortable figures:
whole euros, a ratio that is exactly 60/40, joint payments that halve evenly. Those
are the numbers that cannot expose a rounding defect, because there is nothing to
round. A shared cent paid from the joint account used to become two cents of "paid"
and two of "fair share", and every test stayed green.

So this suite generates the uncomfortable cases on purpose — one-cent totals, odd
cents on couple-owned accounts, ratios that are not clean fractions, refunds larger
than the spend — and checks two things that hold for *every* input:

  conservation   sum(paid) == sum(fair_share) == total_shared, sum(balances) == 0,
                 and the transfer clears the balance exactly;
  faithfulness   each reported figure is within one cent of the exact value computed
                 with Fraction — so conservation cannot be bought by handing all the
                 money to one person.

Faithfulness is what keeps this from being production's algorithm written twice.
Nothing here reimplements largest remainder; it bounds the answer independently and
lets any correct rounding through.

Usage: .venv/bin/python tests/test_settlement_properties.py
"""
import json
import os
import random
import shutil
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

from sandbox import PROJECT, build_sandbox

tmp = Path(tempfile.mkdtemp(prefix="fa-settle-prop-"))
os.environ["FA_ROOT"] = str(tmp)
build_sandbox(tmp)

sys.path.insert(0, str(PROJECT))
from pipeline import settle, store  # noqa: E402
from pipeline.util import cents, load_accounts, load_config, write_json  # noqa: E402

failures = []
total_checks = 0


def check(name, cond, detail=""):
    global total_checks
    total_checks += 1
    if not cond:
        print("  FAIL %s %s" % (name, detail))
        failures.append(name)
    return cond


YEAR = 2031            # far from the fixture years, so nothing else lands in it
PEOPLE = load_config()["people"]
ACCOUNTS = load_accounts()[0]
BY_OWNER = {}
for aid, account in ACCOUNTS.items():
    BY_OWNER.setdefault(account["owner"], []).append(aid)
SALARY = sorted(settle.ratio_income_categories())[0]


def expense_category():
    """Any non-income category — the settlement only cares that it is not income."""
    doc = json.loads((tmp / "rules" / "categories.json").read_text())
    for group in doc["categories"]:
        if group.get("type") != "income" and group.get("subs"):
            return "%s/%s" % (group["slug"], group["subs"][0]["slug"])
    raise SystemExit("the example categories have no expense subcategory")


EXPENSE = expense_category()


def build(entries):
    """Write one year made of `entries` and return what settlement says about it.

    Every scenario goes through the real store: raw JSONL, decisions, the effective
    view, `settle.settlement`. Testing the arithmetic in isolation would not notice
    a rule or an inheritance rule changing which lines reach it.
    """
    txns, decisions = [], {}
    for index, entry in enumerate(entries):
        txn_id = "prop-%d" % index
        txns.append({
            "id": txn_id, "account": entry["account"], "date": "%d-06-%02d" % (YEAR, index % 28 + 1),
            "amount_original": entry["cents"] / 100.0, "currency": "EUR",
            "amount_eur": entry["cents"] / 100.0, "fx_rate": None,
            "counterparty": "PROP %d" % index, "purpose": "property scenario",
            "counterparty_iban": "", "kind": "normal",
            "source": {"file": "properties.jsonl", "format": "test"},
        })
        decision = {"category": entry["category"], "sharing": entry["sharing"]}
        if entry.get("income_owner"):
            decision["income_owner"] = entry["income_owner"]
        decisions[txn_id] = decision
    store.rewrite_year(YEAR, {"properties.jsonl": txns})
    store.save_decisions(YEAR, decisions)
    return settle.settlement(YEAR)


def exact(entries):
    """What the figures are before anybody has to round them.

    Fractions, not floats: the point of comparison is the value production had to
    round, and a float has already rounded it.
    """
    ratio_income = {p: Fraction(0) for p in PEOPLE}
    paid = {p: Fraction(0) for p in PEOPLE}
    total_shared = Fraction(0)
    for entry in entries:
        amount = Fraction(entry["cents"])
        owner = ACCOUNTS[entry["account"]]["owner"]
        if entry["category"] == SALARY:
            income_owner = entry.get("income_owner") or owner
            if income_owner == "couple":
                for person in PEOPLE:
                    ratio_income[person] += amount / 2
            else:
                ratio_income[income_owner] += amount
        elif entry["sharing"] == "shared":
            spent = -amount
            if owner == "couple":
                for person in PEOPLE:
                    paid[person] += spent / 2
            else:
                paid[owner] += spent
            total_shared += spent
    return ratio_income, paid, total_shared


def verify(label, entries, result):
    """Conservation exactly; every reported figure within a cent of the truth."""
    ratio_income, paid, total_shared = exact(entries)
    shared_cents = cents(result["total_shared_expenses"])
    paid_cents = {p: cents(result["paid"][p]) for p in PEOPLE}
    fair_cents = {p: cents(result["fair_share"][p]) for p in PEOPLE}
    balance_cents = {p: cents(result["balances"][p]) for p in PEOPLE}

    ok = check("%s: total shared is the exact total" % label, shared_cents == total_shared,
               "%s vs %s" % (shared_cents, total_shared))
    ok &= check("%s: sum(paid) == total shared" % label, sum(paid_cents.values()) == shared_cents,
                str(paid_cents))
    ok &= check("%s: sum(fair_share) == total shared" % label,
                sum(fair_cents.values()) == shared_cents, str(fair_cents))
    ok &= check("%s: sum(balances) == 0" % label, sum(balance_cents.values()) == 0,
                str(balance_cents))
    ok &= check("%s: balance is paid minus fair share" % label,
                all(balance_cents[p] == paid_cents[p] - fair_cents[p] for p in PEOPLE),
                str((paid_cents, fair_cents, balance_cents)))

    # Faithfulness: conservation alone would be satisfied by giving one person
    # everything, so each figure is also held to within a cent of the exact value.
    ok &= check("%s: paid is within a cent of the exact share" % label,
                all(abs(paid_cents[p] - paid[p]) <= 1 for p in PEOPLE),
                str((paid_cents, {p: float(paid[p]) for p in PEOPLE})))

    total_income = sum(ratio_income.values())
    if total_income > 0 and all(ratio_income[p] >= 0 for p in PEOPLE):
        exact_ratio = {p: ratio_income[p] / total_income for p in PEOPLE}
        ok &= check("%s: the ratio is the exact income proportion" % label,
                    all(abs(Fraction(result["ratio"][p]).limit_denominator(10 ** 6) - exact_ratio[p])
                        < Fraction(1, 10 ** 4) for p in PEOPLE),
                    str((result["ratio"], {p: float(exact_ratio[p]) for p in PEOPLE})))
        ok &= check("%s: fair share is within a cent of ratio x total" % label,
                    all(abs(fair_cents[p] - exact_ratio[p] * total_shared) <= 1 for p in PEOPLE),
                    str((fair_cents, float(total_shared))))
    else:
        # No derivable ratio: the settlement must say so rather than invent one.
        ok &= check("%s: an underivable ratio is reported, not invented" % label,
                    result["ratio_source"].startswith("reference ratio")
                    or result["ratio_source"] == "manual override",
                    result["ratio_source"])

    transfer = result["transfer"]
    creditor = max(balance_cents, key=lambda p: balance_cents[p])
    if balance_cents[creditor] > 0:
        ok &= check("%s: the transfer clears the balance exactly" % label,
                    bool(transfer) and cents(transfer["amount"]) == balance_cents[creditor]
                    and transfer["to"] == creditor,
                    str(transfer))
    else:
        ok &= check("%s: nothing owed means no transfer" % label, transfer is None, str(transfer))
    return ok


# ---------------------------------------------------------------- named cases
# The cases a reader should be able to find by name, because each one is a defect
# that shipped or nearly did.
print("== the awkward cases, by name")

JOINT = BY_OWNER["couple"][0]
P1, P2 = PEOPLE
A1, A2 = BY_OWNER[P1][0], BY_OWNER[P2][0]


def shared(cents_value, account):
    return {"cents": cents_value, "account": account, "category": EXPENSE, "sharing": "shared"}


def salary(cents_value, account, income_owner=None):
    return {"cents": cents_value, "account": account, "category": SALARY, "sharing": "shared",
            "income_owner": income_owner}


NAMED = {
    "one cent from the joint account": [salary(100000, A1), salary(100000, A2), shared(-1, JOINT)],
    "odd cents from the joint account": [salary(100000, A1), salary(100000, A2),
                                         shared(-333, JOINT), shared(-777, JOINT)],
    "a third and two thirds": [salary(100000, A1), salary(200000, A2), shared(-1000, A1)],
    "a ratio of 1 to 99": [salary(100, A1), salary(9900, A2), shared(-12345, A1)],
    "a ratio of 100 to 0": [salary(500000, A1), shared(-9999, A2)],
    "no salary at all falls back to the reference ratio": [shared(-4321, A1)],
    "couple salary splits evenly": [salary(100001, JOINT, "couple"), shared(-777, A1)],
    "a refund larger than the spend": [salary(100000, A1), salary(100000, A2),
                                       shared(-500, A1), shared(1500, JOINT)],
    "everything paid by one person": [salary(300000, A1), salary(100000, A2), shared(-99999, A1)],
    "a single cent of salary each": [salary(1, A1), salary(1, A2), shared(-1, JOINT)],
    "nothing shared at all": [salary(100000, A1), salary(100000, A2)],
    "a negative salary correction": [salary(-100000, A1), salary(500000, A2), shared(-1234, JOINT)],
    "large figures": [salary(98765432, A1), salary(12345678, A2), shared(-87654321, JOINT)],
}
for label, entries in NAMED.items():
    verify(label, entries, build(entries))
print("  %d named cases checked" % len(NAMED))


# ---------------------------------------------------------------- generated cases
# A fixed seed, so a failure is reproducible and the suite does not change its mind
# between runs. The generator is deliberately biased toward small odd amounts: those
# are where rounding decides the answer, and they are exactly what real fixtures lack.
print("== generated cases")
rng = random.Random(20260808)
ACCOUNT_POOL = [A1, A2, JOINT]
generated_failures = 0
for case in range(120):
    entries = []
    for _ in range(rng.randint(1, 2)):
        entries.append(salary(rng.choice([1, 3, 7, 99, 100001, 250000, 333333]),
                              rng.choice(ACCOUNT_POOL),
                              "couple" if rng.random() < 0.25 else None))
    for _ in range(rng.randint(1, 6)):
        amount = rng.choice([-1, -3, -7, -33, -99, -101, -12345, -999999, 55, 1])
        entries.append(shared(amount, rng.choice(ACCOUNT_POOL)))
    if not verify("generated %d" % case, entries, build(entries)):
        generated_failures += 1
        print("    entries: %r" % (entries,))
check("every generated case conserved and stayed faithful", generated_failures == 0,
      "%d of 120 failed" % generated_failures)


# ---------------------------------------------------------------- determinism
print("== the same input always splits the same way")
repeatable = NAMED["odd cents from the joint account"]
first = build(repeatable)
check("settlement is deterministic", build(repeatable) == first)
check("and so is the allocation it rests on",
      settle.allocate_cents(101, {P1: 1, P2: 1}, [P1, P2])
      == settle.allocate_cents(101, {P1: 1, P2: 1}, [P1, P2]))
# The odd cent must land on a person, not vanish and not double.
odd = settle.allocate_cents(101, {P1: 1, P2: 1}, [P1, P2])
check("an odd cent goes to exactly one side", sorted(odd.values()) == [50, 51], str(odd))


print("== the odd cent is decided by the rule, not by binary rounding error")
# Conservation cannot see this: dividing the income weights into a float ratio before
# allocating still hands out every cent, it just hands the last one to the wrong person.
# At weights 1:5 over 3 cents the float path pays 0/3 where exact fractions pay 1/2.
# So this compares against exact arithmetic rather than against a sum.
disagreements = []
for w1 in range(1, 40):
    for w2 in range(1, 40):
        for amount in range(1, 25):
            total = w1 + w2
            approximate = settle.allocate_cents(amount, {P1: w1 / total, P2: w2 / total},
                                                [P1, P2])
            precise = settle.allocate_cents(amount, {P1: Fraction(w1, total),
                                                     P2: Fraction(w2, total)}, [P1, P2])
            if approximate != precise:
                disagreements.append((w1, w2, amount, approximate, precise))
check("a float ratio and an exact one allocate differently — that is why settle passes "
      "exact weights", bool(disagreements),
      "if this ever stops being true the guard below is measuring nothing")

# The property that matters: whatever the salaries, the settlement's own fair share must
# equal the allocation computed from exact integer half-cent weights.
mismatches = []
for salary1 in (1, 2, 3, 5, 7, 100, 12345, 999999):
    for salary2 in (1, 2, 3, 5, 7, 100, 12345, 999999):
        for spend in (-1, -2, -3, -7, -101, -99999):
            entries = [salary(salary1, A1), salary(salary2, A2), shared(spend, JOINT)]
            result = build(entries)
            weights = {P1: salary1, P2: salary2}
            expected = settle.allocate_cents(cents(result["total_shared_expenses"]),
                                             {p: Fraction(weights[p], salary1 + salary2)
                                              for p in (P1, P2)}, [P1, P2])
            actual = {p: cents(result["fair_share"][p]) for p in (P1, P2)}
            if actual != expected:
                mismatches.append((salary1, salary2, spend, actual, expected))
check("every fair share equals the exact allocation from integer income weights",
      not mismatches, "%d mismatches, first: %s" % (len(mismatches), mismatches[:1]))


# ---------------------------------------------------------------- manual overrides
print("== a manual override is a ratio like any other")
write_json(tmp / "data" / str(YEAR) / "ratio-overrides.json",
           {"annual": {P1: 1, P2: 2}})
entries = [salary(100000, A1), salary(100000, A2), shared(-1, JOINT), shared(-1000, A1)]
result = build(entries)
check("an override still conserves cents",
      cents(sum(result["paid"].values())) == cents(result["total_shared_expenses"])
      and cents(sum(result["fair_share"].values())) == cents(result["total_shared_expenses"])
      and cents(sum(result["balances"].values())) == 0, str(result))
check("and it is the override that was applied", result["ratio_source"] == "manual override"
      and abs(result["ratio"][P2] - 2 / 3) < 1e-4, str(result["ratio"]))
(tmp / "data" / str(YEAR) / "ratio-overrides.json").unlink()


# Anti-shrink guard: exact count at implementation time. May only ever be RAISED
# when checks are added — never lowered (see AGENTS.md: never weaken a test).
MIN_CHECKS = 1202
check("suite did not shrink", total_checks >= MIN_CHECKS,
      "total_checks=%d < %d" % (total_checks, MIN_CHECKS))

shutil.rmtree(tmp)
if failures:
    print("\nFAILED: %s" % ", ".join(sorted(set(failures))[:10]))
    sys.exit(1)
print("\nSettlement properties passed: %d checks over %d scenarios, cents conserved and faithful."
      % (total_checks, len(NAMED) + 120))
