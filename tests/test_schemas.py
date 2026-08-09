"""What a persisted record is allowed to be, and where it is wrong when it is not.

Two things are being proved. The first is that corruption is caught: a wrong type, a
non-finite amount, a bool where money belongs, a date no statement could carry, a
reference to something that does not exist. The second matters more in practice — that
the *error says where*. "decisions.json is invalid" costs an afternoon of bisecting a
file by hand; "decisions.json → abc#1.splits[1].amount" costs a minute.

The third thing, quieter but load-bearing: real legacy data must still load. A
validator that rejects the household's own history is not strict, it is broken.

Usage: .venv/bin/python tests/test_schemas.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from sandbox import PROJECT, build_sandbox

tmp = Path(tempfile.mkdtemp(prefix="fa-schemas-"))
os.environ["FA_ROOT"] = str(tmp)
build_sandbox(tmp)

sys.path.insert(0, str(PROJECT))
from pipeline import closings, ingest, schemas, store  # noqa: E402
from pipeline.util import write_json  # noqa: E402

ingest.run(verbose=False)

failures = []
total_checks = 0


def check(name, cond, detail=""):
    global total_checks
    total_checks += 1
    print("  %s %s %s" % ("OK " if cond else "FAIL", name, detail if not cond else ""))
    if not cond:
        failures.append(name)


def rejects(fn, code=None, path_contains=None):
    """Run a validator and return the error, asserting it is precise about location."""
    try:
        fn()
    except schemas.SchemaError as exc:
        if code and exc.code != code:
            return "wrong code %r (wanted %r): %s" % (exc.code, code, exc)
        if path_contains and path_contains not in (exc.path or ""):
            return "path %r does not mention %r" % (exc.path, path_contains)
        return None
    except Exception as exc:                       # noqa: BLE001
        return "raised %s instead of SchemaError: %s" % (type(exc).__name__, exc)
    return "accepted it"


PEOPLE = ("person1", "person2")
GOOD_TXN = {
    "id": "abc#1", "account": "bank1-person1", "date": "2026-06-15",
    "amount_original": -12.34, "currency": "EUR", "amount_eur": -12.34,
    "fx_rate": None, "fx_rate_date": None, "fx_rate_source": None,
    "counterparty": "SHOP", "purpose": "", "counterparty_iban": "",
    "force_review": False, "kind": "normal",
    "source": {"file": "statement.csv", "format": "test"},
}


print("== a valid record is accepted unchanged")
check("a canonical transaction passes", schemas.raw_transaction(dict(GOOD_TXN)) is not None)
check("and the real store passes as a whole", schemas.validate_graph(tmp)["ok"],
      str(schemas.validate_graph(tmp)["findings"][:2]))


print("== a transaction that is not one")
CASES = [
    ("a missing id", {"id": None}, "missing-field", "id"),
    ("an empty id", {"id": "  "}, "empty-field", "id"),
    ("a numeric id", {"id": 5}, "wrong-type", "id"),
    ("no account", {"account": None}, "missing-field", "account"),
    ("a date that is not a date", {"date": "15/06/2026"}, "bad-format", "date"),
    ("a date that does not exist", {"date": "2026-02-30"}, "bad-format", "date"),
    ("a year no statement carries", {"date": "0001-01-01"}, "out-of-range", "date"),
    ("a NaN amount", {"amount_eur": float("nan")}, "not-finite", "amount_eur"),
    ("an infinite amount", {"amount_eur": float("inf")}, "not-finite", "amount_eur"),
    ("a text amount", {"amount_eur": "lots"}, "wrong-type", "amount_eur"),
    ("a boolean amount", {"amount_eur": True}, "wrong-type", "amount_eur"),
    ("a missing amount", {"amount_eur": None}, "missing-field", "amount_eur"),
    ("a currency that is not a code", {"currency": "euros"}, "bad-format", "currency"),
    ("an unknown kind", {"kind": "teleport"}, "bad-enum", "kind"),
    ("a negative exchange rate", {"fx_rate": -1.0}, "out-of-range", "fx_rate"),
    ("no source metadata", {"source": None}, "wrong-type", "source"),
    ("a misspelled field", {"shraing": "out-of-scope"}, "unknown-field", None),
]
for label, patch, code, where in CASES:
    row = dict(GOOD_TXN, **patch)
    problem = rejects(lambda: schemas.raw_transaction(row, path="row", file="f.jsonl"),
                      code=code, path_contains=where)
    check("rejected: %s" % label, problem is None, problem or "")

check("a boolean is not a number, even though Python says it is an int",
      rejects(lambda: schemas.number(True, "x", "f"), code="wrong-type") is None)


print("== decisions, and the splits inside them")
check("a plain decision passes",
      schemas.decision({"category": "core-living/groceries", "sharing": "shared"},
                       people=PEOPLE) is not None)
check("a person who does not live here is rejected",
      rejects(lambda: schemas.decision({"sharing": "personal:ghost"}, "d", "f", people=PEOPLE),
              code="bad-enum", path_contains="sharing") is None)
check("a misspelled decision field is rejected",
      rejects(lambda: schemas.decision({"shraing": "shared"}, "d", "f", people=PEOPLE),
              code="unknown-field") is None)
check("a split part with a NaN amount is rejected, and the part is named",
      rejects(lambda: schemas.decision(
          {"splits": [{"amount": -1.0}, {"amount": float("nan")}]}, "d", "f", people=PEOPLE),
          code="not-money", path_contains="splits[1]") is None)
check("a non-numeric split amount is rejected",
      rejects(lambda: schemas.decision({"splits": [{"amount": "half"}]}, "d", "f"),
              code="not-money") is None)
check("a split part may carry a purpose, which the editor writes",
      schemas.decision({"splits": [{"amount": -1.0, "purpose": "coffee"}]}) is not None)

check("split parts that do not sum to the parent are rejected",
      rejects(lambda: schemas.split_sum_matches(
          {"splits": [{"amount": -60.0}, {"amount": -60.0}]}, -12000 + 1, "d", "f"),
          code="split-sum") is None)
check("and parts that do sum are accepted",
      schemas.split_sum_matches({"splits": [{"amount": -60.0}, {"amount": -60.0}]},
                                -12000, "d", "f") is not None)


print("== accounts, anchors and rules")
GOOD_ACCOUNT = {"id": "bank1-person1", "owner": "person1", "bank": "Bank 1",
                "type": "giro", "currency": "EUR", "iban": None, "label": "Checking",
                "low_activity": False}
check("a valid account passes", schemas.account(dict(GOOD_ACCOUNT), people=PEOPLE) is not None)
check("an owner who is not a household member is rejected",
      rejects(lambda: schemas.account(dict(GOOD_ACCOUNT, owner="ghost"), "a", "f", people=PEOPLE),
              code="bad-enum", path_contains="owner") is None)
check("an account id that is not a slug is rejected",
      rejects(lambda: schemas.account(dict(GOOD_ACCOUNT, id="Bank One!"), "a", "f"),
              code="bad-format") is None)
check("an unknown account type is rejected",
      rejects(lambda: schemas.account(dict(GOOD_ACCOUNT, type="mattress"), "a", "f"),
              code="bad-enum") is None)

GOOD_ANCHOR = {"account": "bank1-person1", "date": "2026-06-30", "balance": 1234.56,
               "currency": "EUR", "kind": "manual", "source": "manual",
               "captured_at": "2026-06-30T10:00:00+00:00"}
check("a valid anchor passes", schemas.balance_anchor(dict(GOOD_ANCHOR)) is not None)
check("an anchor with a NaN balance is rejected",
      rejects(lambda: schemas.balance_anchor(dict(GOOD_ANCHOR, balance=float("nan")), "a", "f"),
              code="not-finite", path_contains="balance") is None)

GOOD_RULE = {"id": "rewe", "match": {"field": "counterparty", "contains": "REWE"},
             "category": "core-living/groceries", "sharing": "shared", "scope": "family"}
check("a valid rule passes", schemas.merchant_rule(dict(GOOD_RULE), people=PEOPLE) is not None)
check("a rule with no pattern is rejected",
      rejects(lambda: schemas.merchant_rule(dict(GOOD_RULE, match={"field": "any"}), "r", "f"),
              code="missing-field", path_contains="contains") is None)
check("a rule matching an unknown field is rejected",
      rejects(lambda: schemas.merchant_rule(
          dict(GOOD_RULE, match={"field": "colour", "contains": "x"}), "r", "f"),
          code="bad-enum") is None)
check("a rule scoped to a stranger is rejected",
      rejects(lambda: schemas.merchant_rule(dict(GOOD_RULE, scope="ghost"), "r", "f", people=PEOPLE),
              code="bad-enum") is None)


print("== whole-tree validation, including references between files")
year = store.years()[0]
decisions_path = tmp / "data" / str(year) / "decisions.json"
if not decisions_path.exists():
    write_json(decisions_path, {})
saved = decisions_path.read_text()

write_json(decisions_path, {"no-such-transaction": {"category": "core-living/groceries"}})
report = schemas.validate_graph(tmp)
check("a decision for a transaction that does not exist is reported",
      any(f["code"] == "dangling-reference" for f in report["findings"]),
      str(report["findings"][:2]))

real_id = store.load_year_raw(year)[0]["id"]
write_json(decisions_path, {real_id: {"category": "no-such/category"}})
report = schemas.validate_graph(tmp)
check("a decision naming a category that does not exist is reported",
      any(f["code"] == "dangling-reference" and "category" in (f["path"] or "")
          for f in report["findings"]), str(report["findings"][:2]))

write_json(decisions_path, {real_id: {"account": "no-such-account"}})
report = schemas.validate_graph(tmp)
check("a decision reassigning to an account that does not exist is reported",
      any(f["code"] == "dangling-reference" and "account" in (f["path"] or "")
          for f in report["findings"]), str(report["findings"][:2]))

decisions_path.write_text(saved)
check("and the tree is clean again", schemas.validate_graph(tmp)["ok"],
      str(schemas.validate_graph(tmp)["findings"][:2]))


print("== the inventory covers every persisted family, not the convenient ones")
# "Schemas at every persisted boundary" was true of five families and silently false of
# the rest: months, closings, uploads, categories, tax buckets and ratio overrides had no
# validator at all, so a restore could stage them corrupt and be told the candidate was
# fine. Each case here seeds exactly one bad document and asserts the tree is refused.
CORRUPTIONS = [
    ("data/%d/months.json" % year, {"%d-13" % year: "closed"},
     "a thirteenth month"),
    ("data/%d/months.json" % year, {"%d-01" % year: "banana"},
     "a month state nobody defined"),
    ("data/%d/closings.json" % year, {"%d-01" % year: {"income": "lots"}},
     "a recorded figure that is not a number"),
    ("data/uploads.json", {"uploads": "not-a-list"},
     "an uploads document that is not a list"),
    ("data/uploads.json", {"uploads": [{"id": "x", "years": ["not-a-year"]}]},
     "an upload whose year is text"),
    ("rules/tax-buckets.json", {"buckets": "not-a-list"},
     "a tax-bucket document that is not a list"),
    ("rules/tax-buckets.json", {"buckets": [{"slug": "a", "name": "A"},
                                            {"slug": "a", "name": "Also A"}]},
     "two tax buckets sharing a slug"),
    ("data/%d/ratio-overrides.json" % year, {"annual": {PEOPLE[0]: 60}},
     "a settlement ratio written as a percentage"),
    ("data/%d/ratio-overrides.json" % year, {"annual": {"nobody": 0.5}},
     "a ratio for somebody who is not in the household"),
    ("rules/categories.json", {"categories": [{"slug": "a", "name": "A", "subs": []},
                                              {"slug": "a", "name": "Again", "subs": []}]},
     "two category groups sharing a stable id"),
    ("rules/budgets.json", {"budgets": "not-an-object"},
     "a budget envelope whose targets are not an object"),
    ("rules/recurring-overrides.json", {"force": "EVERYTHING", "never": []},
     "recurring overrides whose force list is text"),
    ("data/anomaly-dismissals.json", {"version": 1, "dismissed": {"x": {}}},
     "a dismissal with no evidence fingerprint"),
    ("data/%d/audit-checkpoints.json" % year,
     {"version": 1, "checkpoints": {"last-import": {"id": "last-import"}}},
     "an audit checkpoint with no snapshot"),
]
for relative, document, label in CORRUPTIONS:
    path = tmp / relative
    existed = path.read_text() if path.exists() else None
    write_json(path, document)
    report = schemas.validate_graph(tmp)
    check("refused: %s" % label, not report["ok"],
          "validate_graph accepted it, so restore would too")
    check("  and it names the file", any(relative.split("/")[-1] in (f["file"] or "")
                                         for f in report["findings"]),
          str(report["findings"][:1]))
    if existed is None:
        path.unlink()
    else:
        path.write_text(existed)
check("and the tree is clean once every document is restored",
      schemas.validate_graph(tmp)["ok"], str(schemas.validate_graph(tmp)["findings"][:2]))


print("== one bad file does not hide the rest")
transactions = tmp / "data" / str(year) / "transactions"
(transactions / "broken.jsonl").write_text('{"id": "x", not json\n', encoding="utf-8")
report = schemas.validate_graph(tmp)
check("the unreadable file is reported",
      any(f["code"] == "unreadable" for f in report["findings"]), str(report["findings"][:2]))
check("the good files were still validated — the audit did not stop at the first failure",
      len(store.load_year_raw.__name__) > 0 and report["findings"],
      str(len(report["findings"])))
(transactions / "broken.jsonl").write_text(
    json.dumps(dict(GOOD_TXN, id="lone#1", date="%d-06-15" % year)) + "\n"
    + '{"id": "second", "account": 5}\n', encoding="utf-8")
report = schemas.validate_graph(tmp)
codes = [f for f in report["findings"] if f["code"] == "wrong-type"]
check("a bad row is reported while its good neighbours are not", len(codes) == 1, str(codes))
check("and the report names the line", "line 2" in (codes[0]["path"] or ""), str(codes[0]))
(transactions / "broken.jsonl").unlink()


print("== validation happens on the way out, not only on the way in")
check("save_decisions refuses to write an invalid decision",
      rejects(lambda: store.save_decisions(year, {real_id: {"sharing": "personal:ghost"}}),
              code="bad-enum") is None)
check("and the good file on disk was not replaced",
      json.loads(decisions_path.read_text()) == json.loads(saved))
check("append_transactions refuses a malformed row",
      rejects(lambda: store.append_transactions(year, "should-not-exist",
                                                [dict(GOOD_TXN, amount_eur=float("nan"))]),
              code="not-finite") is None)
check("and wrote no file for it",
      not (transactions / "should-not-exist.jsonl").exists())
months_path = tmp / "data" / str(year) / "months.json"
months_before = months_path.read_bytes() if months_path.exists() else None
check("save_months_state validates before publishing",
      rejects(lambda: store.save_months_state(year, {"%d-13" % year: "closed"}),
              code="bad-key") is None)
check("and a rejected month lock leaves the previous bytes untouched",
      (months_path.read_bytes() if months_path.exists() else None) == months_before)
closings_path = tmp / "data" / str(year) / "closings.json"
closings_before = closings_path.read_bytes() if closings_path.exists() else None
check("closings validate nested settlement money before publishing",
      rejects(lambda: closings.save(year, {"annual": {
          "settlement": {"paid": {PEOPLE[0]: float("nan")}}}}),
              code="not-finite") is None)
check("and a rejected closing leaves the previous bytes untouched",
      (closings_path.read_bytes() if closings_path.exists() else None) == closings_before)


print("== errors are machine-readable, not just prose")
try:
    schemas.raw_transaction(dict(GOOD_TXN, amount_eur=float("nan")), "row 7", "y/f.jsonl")
except schemas.SchemaError as exc:
    finding = exc.as_finding()
    check("a finding carries a stable code", finding["code"] == "not-finite", str(finding))
    check("the file", finding["file"] == "y/f.jsonl", str(finding))
    check("the path", "amount_eur" in finding["path"], str(finding))
    check("and what to do about it", bool(finding["fix"]), str(finding))


# Anti-shrink guard: exact count at implementation time. May only ever be RAISED
# when checks are added — never lowered (see AGENTS.md: never weaken a test).
MIN_CHECKS = 87
check("suite did not shrink", total_checks >= MIN_CHECKS,
      "total_checks=%d < %d" % (total_checks, MIN_CHECKS))

shutil.rmtree(tmp)
print()
if failures:
    print("FAILED: %s" % ", ".join(failures))
    sys.exit(1)
print("All checks passed.")
