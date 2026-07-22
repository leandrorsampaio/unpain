"""Unit tests for pipeline.rules_engine.effective — the categorization precedence
and rule-scoping invariants (decision > person rule > family rule > needs_review).

Hermetic: config and tax buckets are passed in, so no files are read.
Usage: .venv/bin/python tests/test_rules_engine.py
"""
import os
import sys
import tempfile
from pathlib import Path

# Isolate FA_ROOT even though effective() with explicit config/tax_buckets reads no files.
os.environ.setdefault("FA_ROOT", tempfile.mkdtemp(prefix="fa-rules-test-"))
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from pipeline.rules_engine import effective

CFG = {"items_threshold_eur": 50}
TAX = []  # no tax buckets → tax_bucket resolves to None

failures = []
n = 0


def check(name, cond):
    global n
    n += 1
    print("  %s %s" % ("OK " if cond else "FAIL", name))
    if not cond:
        failures.append(name)


def txn(cp="REWE MARKT", purpose="", amount=-10.0, **kw):
    return {"id": "t", "counterparty": cp, "purpose": purpose, "amount_eur": amount, **kw}


FAM = {"id": "r-fam", "match": {"field": "any", "contains": "REWE"},
       "category": "core-living/groceries", "sharing": "shared"}
PER = {"id": "r-per", "scope": "person1", "match": {"field": "any", "contains": "REWE"},
       "category": "recreation/restaurants", "sharing": "personal:person1"}

# 1. nothing matches → needs_review, default shared, no category
t = effective(txn(), None, [], config=CFG, tax_buckets=TAX)
check("unmatched → needs_review/shared", t["status"] == "needs_review" and t["category"] is None and t["sharing"] == "shared")

# 2. family rule matches
t = effective(txn(), None, [FAM], config=CFG, tax_buckets=TAX)
check("family rule → rule-matched", t["status"] == "rule-matched" and t["category"] == "core-living/groceries" and t["matched_rule"] == "r-fam")

# 3. person-scoped rule beats family rule on that person's account
t = effective(txn(), None, [FAM, PER], owner="person1", config=CFG, tax_buckets=TAX)
check("person rule beats family (own account)", t["matched_rule"] == "r-per" and t["category"] == "recreation/restaurants")

# 4. person-scoped rule does NOT leak to the other person's account
t = effective(txn(), None, [FAM, PER], owner="person2", config=CFG, tax_buckets=TAX)
check("person rule confined to its owner", t["matched_rule"] == "r-fam")

# 5. couple-owned account matches family rules only (person rule ignored)
t = effective(txn(), None, [PER, FAM], owner="couple", config=CFG, tax_buckets=TAX)
check("couple account → family rule only", t["matched_rule"] == "r-fam")

# 6. a decision overrides a matching rule's category
t = effective(txn(), {"category": "health/doctors", "sharing": "out-of-scope"}, [FAM], config=CFG, tax_buckets=TAX)
check("decision overrides rule category", t["category"] == "health/doctors")
check("decision out-of-scope → status auto", t["status"] == "auto")

# 7. auto:items resolves by the EUR amount threshold
low = effective(txn(amount=-40.0), {"category": "auto:items"}, [], config=CFG, tax_buckets=TAX)
high = effective(txn(amount=-80.0), {"category": "auto:items"}, [], config=CFG, tax_buckets=TAX)
check("auto:items ≤ threshold → items-up-to-50", low["category"] == "core-living/items-up-to-50")
check("auto:items > threshold → items-over-50", high["category"] == "core-living/items-over-50")

# 8. kind=internal-transfer short-circuits everything → out-of-scope + auto
t = effective(txn(), {"kind": "internal-transfer"}, [FAM], config=CFG, tax_buckets=TAX)
check("internal-transfer → out-of-scope/auto", t["kind"] == "internal-transfer" and t["sharing"] == "out-of-scope" and t["status"] == "auto" and t["category"] is None)

# 9. an invalid split (parts don't sum to the total) is rejected
t = effective(txn(amount=-100.0), {"splits": [{"amount": -60.0, "category": "core-living/groceries", "sharing": "shared"}]}, [], config=CFG, tax_buckets=TAX)
check("invalid split rejected", t["splits"] is None and t["status"] == "needs_review" and t.get("error") == "invalid split")

# 10. a valid split (sums exactly) is accepted and confirmed
t = effective(txn(amount=-100.0), {"splits": [
    {"amount": -60.0, "category": "core-living/groceries", "sharing": "shared"},
    {"amount": -40.0, "category": "recreation/hobbies", "sharing": "personal:person1"}]}, [], config=CFG, tax_buckets=TAX)
check("valid split accepted/confirmed", t["splits"] is not None and len(t["splits"]) == 2 and t["status"] == "confirmed")

# 11. a review-action rule keeps the item in the queue but records the match
REV = {"id": "r-rev", "match": {"field": "any", "contains": "AMAZON"}, "action": "review"}
t = effective(txn(cp="AMAZON EU"), None, [REV], config=CFG, tax_buckets=TAX)
check("review rule → needs_review + matched_rule", t["status"] == "needs_review" and t["matched_rule"] == "r-rev" and t["category"] is None)

# 12. force_review suppresses an otherwise-matching rule
t = effective(txn(), {"force_review": True}, [FAM], config=CFG, tax_buckets=TAX)
check("force_review suppresses rule", t["matched_rule"] is None and t["status"] == "needs_review")

MIN_CHECKS = 14
check("suite did not shrink", n >= MIN_CHECKS)

if failures:
    print("\nFAILED: %s" % ", ".join(failures))
    sys.exit(1)
print("\nrules_engine passed: %d checks" % n)
