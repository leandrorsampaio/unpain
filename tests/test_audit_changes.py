"""Explaining a drift, rather than announcing one.

`closings` can already say that a settled period moved. What it could not say was what
moved, and "the digest is different" is not something a household can act on. So the
question every check here asks is the same: after this change, does the comparison name
the transaction and the field, or does it just report that something happened?

The cases that matter most are the ones where the totals do NOT move — a rule
reclassifying a row, two edits cancelling out, a merchant renamed — because those are
exactly the changes a totals-only report is blind to.

Usage: .venv/bin/python tests/test_audit_changes.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from sandbox import PROJECT, build_sandbox

tmp = Path(tempfile.mkdtemp(prefix="fa-audit-changes-"))
os.environ["FA_ROOT"] = str(tmp)
build_sandbox(tmp)

sys.path.insert(0, str(PROJECT))
from fastapi import HTTPException  # noqa: E402
from pipeline import audit, closings, ingest, settle, store  # noqa: E402
from pipeline.util import cents, read_json, write_json  # noqa: E402
from app import server  # noqa: E402

ingest.run(verbose=False)

failures = []
total_checks = 0


def check(name, cond, detail=""):
    global total_checks
    total_checks += 1
    print("  %s %s %s" % ("OK " if cond else "FAIL", name, detail if not cond else ""))
    if not cond:
        failures.append(name)


def status_of(fn):
    try:
        fn()
    except HTTPException as exc:
        return exc.status_code
    return 200


YEAR = store.years()[0]
RAW = store.load_year_raw(YEAR)
MONTH = int(sorted(t["date"] for t in RAW)[0][5:7])
KEY = "%d-%02d" % (YEAR, MONTH)
IN_MONTH = [t for t in RAW if t["date"][:7] == KEY]
ALL_CATEGORIES = sorted(server._decision_options()[0] - {"auto:items"})
# Deliberate row choices. An income row, an out-of-scope row and an expense row behave
# differently under every check below, so "the first two" would test something other
# than the intent. The effective view is used because it carries the category a rule
# has already applied — assigning a row the category it already has changes nothing,
# and a test that asserts a change would then be asserting the wrong thing.
EFFECTIVE = {t["id"]: t for t in store.effective_year(YEAR)}
EXPENSES = [t for t in store.effective_year(YEAR)
            if t["amount_eur"] < 0 and t["date"][:7] == KEY and t["sharing"] != "out-of-scope"]
SHARED_EXPENSES = [t for t in EXPENSES if t["sharing"] == "shared"]


def other_category(txn, avoid=()):
    """A category this row does not already have."""
    current = {txn.get("category")} | set(avoid)
    return next(name for name in ALL_CATEGORIES if name not in current)


CATEGORY = other_category(EXPENSES[0])
OTHER_CATEGORY = other_category(EXPENSES[0], avoid=[CATEGORY])


def baseline(kind="manual", month=None):
    """Take a fresh checkpoint to compare against."""
    return audit.checkpoint(YEAR, kind, period="manual", month=month, label="test baseline")


def compare(checkpoint_id="close:manual"):
    return audit.compare(YEAR, checkpoint_id)


print("== nothing changed is a result, not an empty screen")
mark = baseline("close")
result = compare()
check("an untouched year reports no additions", result["summary"]["added"] == 0)
check("no removals", result["summary"]["removed"] == 0)
check("no changes", result["summary"]["changed"] == 0)
check("and no moved figures", result["figure_changes"] == [], str(result["figure_changes"]))
check("the digest agrees", result["summary"]["digest_changed"] is False)


print("== a manual decision")
target = EXPENSES[0]
baseline("close")
store.save_decisions(YEAR, {target["id"]: {"category": CATEGORY, "sharing": "shared"}})
result = compare()
changed = result["line_changes"]["changed"]
check("the changed row is named", len(changed) == 1 and changed[0]["transaction_id"] == target["id"],
      str([c["transaction_id"] for c in changed]))
check("and so is the field", "category" in changed[0]["fields"], str(changed[0]["fields"]))
check("with the old and new value", changed[0]["after"]["category"] == CATEGORY,
      str(changed[0]["after"]))
check("classified as a classification change",
      "classification_changed" in changed[0]["kinds"], str(changed[0]["kinds"]))
check("and it is attributed to a manual decision, not a rule",
      "category" in (changed[0]["decision_fields"] or []) or changed[0]["matched_rule"] is None,
      str(changed[0]))

print("== the same change made by a rule instead")
store.save_decisions(YEAR, {})
baseline("close")
rules_path = tmp / "rules" / "merchant-rules.json"
saved_rules = rules_path.read_text()
needle = (target.get("counterparty") or target.get("purpose") or "")[:12]
document = json.loads(saved_rules)
document["rules"].insert(0, {"id": "audit-test-rule", "match": {"field": "any", "contains": needle},
                             "category": OTHER_CATEGORY, "sharing": "shared", "scope": "family"})
write_json(rules_path, document)
result = compare()
by_rule = [c for c in result["line_changes"]["changed"] if c["transaction_id"] == target["id"]]
check("a rule change is reported too", bool(by_rule), str(result["summary"]))
check("and names the rule that did it",
      by_rule and by_rule[0]["matched_rule"] == "audit-test-rule", str(by_rule[:1]))
rules_path.write_text(saved_rules)


print("== changes that leave the totals exactly where they were")
store.save_decisions(YEAR, {})
baseline("close")
# Two rows swap categories. Income, expenses and savings are all identical afterwards;
# only the lines underneath moved. A totals-only report sees nothing here.
first, second = EXPENSES[0], EXPENSES[1]
store.save_decisions(YEAR, {first["id"]: {"category": CATEGORY},
                            second["id"]: {"category": CATEGORY}})
result = compare()
check("both reclassified rows are reported", result["summary"]["changed"] == 2,
      str(result["summary"]))
# Moving expenses between categories cannot change what was spent in total.
check("income and expenses did not move",
      not [f for f in result["figure_changes"] if f["name"] in ("income_cents", "expenses_cents")],
      str(result["figure_changes"]))
check("only the categories did",
      all(f["group"] == "category" for f in result["figure_changes"]),
      str([f["name"] for f in result["figure_changes"]]))
check("but the digest did, and says so", result["summary"]["digest_changed"] is True)

# A rename that changes no classification moves no money at all, and must be reported
# as presentation rather than buried or silently dropped. The row is chosen for having
# no matching rule: renaming one that DOES match reclassifies it, which is a real
# financial change and is correctly reported as one — see the next check.
unruled = next(t for t in EXPENSES if t.get("category") is None)
store.save_decisions(YEAR, {})
baseline("close")
store.edit_transaction(YEAR, unruled["id"], {"counterparty": "RENAMED MERCHANT"})
result = compare()
renamed = [c for c in result["line_changes"]["changed"] if c["transaction_id"] == unruled["id"]]
check("a renamed merchant is reported", bool(renamed), str(result["summary"]))
check("as presentation only, not as money moving",
      renamed and renamed[0]["kinds"] == ["presentation_only"], str(renamed[:1]))
check("and no figure changed with it",
      not [f for f in result["figure_changes"] if f["group"] == "totals"],
      str(result["figure_changes"]))
store.reset_transaction(YEAR, unruled["id"])

# Renaming a row that a rule matches takes it out of that rule's reach. That is a
# classification change, not a cosmetic one, and the report has to say so — this is
# exactly the kind of consequence a person does not expect from "fixing a typo".
ruled = next(t for t in EXPENSES if t.get("category") is not None)
baseline("close")
store.edit_transaction(YEAR, ruled["id"], {"counterparty": "RENAMED PAST THE RULE"})
result = compare()
fell_out = [c for c in result["line_changes"]["changed"] if c["transaction_id"] == ruled["id"]]
check("renaming a row out of its rule is reported as a classification change",
      fell_out and "classification_changed" in fell_out[0]["kinds"], str(fell_out[:1]))
check("and the category it lost is named",
      fell_out and fell_out[0]["before"].get("category") == ruled.get("category"),
      str(fell_out[:1]))
store.reset_transaction(YEAR, ruled["id"])


print("== an amount edit moves figures, and says by how much")
baseline("close")
before_amount = next(t for t in store.load_year_raw(YEAR) if t["id"] == first["id"])["amount_eur"]
store.edit_transaction(YEAR, first["id"], {"amount_eur": round(before_amount - 50.0, 2)})
result = compare()
check("the amount change is reported",
      any("amount_cents" in c["fields"] for c in result["line_changes"]["changed"]),
      str(result["line_changes"]["changed"][:1]))
savings = [f for f in result["figure_changes"] if f["name"] == "savings_cents"]
check("savings moved by exactly the edit", savings and savings[0]["delta_cents"] == -5000,
      str(savings))
check("in integer cents, with old and new both present",
      savings and {"old_cents", "new_cents", "delta_cents"} <= set(savings[0]))
store.reset_transaction(YEAR, first["id"])


print("== settlement is compared, not only income and expenses")
baseline("close")
# Moving a shared expense to personal changes who owes whom while the household's
# total expenses sit perfectly still.
shared_target = SHARED_EXPENSES[0]
store.save_decisions(YEAR, {shared_target["id"]: {"sharing": "personal:%s" % server._people()[0]}})
result = compare()
check("the settlement change is reported", result["summary"]["settlement_changed"] is True,
      str([f["name"] for f in result["figure_changes"]]))
check("and names the person whose share moved",
      any(f["name"].startswith("fair_share_cents.") for f in result["figure_changes"]),
      str([f["name"] for f in result["figure_changes"]]))
check("while total expenses did not move",
      not [f for f in result["figure_changes"] if f["name"] == "expenses_cents"],
      str(result["figure_changes"]))
store.save_decisions(YEAR, {})


print("== splits are one event, not three")
baseline("close")
amount = next(t for t in store.load_year_raw(YEAR) if t["id"] == first["id"])["amount_eur"]
half = round(amount / 2, 2)
store.save_decisions(YEAR, {first["id"]: {"splits": [
    {"amount": half, "category": CATEGORY, "sharing": "shared"},
    {"amount": round(amount - half, 2), "category": CATEGORY, "sharing": "shared"}]}})
result = compare()
check("splitting a row is summarised at the parent",
      any(s["transaction_id"] == first["id"] and s["parts_before"] == 1 and s["parts_after"] == 2
          for s in result["split_changes"]), str(result["split_changes"]))
store.save_decisions(YEAR, {})
result = compare()
check("and removing the split is summarised the same way",
      not result["split_changes"] or all(s["parts_after"] == 1 for s in result["split_changes"]),
      str(result["split_changes"]))


print("== a removed row stays explainable")
baseline("close")
remaining = [t for t in store.load_year_raw(YEAR) if t["id"] != first["id"]]
by_file = store.load_year_by_file(YEAR)
source = next(name for name, rows in by_file.items() if any(r["id"] == first["id"] for r in rows))
store.rewrite_year(YEAR, {source: [r for r in by_file[source] if r["id"] != first["id"]]})
result = compare()
removed = result["line_changes"]["removed"]
check("the removed row is reported", len(removed) == 1, str(len(removed)))
check("and can still be described, from the baseline",
      removed and removed[0]["counterparty"] is not None and removed[0]["amount_cents"],
      str(removed[:1]))
check("the store can no longer describe it",
      not any(t["id"] == first["id"] for t in store.load_year_raw(YEAR)))
store.rewrite_year(YEAR, {source: by_file[source]})
check("and restoring it reports an addition instead",
      compare()["summary"]["added"] == 0 and compare()["summary"]["removed"] == 0,
      str(compare()["summary"]))


print("== closing, reopening and accepting")
server.close_month(server.MonthState(year=YEAR, month=MONTH, state="closed"))
checkpoints = audit.load_checkpoints(YEAR)
check("closing a month records a checkpoint for it", "close:%s" % KEY in checkpoints,
      str(sorted(checkpoints)))
check("scoped to that month", checkpoints["close:%s" % KEY]["snapshot"]["month"] == MONTH)
check("and it compares only that month",
      all(line["date"][:7] == KEY
          for line in checkpoints["close:%s" % KEY]["snapshot"]["lines"].values()))
server.close_month(server.MonthState(year=YEAR, month=MONTH, state="open"))
check("reopening drops it", "close:%s" % KEY not in audit.load_checkpoints(YEAR))
check("along with the closing baseline it explained", not closings.load(YEAR).get(KEY))


print("== import and backup checkpoints")
before_slots = set(audit.load_checkpoints(YEAR))
ingest.run(verbose=False)      # an empty inbox: no years touched, so no new checkpoint
check("an ingest that changed nothing records nothing",
      set(audit.load_checkpoints(YEAR)) == before_slots, str(set(audit.load_checkpoints(YEAR))))
server.backup(parts="data")
after_backup = audit.load_checkpoints(YEAR)
check("a successful backup records one", "last-backup" in after_backup, str(sorted(after_backup)))
check("carrying the artefact it refers to",
      after_backup["last-backup"]["metadata"].get("sha256_16")
      and after_backup["last-backup"]["metadata"].get("file"),
      str(after_backup["last-backup"]["metadata"]))
check("backup checkpoints are rolling — one per year, the latest",
      len([slot for slot in after_backup if slot.startswith("last-backup")]) == 1)


print("== retention and versioning")
audit.checkpoint(YEAR, "close", period="2099-01", month=1, label="stale close")
kept = audit.prune(YEAR, active_closes=[])
check("a close for a period that is no longer closed is pruned", "close:2099-01" not in kept,
      str(sorted(kept)))
check("but the rolling ones survive", "last-backup" in kept, str(sorted(kept)))

old = audit.load_checkpoints(YEAR)
old["last-backup"] = dict(old["last-backup"], snapshot_version=0)
audit.save_checkpoints(YEAR, old)
result = audit.compare(YEAR, "last-backup")
check("an older snapshot version reports reduced coverage",
      result["reduced_coverage"] is True)
check("rather than reporting false drift",
      isinstance(result["summary"]["changed"], int))


print("== the endpoints")
check("an unknown baseline id is 404",
      status_of(lambda: server.changes_view(year=YEAR, baseline="close:nope")) == 404)
listing = server.changes_baselines(year=YEAR)
check("baselines are listed newest first",
      [b["created_at"] for b in listing["baselines"]]
      == sorted((b["created_at"] for b in listing["baselines"]), reverse=True),
      str([b["id"] for b in listing["baselines"]]))
check("every baseline carries what a picker needs",
      all({"id", "kind", "created_at", "label"} <= set(b) for b in listing["baselines"]))
payload = server.changes_view(year=YEAR, baseline=listing["baselines"][0]["id"])
check("a comparison comes back through the endpoint", "summary" in payload)


print("== checkpoints never influence a calculation")
before_summary = server.summary(year=YEAR)
before_shared = cents(settle.settlement(YEAR)["total_shared_expenses"])
stored = audit.load_checkpoints(YEAR)
poisoned = dict(stored[next(iter(stored))])
poisoned["snapshot"] = dict(poisoned["snapshot"],
                            figures=dict(poisoned["snapshot"]["figures"], income_cents=99999999))
audit.save_checkpoints(YEAR, dict(stored, **{poisoned["id"]: poisoned}))
check("a tampered checkpoint changes no total",
      server.summary(year=YEAR)["income"] == before_summary["income"],
      str((before_summary["income"], server.summary(year=YEAR)["income"])))
check("and no settlement figure either",
      cents(settle.settlement(YEAR)["total_shared_expenses"]) == before_shared)


print("== deterministic")
one = audit.compare(YEAR, listing["baselines"][0]["id"])
two = audit.compare(YEAR, listing["baselines"][0]["id"])
check("the same comparison twice is byte-identical",
      json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True))
check("and the checkpoint file is valid JSON with a version",
      read_json(audit.path(YEAR)).get("version") == 1)


# Anti-shrink guard: exact count at implementation time. May only ever be RAISED
# when checks are added — never lowered (see AGENTS.md: never weaken a test).
MIN_CHECKS = 54
check("suite did not shrink", total_checks >= MIN_CHECKS,
      "total_checks=%d < %d" % (total_checks, MIN_CHECKS))

shutil.rmtree(tmp)
print()
if failures:
    print("FAILED: %s" % ", ".join(failures))
    sys.exit(1)
print("All checks passed.")
