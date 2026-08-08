"""The doctor as an auditor, not a second opinion from the same source.

An auditor that calls the function it is auditing can only ever agree with it. The
conservation checks here therefore recompute the totals the long way, from the effective
rows, and the test that matters most is the one that breaks `settle.year_summary` on
purpose and asserts the doctor notices — because if it does not, the check is decoration.

The rest is the discipline every integrity tool needs and few have: seed exactly one
violation, assert exactly the finding it should produce, and prove the tool wrote
nothing and asked the network nothing while looking.

Usage: .venv/bin/python tests/test_doctor_audit.py
"""
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

from sandbox import PROJECT, build_sandbox

tmp = Path(tempfile.mkdtemp(prefix="fa-doctor-audit-"))
os.environ["FA_ROOT"] = str(tmp)
build_sandbox(tmp)

sys.path.insert(0, str(PROJECT))
from pipeline import doctor, ingest, settle, store  # noqa: E402
from pipeline.util import read_json, write_json  # noqa: E402

ingest.run(verbose=False)

failures = []
total_checks = 0


def check(name, cond, detail=""):
    global total_checks
    total_checks += 1
    print("  %s %s %s" % ("OK " if cond else "FAIL", name, detail if not cond else ""))
    if not cond:
        failures.append(name)


YEAR = store.years()[0]
RULES_PATH = tmp / "rules" / "merchant-rules.json"
SAVED_RULES = RULES_PATH.read_text()


def findings(year=YEAR):
    return doctor.run(year)["findings"]


def has(check_name, year=YEAR):
    return [f for f in findings(year) if f["check"] == check_name]


def tree_hash():
    digest = hashlib.sha256()
    for path in sorted(tmp.rglob("*")):
        if path.is_file() and not path.name.startswith("."):
            digest.update(str(path.relative_to(tmp)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


print("== a healthy store produces no integrity errors")
errors = [f for f in findings() if f["severity"] == "error"]
check("no errors on the fixture store", not errors, str([f["check"] for f in errors]))
check("and the schema audit is actually registered",
      doctor._schema_findings in doctor.CHECKS,
      "it was defined but never added to CHECKS, so it never ran")


print("== the auditor does not take settle's word for it")
# The point of an independent recomputation is that it disagrees when the thing it is
# auditing is wrong. Break the reported total and the doctor must notice.
original_year_summary = settle.year_summary


def lying_summary(year, scope="all"):
    result = original_year_summary(year, scope=scope)
    if scope == "all":
        result = dict(result, income=result["income"] + 100.0)
    return result


settle.year_summary = lying_summary
try:
    check("a wrong year total is caught by recomputation from the rows",
          bool(has("conservation:year-totals")),
          str([f["check"] for f in findings()]))
finally:
    settle.year_summary = original_year_summary
check("and the healthy store passes again", not has("conservation:year-totals"))

original_settlement = settle.settlement


def lying_settlement(year, month=None):
    result = original_settlement(year, month)
    if month is None:
        paid = dict(result["paid"])
        first = sorted(paid)[0]
        paid[first] = round(paid[first] + 1.0, 2)      # a euro from nowhere
        result = dict(result, paid=paid)
    return result


settle.settlement = lying_settlement
try:
    check("a settlement that does not conserve is caught",
          bool(has("conservation:settlement")), str([f["check"] for f in findings()]))
finally:
    settle.settlement = original_settlement


print("== a split that does not add up")
# Checked where splits actually live — in the decisions. The effective view drops an
# invalid split rather than applying it, so a check reading that view could never see
# one, which is why the conservation pass deliberately does not duplicate this.
raw = store.load_year_raw(YEAR)[0]
decisions = store.decisions(YEAR)
write_json(tmp / "data" / str(YEAR) / "decisions.json",
           dict(decisions, **{raw["id"]: {"splits": [{"amount": -1.0}, {"amount": -2.0}]}}))
reported = has("split-sum") + has("schema:split-sum")
check("the parent and its parts are reconciled", bool(reported),
      str([f["check"] for f in findings()]))
check("and the transaction is named",
      any(raw["id"] in (f["ids"] or []) or raw["id"] in f["message"] for f in reported),
      str(reported[:1]))
check("and the invalid split is not applied to the totals",
      all(t.get("splits") is None for t in store.effective_year(YEAR) if t["id"] == raw["id"]))
write_json(tmp / "data" / str(YEAR) / "decisions.json", decisions)
check("clean again once the split is removed", not has("split-sum") and not has("schema:split-sum"))


print("== a stored euro amount that does not follow from its rate")
by_file = store.load_year_by_file(YEAR)
source_name = next(iter(by_file))
rows = [dict(row) for row in by_file[source_name]]
rows[0].update({"currency": "BRL", "amount_original": -62.00, "amount_eur": -99.99,
                "fx_rate": 6.20, "fx_rate_date": rows[0]["date"], "fx_rate_source": "ECB"})
store.rewrite_year(YEAR, {source_name: rows})
fx_cache = tmp / "data" / "fx" / "eurofxref-hist.csv"
fx_cache.parent.mkdir(parents=True, exist_ok=True)
fx_cache.write_text("Date,USD,BRL\n%s,1.10,6.2000\n" % rows[0]["date"], encoding="utf-8")
from pipeline import fx  # noqa: E402
fx._rates = None
found = has("fx:amount-mismatch")
check("a euro figure that cannot be reproduced is reported", bool(found),
      str([f["check"] for f in findings()]))
check("and it names the transaction", found and rows[0]["id"] in found[0]["ids"], str(found[:1]))
rows[0].update({"amount_eur": -10.00})
store.rewrite_year(YEAR, {source_name: rows})
check("and clears once the amount agrees", not has("fx:amount-mismatch"),
      str(has("fx:amount-mismatch")[:1]))
store.rewrite_year(YEAR, {source_name: by_file[source_name]})
fx_cache.unlink()
fx._rates = None


print("== rules that cannot fire, or that eat each other")
def set_rules(rules):
    write_json(RULES_PATH, {"rules": rules})


category = sorted(read_json(tmp / "rules" / "categories.json")["categories"][0]["subs"],
                  key=lambda s: s["slug"])
category_slug = "%s/%s" % (read_json(tmp / "rules" / "categories.json")["categories"][0]["slug"],
                           category[0]["slug"])

set_rules([{"id": "empty", "match": {"field": "any", "contains": ""},
            "category": category_slug, "scope": "family"}])
check("a rule with no pattern is reported as never matching", bool(has("rule:never-matches")),
      str([f["check"] for f in findings()]))

set_rules([{"id": "a", "match": {"field": "any", "contains": "REWE"}, "category": category_slug},
           {"id": "b", "match": {"field": "any", "contains": "REWE"}, "category": category_slug}])
check("a repeated condition is reported", bool(has("rule:duplicate")),
      str([f["check"] for f in findings()]))

set_rules([{"id": "broad", "match": {"field": "any", "contains": "REWE"}, "category": category_slug},
           {"id": "narrow", "match": {"field": "any", "contains": "REWE MARKT BERLIN"},
            "category": category_slug}])
found = has("rule:shadowed")
check("a rule the earlier one always beats is reported", bool(found),
      str([f["check"] for f in findings()]))
check("and it names the rule that will never see a row",
      found and "narrow" in found[0]["ids"], str(found[:1]))

set_rules([{"id": "ghost", "match": {"field": "any", "contains": "SHOP"},
            "category": "no-such/category"}])
check("a rule assigning a category that does not exist is reported",
      bool(has("rule:unknown-category")), str([f["check"] for f in findings()]))

set_rules([{"id": "fine", "match": {"field": "any", "contains": "REWE"}, "category": category_slug}])
check("a healthy rule set produces nothing", not has("rule:never-matches")
      and not has("rule:duplicate") and not has("rule:shadowed")
      and not has("rule:unknown-category"))
RULES_PATH.write_text(SAVED_RULES)


print("== rows nobody can account for")
rows = [dict(row) for row in store.load_year_by_file(YEAR)[source_name]]
rows[0] = dict(rows[0], source={})
store.rewrite_year(YEAR, {source_name: rows})
found = has("provenance:no-source")
check("a row with no source file is reported", bool(found), str([f["check"] for f in findings()]))
check("and named", found and rows[0]["id"] in found[0]["ids"], str(found[:1]))
store.rewrite_year(YEAR, {source_name: by_file[source_name]})
check("clean again", not has("provenance:no-source"))


print("== the format catalogue is audited with the same linter CI uses")
check("the shipped catalogue passes", not has("format:invalid-manifest"),
      str(has("format:invalid-manifest")[:1]))
# Deliberately NOT reported here: a best-guess format is a fact about the software, not
# this household's data, and it never clears. A doctor finding that cannot be resolved
# teaches people to scroll past the ones that can. It is surfaced in the import preview
# and in `formats-lint`, where it is actionable.
check("a best-guess format is not permanent noise in the health report",
      not [f for f in findings() if f["check"] == "format:best-guess"])
from pipeline import format_lint  # noqa: E402
check("but the linter still names it", "barclays-de" in format_lint.lint()["best_guess"],
      str(format_lint.lint()["best_guess"]))


print("== one bad file does not stop the audit")
transactions = tmp / "data" / str(YEAR) / "transactions"
(transactions / "zz-broken.jsonl").write_text('{"id": "x", not json\n', encoding="utf-8")
result = doctor.run(YEAR)
check("the unreadable file is reported",
      any(f["check"] in ("unreadable-file", "schema:unreadable") for f in result["findings"]),
      str([f["check"] for f in result["findings"]][:6]))
check("and the audit still returns a full report", isinstance(result["checked"], dict))
(transactions / "zz-broken.jsonl").unlink()


print("== read-only, and offline")
before = tree_hash()
doctor.run(YEAR)
doctor.run(None)
check("running the doctor writes nothing at all", before == tree_hash(),
      "an auditor that edits what it audits is not one")

import urllib.request  # noqa: E402
original_urlopen = urllib.request.urlopen


def refuse(*args, **kwargs):
    raise AssertionError("the doctor tried to reach the network")


urllib.request.urlopen = refuse
try:
    doctor.run(YEAR)
    check("and asks the network for nothing", True)
except AssertionError as exc:
    check("and asks the network for nothing", False, str(exc))
finally:
    urllib.request.urlopen = original_urlopen


print("== every finding is actionable")
sample = doctor.run(None)["findings"]
check("each finding carries a stable check id, severity and message",
      all(f.get("check") and f.get("severity") in ("error", "warning", "info") and f.get("message")
          for f in sample), str(sample[:1]))
check("and severities are only the three defined",
      {f["severity"] for f in sample} <= {"error", "warning", "info"})


# Anti-shrink guard: exact count at implementation time. May only ever be RAISED
# when checks are added — never lowered (see AGENTS.md: never weaken a test).
MIN_CHECKS = 29
check("suite did not shrink", total_checks >= MIN_CHECKS,
      "total_checks=%d < %d" % (total_checks, MIN_CHECKS))

shutil.rmtree(tmp)
print()
if failures:
    print("FAILED: %s" % ", ".join(failures))
    sys.exit(1)
print("All checks passed.")
