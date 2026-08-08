"""The gates that stop wrong money getting in, and stop right money getting lost.

Every check here corresponds to a defect that was live: a statement row that vanished
without appearing in any count, a settlement that created a cent out of a rounding,
a month lock that never matched the key it was asked about. They are grouped by the
thing they protect rather than by module, because that is what a reader needs to know
before changing one of them.

Usage: .venv/bin/python tests/test_integrity_gates.py
"""
import csv
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from sandbox import PROJECT, build_sandbox

tmp = Path(tempfile.mkdtemp(prefix="fa-gates-test-"))
os.environ["FA_ROOT"] = str(tmp)
build_sandbox(tmp)

sys.path.insert(0, str(PROJECT))
from fastapi import HTTPException  # noqa: E402
from pipeline import doctor, extraction, formats, ingest, settle, store  # noqa: E402
from pipeline.util import cents, parse_amount, write_json  # noqa: E402
from app import server  # noqa: E402

ingest.run(verbose=False)          # the fixture inbox, so the settlement checks have real years

failures = []
total_checks = 0


def check(name, cond, detail=""):
    global total_checks
    total_checks += 1
    print("  %s %s %s" % ("OK " if cond else "FAIL", name, detail if not cond else ""))
    if not cond:
        failures.append(name)


def raises(fn, wanted=Exception):
    try:
        fn()
    except wanted as exc:
        return str(exc)
    except Exception as exc:                       # the wrong exception is still a failure
        return "UNEXPECTED %s: %s" % (type(exc).__name__, exc)
    return None


def status_of(fn):
    try:
        fn()
    except HTTPException as exc:
        return exc.status_code
    return 200


def write_csv(path, rows, header=("date", "amount", "currency", "counterparty", "purpose",
                                  "counterparty_iban", "force_review")):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)
    return path


GOOD_ROW = ("2026-03-02", "-12.34", "EUR", "SHOP", "purchase", "", "false")
work = tmp / "gates"
work.mkdir()


# ---------------------------------------------------------------- parsing money
print("== a row that cannot be read is never quietly dropped")

# A valid date with an unreadable amount used to be skipped and not counted, so a
# statement could be imported missing a line while still looking complete.
path = write_csv(work / "bad-amount.csv", [GOOD_ROW, ("2026-03-03", "NOT_AN_AMOUNT", "EUR", "X", "y", "", "false")])
message = raises(lambda: formats.parse(path, formats.detect(path)), ValueError)
check("an unreadable amount refuses the whole file", bool(message) and "cannot be read as money" in message,
      str(message))

# An empty amount cell is a different thing: an informational row, not a lost transaction.
path = write_csv(work / "blank-amount.csv", [GOOD_ROW, ("2026-03-03", "", "EUR", "NOTE", "info row", "", "false")])
rows = formats.parse(path, formats.detect(path))
check("an empty amount cell is still just skipped", len(rows) == 1, str(rows))

check("NaN does not parse as money", parse_amount("NaN", "dot") is None)
check("Infinity does not parse as money", parse_amount("Infinity", "dot") is None)
check("-Infinity does not parse as money", parse_amount("-Infinity", "dot") is None)
check("ordinary money still parses", parse_amount("-1.234,56", "comma") == -1234.56)

path = write_csv(work / "nan.csv", [GOOD_ROW, ("2026-03-03", "NaN", "EUR", "X", "y", "", "false")])
check("a NaN amount refuses the file rather than entering the store",
      raises(lambda: formats.parse(path, formats.detect(path)), ValueError) is not None)

check("write_json refuses a non-finite number",
      raises(lambda: write_json(work / "nan.json", {"x": float("nan")}), ValueError) is not None)
check("and leaves no file behind when it does", not (work / "nan.json").exists())
check("cents() refuses a non-finite amount",
      raises(lambda: cents(float("inf")), ValueError) is not None)


# ---------------------------------------------------------------- year range
print("== a date no statement could carry is a mis-parse, not history")

path = write_csv(work / "year-one.csv", [("0001-01-01", "-5.00", "EUR", "X", "y", "", "false")])
message = raises(lambda: formats.parse(path, formats.detect(path)), ValueError)
check("year 0001 refuses the file (it would be written to data/1 and never read again)",
      bool(message) and "outside 1900-2999" in message, str(message))


# ---------------------------------------------------------------- extraction gate
print("== an extraction must prove itself, not assert itself")

csv_path = write_csv(work / "extract.csv", [GOOD_ROW])
honest = {"status": "ok", "opening_balance": 100.00, "closing_balance": 87.66,
          "sum_of_transactions": -12.34, "transactions_extracted": 1,
          "balance_anchors": [{"date": "2026-03-01", "balance": 100.00},
                              {"date": "2026-03-02", "balance": 87.66}]}
check("an honest report is admitted", extraction.admit(honest, csv_path)["total_cents"] == -1234)

for name, patch in [
    ("status is not ok", {"status": "failed"}),
    ("no opening balance", {"opening_balance": None}),
    ("no closing balance", {"closing_balance": None}),
    ("balances do not carry", {"closing_balance": 87.65}),
    ("the reconciled total disagrees with its own file", {"sum_of_transactions": -99.99}),
    ("count disagrees with its own file", {"transactions_extracted": 9}),
    ("anchors contradict the reconciliation",
     {"balance_anchors": [{"date": "2026-03-01", "balance": 1.0},
                          {"date": "2026-03-02", "balance": 2.0}]}),
    ("a non-finite balance", {"closing_balance": float("inf")}),
]:
    report = dict(honest, **patch)
    check("rejected: %s" % name,
          raises(lambda: extraction.admit(report, csv_path), extraction.ExtractionRejected) is not None)

check("a report about a file that no longer matches it is rejected",
      raises(lambda: extraction.admit(honest, write_csv(work / "changed.csv", [GOOD_ROW, GOOD_ROW])),
             extraction.ExtractionRejected) is not None)
check("a mixed-currency file cannot be reconciled against one pair of balances",
      raises(lambda: extraction.admit(honest, write_csv(
          work / "mixed.csv", [GOOD_ROW, ("2026-03-02", "-1.00", "BRL", "X", "y", "", "false")])),
          extraction.ExtractionRejected) is not None)
check("storable() keeps a rejected report writable", json.dumps(
    extraction.storable({"closing_balance": float("nan"), "issues": [float("inf")]})) is not None)

# The skill's own output names itself, and may not be imported on trust alone.
extracted = write_csv(tmp / "inbox" / "bank1-person1__march.extracted.csv", [GOOD_ROW])
message = raises(lambda: ingest._admit_extracted(extracted), ValueError)
check("an .extracted.csv without its report is refused",
      bool(message) and "reconciliation report" in message, str(message))
write_json(ingest.report_path_for(extracted), honest)
check("and admitted once the report is there and holds up",
      ingest._admit_extracted(extracted)["rows"] == 1)
write_json(ingest.report_path_for(extracted), dict(honest, closing_balance=1.0))
check("but not when the report does not hold up",
      raises(lambda: ingest._admit_extracted(extracted), extraction.ExtractionRejected) is not None)
extracted.unlink()
ingest.report_path_for(extracted).unlink()

# A plain bank CSV is not an extraction and is not asked for a report.
plain = write_csv(work / "plain.csv", [GOOD_ROW])
check("an ordinary CSV needs no report", ingest._admit_extracted(plain) is None)


# ---------------------------------------------------------------- settlement cents
print("== settlement conserves every cent it moves")


def settle_probe(entries, ratio=None, people=("person1", "person2")):
    """Run the settlement arithmetic over hand-built entries, without a store."""
    total = 0
    paid = {p: 0 for p in people}
    half = {p: 1 for p in people}
    for amount_cents, payer in entries:
        if payer == "couple":
            for p, share in settle.allocate_cents(amount_cents, half, list(people)).items():
                paid[p] += share
        else:
            paid[payer] += amount_cents
        total += amount_cents
    fair = settle.allocate_cents(total, ratio or {p: 1 for p in people}, list(people))
    return total, paid, fair


total, paid, fair = settle_probe([(1, "couple")])
check("one cent paid from a joint account stays one cent", sum(paid.values()) == 1, str(paid))
check("and one cent of fair share, not two", sum(fair.values()) == 1, str(fair))
check("so the two balances cancel exactly",
      sum(paid[p] - fair[p] for p in paid) == 0, str((paid, fair)))

total, paid, fair = settle_probe([(1000, "person1")], ratio={"person1": 1 / 3, "person2": 2 / 3})
check("a third of ten euros loses nothing", sum(fair.values()) == 1000, str(fair))

total, paid, fair = settle_probe([(-5, "couple")])
check("a negative total allocates without inventing a cent", sum(paid.values()) == -5, str(paid))

check("allocation is deterministic across runs",
      settle.allocate_cents(101, {"a": 1, "b": 1}, ["a", "b"])
      == settle.allocate_cents(101, {"a": 1, "b": 1}, ["a", "b"]))
check("and a zero total splits into nothing",
      settle.allocate_cents(0, {"a": 1, "b": 3}, ["a", "b"]) == {"a": 0, "b": 0})

for year in store.years():
    result = settle.settlement(year)
    paid_total = cents(sum(result["paid"].values()))
    fair_total = cents(sum(result["fair_share"].values()))
    shared = cents(result["total_shared_expenses"])
    check("%d: what was paid adds up to the shared total" % year, paid_total == shared,
          "%d vs %d" % (paid_total, shared))
    check("%d: fair shares add up to the shared total" % year, fair_total == shared,
          "%d vs %d" % (fair_total, shared))
    check("%d: the two balances cancel" % year, cents(sum(result["balances"].values())) == 0)
    owed = max(cents(v) for v in result["balances"].values())
    check("%d: the transfer is exactly what is owed, and absent when nothing is" % year,
          cents(result["transfer"]["amount"]) == owed if result["transfer"] else owed <= 0,
          str(result["transfer"]))


# ---------------------------------------------------------------- ratios stay ratios
print("== a share of the costs is a fraction of a whole")

people = server._people()
base = dict(json.loads((tmp / "config.json").read_text()))


def settings_payload(**over):
    payload = {
        "person_labels": base.get("person_labels", {p: p.title() for p in people}),
        "reference_ratio": {people[0]: 0.6, people[1]: 0.4},
        "items_threshold_eur": base.get("items_threshold_eur", 50),
        "transfer_match_window_days": base.get("transfer_match_window_days", 3),
        "transfer_match_tolerance_cents": base.get("transfer_match_tolerance_cents", 0),
        "currencies": base.get("currencies", ["EUR"]),
    }
    payload.update(over)
    return server.SettingsUpdate(**payload)


check("a negative reference share is refused even when the pair sums to one",
      status_of(lambda: server.settings_update(
          settings_payload(reference_ratio={people[0]: -0.2, people[1]: 1.2}))) == 400)
check("a share above 100% is refused too",
      status_of(lambda: server.settings_update(
          settings_payload(reference_ratio={people[0]: 1.5, people[1]: -0.5}))) == 400)
check("an ordinary split is still accepted",
      status_of(lambda: server.settings_update(settings_payload())) == 200)

# The form is not the only way a config arrives — a hand edit and a restored backup both
# skip it, and settlement falls back to this ratio whenever there is no salary to derive
# one from.
from pipeline.util import ConfigError, load_config  # noqa: E402
config_path = tmp / "config.json"
saved = config_path.read_text()
for name, bad in [("a negative share", {people[0]: -0.2, people[1]: 1.2}),
                  ("shares that do not sum to one", {people[0]: 0.5, people[1]: 0.4}),
                  ("a share that is not a number", {people[0]: "half", people[1]: 0.5})]:
    config_path.write_text(json.dumps(dict(json.loads(saved), reference_ratio=bad)))
    check("load_config refuses %s" % name, raises(load_config, ConfigError) is not None)
config_path.write_text(saved)
check("and still loads the real one", isinstance(load_config()["reference_ratio"], dict))

# A negative annual salary total is a booking error. It used to produce ratios of -11% and
# 111% and a transfer larger than the whole shared cost, with a sentence of warning attached
# to a number nobody could tell was nonsense.
ratio_cats = sorted(settle.ratio_income_categories())
probe_year = store.years()[0]
salary_cat = ratio_cats[0]
before = store.decisions(probe_year)
raw = store.load_year_raw(probe_year)[0]
reversal = dict(raw, id="gates-negative-salary", amount_eur=-99999.0, amount_original=-99999.0,
                source={"file": "gates.jsonl", "format": "test"})
store.append_transactions(probe_year, "gates-negative-salary", [reversal])
store.save_decisions(probe_year, dict(before, **{
    reversal["id"]: {"category": salary_cat, "income_owner": people[0], "sharing": "shared"}}))
broken = settle.settlement(probe_year)
check("negative salary income does not produce a ratio outside 0-100%",
      all(0 <= broken["ratio"][p] <= 1 for p in people), str(broken["ratio"]))
check("and the settlement says so instead of quietly using the broken figure",
      bool(broken["ratio_problem"]) and "reference ratio" in broken["ratio_source"],
      str(broken["ratio_source"]))
check("it still conserves cents while it is standing on bad data",
      cents(sum(broken["balances"].values())) == 0)
(tmp / "data" / str(probe_year) / "transactions" / "gates-negative-salary.jsonl").unlink()
store.save_decisions(probe_year, before)
check("and a healthy year reports no ratio problem",
      settle.settlement(probe_year)["ratio_problem"] is None)


# ---------------------------------------------------------------- the month lock
print("== a closed month rejects the ratio that would resettle it")

year = store.years()[0] if store.years() else 2026
months = store.months_state(year)
months["%d-03" % year] = "closed"
store.save_months_state(year, months)
ratio = {people[0]: 0.5, people[1]: 0.5}

check("a monthly override on a closed month is refused",
      status_of(lambda: server.set_ratio_override(
          server.RatioOverride(year=year, key="3", ratio=ratio))) == 409)
check("clearing one is refused as well",
      status_of(lambda: server.set_ratio_override(
          server.RatioOverride(year=year, key="3", ratio=None))) == 409)
check("an annual override is refused while any month is closed",
      status_of(lambda: server.set_ratio_override(
          server.RatioOverride(year=year, key="annual", ratio=ratio))) == 409)
check("an open month still accepts one",
      status_of(lambda: server.set_ratio_override(
          server.RatioOverride(year=year, key="4", ratio=ratio))) == 200)
check("a key that is neither 'annual' nor a month is refused, not stored and ignored",
      status_of(lambda: server.set_ratio_override(
          server.RatioOverride(year=year, key="13", ratio=ratio))) == 400)
check("and so is a key that is not a number at all",
      status_of(lambda: server.set_ratio_override(
          server.RatioOverride(year=year, key="march", ratio=ratio))) == 400)
overrides = settle.ratio_overrides(year)
check("the override that was accepted is readable under the key settlement looks for",
      settle.ratio_override(year, 4, people) is not None, str(overrides))
server.set_ratio_override(server.RatioOverride(year=year, key="4", ratio=None))
months.pop("%d-03" % year, None)
store.save_months_state(year, months)


# ---------------------------------------------------------------- rule creation
print("== a rule is retroactive, so creating one is validated like changing one")

check("an empty pattern is refused (it matches every transaction ever imported)",
      status_of(lambda: server.add_rule(server.RulePayload(pattern="", category=None))) == 400)
check("a two-character pattern is refused",
      status_of(lambda: server.add_rule(server.RulePayload(pattern="ab"))) == 400)
check("an unknown match field is refused",
      status_of(lambda: server.add_rule(server.RulePayload(pattern="REWE", field="anything"))) == 400)
check("an unknown category is refused",
      status_of(lambda: server.add_rule(server.RulePayload(pattern="REWE", category="no/such"))) == 400)
check("an invented sharing value is refused",
      status_of(lambda: server.add_rule(server.RulePayload(pattern="REWE", sharing="whatever"))) == 400)
check("an unknown tax bucket is refused",
      status_of(lambda: server.add_rule(server.RulePayload(pattern="REWE", tax_bucket="nope"))) == 400)
check("an unknown action is refused",
      status_of(lambda: server.add_rule(server.RulePayload(pattern="REWE", action="delete"))) == 400)
valid_category = sorted(server._decision_options()[0] - {"auto:items"})[0]
check("a valid rule is still created",
      status_of(lambda: server.add_rule(
          server.RulePayload(pattern="GATES TEST MERCHANT", category=valid_category))) == 200)


# ---------------------------------------------------------------- backup parts
print("== a selection means what it says")

check("no selection still means everything",
      set(server._selected_parts("")) == set(server.BACKUP_PARTS))
check("a real selection is honoured", server._selected_parts("data,rules") == ["data", "rules"])
check("a typo is an error rather than silently widening to everything",
      status_of(lambda: server._selected_parts("dta")) == 400)
check("one bad token spoils the selection rather than being dropped",
      status_of(lambda: server._selected_parts("data,typo")) == 400)


# ---------------------------------------------------------------- doctor
print("== the doctor sees what it claims to check")

dup_year = store.years()[0]
dup_dir = tmp / "data" / str(dup_year) / "transactions"
row = json.loads(sorted(dup_dir.glob("*.jsonl"))[0].read_text().splitlines()[0])
line = json.dumps(row, ensure_ascii=False)
(dup_dir / "gates-dupe.jsonl").write_text(line + "\n" + line + "\n", encoding="utf-8")
findings = doctor.run(dup_year)["findings"]
dupes = [f for f in findings if f["check"] == "duplicate-id"]
check("two identical rows in ONE file are a duplicate, not a clean bill of health",
      any(row["id"] in f["ids"] for f in dupes), str([f["message"] for f in dupes]))
(dup_dir / "gates-dupe.jsonl").unlink()

stray = tmp / "data" / "1" / "transactions"
stray.mkdir(parents=True, exist_ok=True)
(stray / "lost.jsonl").write_text(line + "\n", encoding="utf-8")
findings = doctor.run(dup_year)["findings"]
check("a year directory nothing scans is reported rather than left invisible",
      any(f["check"] == "unscanned-year-dir" for f in findings))
shutil.rmtree(tmp / "data" / "1")

decisions = store.decisions(dup_year)
target = row["id"]
other_account = next(a for a in server.load_accounts()[0] if a != row["account"])
store.save_decisions(dup_year, dict(decisions, **{target: {"account": other_account}}))
findings = doctor.run(dup_year)["findings"]
check("a decision that moves a transaction to another account is reported, because "
      "settlement follows it and balance reconciliation does not",
      any(f["check"] == "decision-account-reassignment" and target in f["ids"] for f in findings),
      str([f["check"] for f in findings]))
store.save_decisions(dup_year, decisions)
check("and a decision that does not move it is not reported",
      not any(f["check"] == "decision-account-reassignment"
              for f in doctor.run(dup_year)["findings"]))


# ---------------------------------------------------------------- extractor completeness
print("== a self-derived balance chain is not proof of completeness")

sys.path.insert(0, str(PROJECT / "scripts"))
from scripts.banco_rendimento import extract as rendimento  # noqa: E402

full = [
    {"date": "2025-12-29", "order": (1, 10), "amount_cents": 10000, "balance_cents": 110000,
     "counterparty": "first"},
    {"date": "2025-12-29", "order": (1, 20), "amount_cents": 18000, "balance_cents": 128000,
     "counterparty": "second"},
]
closes = [{"page": 1, "date": "2025-12-29", "balance_cents": 128000}]
check("a complete day passes its printed close", rendimento.verify_checkpoints(full, closes) == [])
check("dropping the last row of a day is caught by that day's printed close",
      rendimento.verify_checkpoints(full[:1], closes) != [])
check("a printed close for a day we read nothing from is caught",
      rendimento.verify_checkpoints(
          full, closes + [{"page": 1, "date": "2025-12-28", "balance_cents": 100000}]) != [])
check("a day with rows but no printed close cannot be checked, and says so",
      rendimento.verify_checkpoints(
          full + [{"date": "2025-12-30", "order": (1, 30), "amount_cents": 100,
                   "balance_cents": 128100, "counterparty": "third"}], closes) != [])


# Anti-shrink guard: exact count at implementation time. May only ever be RAISED
# when checks are added — never lowered (see AGENTS.md: never weaken a test).
MIN_CHECKS = 76
check("suite did not shrink", total_checks >= MIN_CHECKS,
      "total_checks=%d < %d" % (total_checks, MIN_CHECKS))

shutil.rmtree(tmp)
print()
if failures:
    print("FAILED: %s" % ", ".join(failures))
    sys.exit(1)
print("All checks passed.")
