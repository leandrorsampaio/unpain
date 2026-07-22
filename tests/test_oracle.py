"""Independent arithmetic verification using the deterministic test fixture.

Runs in an isolated temp root (FA_ROOT) and compares the pipeline's output
against the expected results computed by scripts/gen_test_fixture.py.
Usage: .venv/bin/python tests/test_oracle.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from sandbox import PROJECT, build_sandbox

tmp = Path(tempfile.mkdtemp(prefix="fa-oracle-test-"))
os.environ["FA_ROOT"] = str(tmp)
# build_sandbox gives us config.json and categories.json;
# gen_test_fixture will overwrite accounts, rules, and inbox.
build_sandbox(tmp)
(tmp / "scripts").mkdir()
with open(tmp / "config.json", "r", encoding="utf-8") as f:
    cfg = json.load(f)
cfg["people"] = ["person1", "person2"]
# keep person_labels (from the examples template) consistent with the fixture's own slugs,
# or load_config()'s subset check rejects the leftover placeholder keys
cfg.pop("person_labels", None)
with open(tmp / "config.json", "w", encoding="utf-8") as f:
    json.dump(cfg, f)

# gen_test_fixture uses many categories not in the examples/categories.json.
sys.path.insert(0, str(PROJECT))
import scripts.gen_test_fixture as gen  # noqa: E402
cats = {"to-receive": {"type": "income", "subs": []}, 
        "living-costs": {"type": "expense", "subs": []},
        "core-living": {"type": "expense", "subs": []},
        "recreation": {"type": "expense", "subs": []},
        "living-upgrades": {"type": "expense", "subs": []},
        "donations": {"type": "expense", "subs": []},
        "health": {"type": "expense", "subs": []},
        "sports": {"type": "expense", "subs": []}}

for slug in gen.INCOME_CATS:
    group, sub = slug.split("/")
    cats[group]["subs"].append({"slug": sub, "name": sub, "ratio_income": slug in gen.RATIO_CATS})

for _, cat, _ in gen.RULE_TABLE:
    if cat:
        group, sub = cat.split("/")
        if not any(s["slug"] == sub for s in cats[group]["subs"]):
            cats[group]["subs"].append({"slug": sub, "name": sub})

# Also add the one used in the split decision
cats["core-living"]["subs"].append({"slug": "items-over-50", "name": "Items > 50"})

cat_doc = {"categories": [{"slug": k, "name": k, "type": v["type"], "subs": v["subs"]} for k, v in cats.items()]}
with open(tmp / "rules" / "categories.json", "w", encoding="utf-8") as f:
    json.dump(cat_doc, f)

sys.path.insert(0, str(PROJECT))
from pipeline import ingest, settle, store  # noqa: E402
from pipeline.util import cents  # noqa: E402
import scripts.gen_test_fixture as gen  # noqa: E402

failures = []
total_checks = 0

def check(name, cond, detail=""):
    global total_checks
    total_checks += 1
    print("  %s %s %s" % ("OK " if cond else "FAIL", name, detail if not cond else ""))
    if not cond:
        failures.append(name)

print("== Generating fixture in %s" % tmp)
gen.main()

print("== Ingesting fixture")
ingest.run(verbose=False)

# Load expected results — only ever from the sandbox the generator just wrote;
# falling back to a repo copy could silently compare against stale expectations.
expected_path = tmp / "scripts" / "expected.json"
with open(expected_path, "r", encoding="utf-8") as f:
    expected = json.load(f)

for y_str, exp in expected.items():
    y = int(y_str)
    print("== Verifying year %d" % y)
    ys = settle.year_summary(y)
    s = settle.settlement(y)
    
    # 1. year_summary
    check(f"{y} income", cents(ys["income"]) == cents(exp["income"]), f"got {ys['income']}, expected {exp['income']}")
    check(f"{y} expenses", cents(ys["expenses"]) == cents(exp["expenses"]), f"got {ys['expenses']}, expected {exp['expenses']}")
    check(f"{y} savings", cents(ys["savings"]) == cents(exp["savings"]), f"got {ys['savings']}, expected {exp['savings']}")
    for cat, exp_val in exp["by_category"].items():
        act_val = ys["by_category"].get(cat, 0.0)
        check(f"{y} category {cat}", cents(act_val) == cents(exp_val), f"got {act_val}, expected {exp_val}")
        
    # 2. settlement
    check(f"{y} ratio person1", abs(s["ratio"]["person1"] - exp["ratio"]["person1"]) < 1e-4, f"got {s['ratio']['person1']}, expected {exp['ratio']['person1']}")
    check(f"{y} ratio person2", abs(s["ratio"]["person2"] - exp["ratio"]["person2"]) < 1e-4, f"got {s['ratio']['person2']}, expected {exp['ratio']['person2']}")
    check(f"{y} total_shared", cents(s["total_shared_expenses"]) == cents(exp["total_shared"]), f"got {s['total_shared_expenses']}, expected {exp['total_shared']}")
    check(f"{y} shared_paid person1", cents(s["paid"]["person1"]) == cents(exp["shared_paid"]["person1"]), f"got {s['paid']['person1']}, expected {exp['shared_paid']['person1']}")
    check(f"{y} shared_paid person2", cents(s["paid"]["person2"]) == cents(exp["shared_paid"]["person2"]), f"got {s['paid']['person2']}, expected {exp['shared_paid']['person2']}")
    check(f"{y} settlement balance person1", cents(s["balances"]["person1"]) == cents(exp["settlement_balance"]["person1"]), f"got {s['balances']['person1']}, expected {exp['settlement_balance']['person1']}")
    check(f"{y} settlement balance person2", cents(s["balances"]["person2"]) == cents(exp["settlement_balance"]["person2"]), f"got {s['balances']['person2']}, expected {exp['settlement_balance']['person2']}")

    # 3. transfer counts
    txns = store.effective_year(y)
    txns_raw = store.load_year_raw(y)
    
    act_transfers = sum(1 for t in txns if t["kind"] == "internal-transfer")
    check(f"{y} transfer counts", act_transfers == exp["counts"]["transfers"], f"got {act_transfers}, expected {exp['counts']['transfers']}")
    
    # 4. out-of-scope invisibility
    oos = [t for t in txns_raw if t.get("sharing") == "out-of-scope" or store.decisions(y).get(t["id"], {}).get("sharing") == "out-of-scope" or store.rules_engine.effective(t, store.decisions(y).get(t["id"]), store.rules_engine.load_rules()).get("sharing") == "out-of-scope"]
    # The actual invisibility is proven by the sums above matching the expected ones,
    # but we can explicitly check they are not in the summary.
    act_oos = sum(1 for t in txns if t["sharing"] == "out-of-scope" and t["kind"] != "internal-transfer")
    check(f"{y} out_of_scope counts in effective view", act_oos == exp["counts"]["out_of_scope"], f"got {act_oos}, expected {exp['counts']['out_of_scope']}")

MIN_CHECKS = 130
check("suite did not shrink", total_checks >= MIN_CHECKS, f"total_checks={total_checks} < {MIN_CHECKS}")

shutil.rmtree(tmp)
print()
if failures:
    print("FAILED: %s" % ", ".join(failures))
    sys.exit(1)
print("All checks passed.")
