"""Suggestions that earn their place, and — mostly — the ones that must not appear.

A heuristic feature is judged by its false positives. A review list that calls ordinary
purchases suspicious gets ignored within a week, and an ignored list is worse than none
because it hides the real signals too. So this file is deliberately lopsided: for every
detector there is one scenario that must fire and several that must not, and the ones
that must not are the point.

It also pins the three promises the module makes: it writes nothing to the ledger, it
says the same thing twice given the same `as_of`, and a dismissal expires when the
evidence behind it changes.

Usage: .venv/bin/python tests/test_anomalies.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from sandbox import PROJECT, build_sandbox

tmp = Path(tempfile.mkdtemp(prefix="fa-anomalies-"))
os.environ["FA_ROOT"] = str(tmp)
build_sandbox(tmp)

sys.path.insert(0, str(PROJECT))
from fastapi import HTTPException  # noqa: E402
from pipeline import anomalies, store  # noqa: E402
from pipeline.util import write_json  # noqa: E402
from app import server  # noqa: E402

failures = []
total_checks = 0


def check(name, cond, detail=""):
    global total_checks
    total_checks += 1
    print("  %s %s %s" % ("OK " if cond else "FAIL", name, detail if not cond else ""))
    if not cond:
        failures.append(name)


YEAR = 2026
AS_OF = "2026-12-31"
ACCOUNT = "bank1-person1"
OTHER = "bank2-person2"


def txn(txn_id, date, amount, counterparty="SHOP", account=ACCOUNT, currency="EUR", **extra):
    record = {
        "id": txn_id, "account": account, "date": date,
        "amount_original": amount, "currency": currency, "amount_eur": amount,
        "fx_rate": None, "fx_rate_date": None, "fx_rate_source": None,
        "counterparty": counterparty, "purpose": "", "counterparty_iban": "",
        "kind": "normal", "source": {"file": "fixture.csv", "format": "test"},
    }
    record.update(extra)
    return record


def scan(rows, decisions=None, **kwargs):
    store.rewrite_year(YEAR, {"fixture.jsonl": rows})
    store.save_decisions(YEAR, decisions or {})
    return anomalies.scan(YEAR, as_of=kwargs.pop("as_of", AS_OF), **kwargs)


def _status(fn):
    try:
        fn()
    except HTTPException as exc:
        return exc.status_code
    return 200


def checks_in(result, name):
    return [item for item in result["items"] if item["check"] == name]


def series(merchant, amounts, start_day=1, account=ACCOUNT, prefix="h"):
    """A merchant's ordinary history: one charge a month, same shape every time."""
    return [txn("%s%d" % (prefix, i), "%d-%02d-%02d" % (YEAR, i + 1, start_day),
                amount, merchant, account)
            for i, amount in enumerate(amounts)]


print("== the same charge booked twice")
same_day = [txn("d1", "2026-03-02", -12.99, "KIOSK"), txn("d2", "2026-03-02", -12.99, "KIOSK")]
found = checks_in(scan(same_day), "exact-duplicate")
check("two identical same-day charges are suggested", len(found) == 1, str(found))
check("both rows are named", found and sorted(found[0]["transaction_ids"]) == ["d1", "d2"])
check("the wording suggests rather than asserts", found and "Possible duplicate" in found[0]["message"],
      found[0]["message"] if found else "")
check("neither row was changed",
      all(t["amount_eur"] == -12.99 for t in store.load_year_raw(YEAR)))
check("and neither was removed", len(store.load_year_raw(YEAR)) == 2)

check("the same amount at a DIFFERENT merchant is not a duplicate",
      not checks_in(scan([txn("x1", "2026-03-02", -12.99, "KIOSK"),
                          txn("x2", "2026-03-02", -12.99, "BAKERY")]), "exact-duplicate"))
check("the same merchant and day at a different amount is not a duplicate",
      not checks_in(scan([txn("x3", "2026-03-02", -12.99, "KIOSK"),
                          txn("x4", "2026-03-02", -13.99, "KIOSK")]), "exact-duplicate"))
check("the same charge on two different accounts is not a duplicate",
      not checks_in(scan([txn("x5", "2026-03-02", -12.99, "KIOSK"),
                          txn("x6", "2026-03-02", -12.99, "KIOSK", account=OTHER)]),
                    "exact-duplicate"))
# The counterparty text is compared raw. Digit-stripping normalization groups a
# merchant across time, and applying it here made ten Brazilian instalments of one
# purchase look like ten identical charges — confidently.
instalments = [txn("i%d" % n, "2026-03-02", -50.00, "DELL - Parcela %d/12" % n) for n in range(1, 11)]
check("instalments of one purchase are not ten duplicates",
      not checks_in(scan(instalments), "exact-duplicate"),
      str([i["message"] for i in checks_in(scan(instalments), "exact-duplicate")]))

print("== the same amount a day or two apart")
near = [txn("n1", "2026-03-02", -9.99, "STREAM"), txn("n2", "2026-03-04", -9.99, "STREAM")]
found = checks_in(scan(near), "near-duplicate")
check("two days apart is suggested", len(found) == 1, str(found))
check("but only at medium confidence — repeat purchases are ordinary",
      found and found[0]["confidence"] == "medium")
check("a week apart is not suggested",
      not checks_in(scan([txn("n3", "2026-03-02", -9.99, "STREAM"),
                          txn("n4", "2026-03-12", -9.99, "STREAM")]), "near-duplicate"))
check("an exact same-day duplicate is not also reported as a near duplicate",
      not checks_in(scan(same_day), "near-duplicate"))

print("== an amount unlike this merchant's others")
history = series("GROCER", [-40.00, -42.00, -38.00, -41.00, -39.00, -43.00])
spike = history + [txn("s1", "2026-07-01", -400.00, "GROCER")]
found = checks_in(scan(spike), "amount-spike")
check("a ten-fold charge is suggested", len(found) == 1, str([f["message"] for f in found]))
check("the evidence shows the normal range, not just a verdict",
      found and {"median_cents", "mad_cents", "sample_size",
                 "normal_low_cents", "normal_high_cents"} <= set(found[0]["evidence"]),
      str(found[0]["evidence"]) if found else "")
check("with fewer than six prior charges nothing is judged",
      not checks_in(scan(series("GROCER", [-40.00, -42.00, -38.00, -41.00, -39.00])
                         + [txn("s2", "2026-07-01", -400.00, "GROCER")]), "amount-spike"))
check("a small absolute difference never fires, however proportionally large",
      not checks_in(scan(series("COFFEE", [-2.00] * 6) + [txn("s3", "2026-07-01", -12.00, "COFFEE")]),
                    "amount-spike"))
check("ordinary variation does not fire",
      not checks_in(scan(history + [txn("s4", "2026-07-01", -45.00, "GROCER")]), "amount-spike"))
# A refund is income for this merchant; letting it into the expense baseline would drag
# the median toward zero and make the next ordinary charge look enormous.
refunded = series("GROCER", [-40.00, -42.00, -38.00, -41.00, -39.00, -43.00]) + [
    txn("r1", "2026-06-15", 41.00, "GROCER"), txn("s5", "2026-07-01", -42.00, "GROCER")]
check("a refund does not contaminate the expense median",
      not checks_in(scan(refunded), "amount-spike"),
      str([f["message"] for f in checks_in(scan(refunded), "amount-spike")]))

print("== money the totals ignore produces no suggestions about spending")
oos = [txn("o1", "2026-03-02", -12.99, "KIOSK"), txn("o2", "2026-03-02", -12.99, "KIOSK")]
check("two out-of-scope rows are not a duplicate suggestion",
      not checks_in(scan(oos, {"o1": {"sharing": "out-of-scope"},
                               "o2": {"sharing": "out-of-scope"}}), "exact-duplicate"))
transfers = [dict(txn("tr1", "2026-03-02", -100.00, "UMBUCHUNG"), kind="internal-transfer"),
             dict(txn("tr2", "2026-03-02", -100.00, "UMBUCHUNG"), kind="internal-transfer")]
check("two legs of an internal transfer are not a duplicate suggestion",
      not checks_in(scan(transfers), "exact-duplicate"))

print("== a split is one charge, counted once")
split_rows = series("GROCER", [-40.00, -42.00, -38.00, -41.00, -39.00, -43.00]) + [
    txn("sp1", "2026-07-01", -400.00, "GROCER")]
split_decision = {"sp1": {"splits": [
    {"amount": -20.00, "category": "core-living/groceries", "sharing": "shared"},
    {"amount": -380.00, "category": "core-living/groceries", "sharing": "out-of-scope"}]}}
found = checks_in(scan(split_rows, split_decision), "amount-spike")
# The face value is 400; only 20 of it counts. Judging the face value would report a
# ten-fold spike on money the totals never saw.
check("a split parent is judged on the part that counts, not its face value",
      found and found[0]["evidence"]["amount_cents"] == 2000,
      str([f["evidence"] for f in found]))
check("and the face value is not what was measured",
      not any(f["evidence"]["amount_cents"] == 40000 for f in found))
check("and the parent is never counted twice",
      len({item["id"] for item in scan(split_rows, split_decision)["items"]})
      == len(scan(split_rows, split_decision)["items"]))

print("== a merchant that breaks its habit")
habit = series("UTILITY", [-30.00] * 5) + [txn("u1", "2026-07-01", 30.00, "UTILITY")]
found = checks_in(scan(habit), "unexpected-sign")
check("a first-ever refund from an expense merchant is suggested", len(found) == 1, str(found))
check("at medium confidence, because refunds are ordinary",
      found and found[0]["confidence"] == "medium")
check("the wording describes the habit rather than alleging a problem",
      found and "normally" in found[0]["message"], found[0]["message"] if found else "")
check("with fewer than five prior charges the habit is not established",
      not checks_in(scan(series("UTILITY", [-30.00] * 4)
                         + [txn("u2", "2026-07-01", 30.00, "UTILITY")]), "unexpected-sign"))
check("a merchant that has always done both never fires",
      not checks_in(scan(series("MIXED", [-30.00, 30.00, -30.00, 30.00, -30.00, 30.00])),
                    "unexpected-sign"))

print("== an account used in a new currency")
long_history = [txn("c%d" % i, "2026-01-%02d" % (i + 1), -10.00, "SHOP") for i in range(20)]
foreign = long_history + [txn("cf", "2026-06-01", -50.00, "SHOP", currency="BRL")]
found = checks_in(scan(foreign), "new-account-currency")
check("the first foreign charge on a long-EUR account is suggested", len(found) == 1, str(found))
twice = foreign + [txn("cf2", "2026-07-01", -60.00, "SHOP", currency="BRL")]
check("a multi-currency card does not warn on every later transaction",
      len(checks_in(scan(twice), "new-account-currency")) == 1,
      str([f["message"] for f in checks_in(scan(twice), "new-account-currency")]))
check("a young account is not judged for its currencies",
      not checks_in(scan([txn("y1", "2026-01-01", -10.00, "SHOP"),
                          txn("y2", "2026-02-01", -50.00, "SHOP", currency="BRL")]),
                    "new-account-currency"))

print("== a statement's own declared period")
uploads = {"uploads": [{"source_stem": "up1", "original_name": "march.csv",
                        "period_range": {"start": "2026-03-01", "end": "2026-03-31"}}]}
write_json(tmp / "data" / "uploads.json", uploads)
inside = txn("w1", "2026-03-15", -10.00, "SHOP", source={"file": "march.csv", "upload": "up1"})
outside = txn("w2", "2026-04-15", -10.00, "SHOP", source={"file": "march.csv", "upload": "up1"})
found = checks_in(scan([inside, outside]), "outside-statement-period")
check("a row dated outside its statement's period is suggested",
      len(found) == 1 and found[0]["transaction_ids"] == ["w2"], str(found))
write_json(tmp / "data" / "uploads.json", {"uploads": [{"source_stem": "up1",
                                                        "original_name": "march.csv"}]})
check("an upload with no declared period accuses nothing",
      not checks_in(scan([inside, outside]), "outside-statement-period"))
write_json(tmp / "data" / "uploads.json", {"uploads": []})

print("== dismissals")
result = scan(same_day)
target = checks_in(result, "exact-duplicate")[0]
anomalies.dismiss(target["id"], target["fingerprint"])
after = scan(same_day)
check("a dismissed suggestion disappears", not checks_in(after, "exact-duplicate"))
check("but is still visible when asked for",
      any(item["id"] == target["id"] and item["dismissed"]
          for item in scan(same_day, include_dismissed=True)["items"]))
check("and it survives a reload of the file",
      anomalies.load_dismissals().get(target["id"], {}).get("fingerprint") == target["fingerprint"])
# The dismissal was about *these* facts. Change one and the question is new again.
changed = [txn("d1", "2026-03-02", -129.90, "KIOSK"), txn("d2", "2026-03-02", -129.90, "KIOSK")]
check("changing the evidence brings the suggestion back",
      len(checks_in(scan(changed), "exact-duplicate")) == 1,
      str(checks_in(scan(changed), "exact-duplicate")))
check("dismissing writes only its own file, never a transaction",
      all(t["amount_eur"] == -129.90 for t in store.load_year_raw(YEAR)))

print("== identity is about the finding, not its wording")
first = anomalies.stable_id("exact-duplicate", ["b", "a"])
check("ids do not depend on id order", first == anomalies.stable_id("exact-duplicate", ["a", "b"]))
check("a different check is a different id", first != anomalies.stable_id("near-duplicate", ["a", "b"]))
check("a discriminator separates repeats of one check",
      anomalies.stable_id("amount-spike", ["a"], "2026-01") !=
      anomalies.stable_id("amount-spike", ["a"], "2026-02"))

print("== deterministic and read-only")
mixed = same_day + series("GROCER", [-40.00, -42.00, -38.00, -41.00, -39.00, -43.00]) + [
    txn("z1", "2026-07-01", -400.00, "GROCER")]
store.rewrite_year(YEAR, {"fixture.jsonl": mixed})
store.save_decisions(YEAR, {})
before = {path: path.read_bytes() for path in sorted((tmp / "data").rglob("*")) if path.is_file()}
one = anomalies.scan(YEAR, as_of=AS_OF)
two = anomalies.scan(YEAR, as_of=AS_OF)
after_bytes = {path: path.read_bytes() for path in sorted((tmp / "data").rglob("*")) if path.is_file()}
check("scanning writes nothing", before == after_bytes)
check("and returns byte-identical JSON",
      json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True))
check("ordering is stable and grouped by check",
      [i["check"] for i in one["items"]] == sorted(i["check"] for i in one["items"]))
check("counts match the items returned",
      one["counts"]["total"] == len(one["items"])
      and one["counts"]["high"] == sum(1 for i in one["items"] if i["confidence"] == "high"))

print("== the endpoints")
check("a bad scope is refused",
      _status(lambda: server.anomalies_view(year=YEAR, scope="nobody")) == 400)
live = server.anomalies_view(year=YEAR, scope="all")
item = live["items"][0]
check("dismissing an id the scan does not produce is 404",
      _status(lambda: server.anomaly_dismiss(server.AnomalyDismiss(
          id="nope", fingerprint="x", year=YEAR))) == 404)
check("dismissing with a stale fingerprint is 409, not a silent write",
      _status(lambda: server.anomaly_dismiss(server.AnomalyDismiss(
          id=item["id"], fingerprint="stale", year=YEAR))) == 409)
check("a matching dismissal is accepted",
      _status(lambda: server.anomaly_dismiss(server.AnomalyDismiss(
          id=item["id"], fingerprint=item["fingerprint"], year=YEAR))) == 200)


# Anti-shrink guard: exact count at implementation time. May only ever be RAISED
# when checks are added — never lowered (see AGENTS.md: never weaken a test).
MIN_CHECKS = 50
check("suite did not shrink", total_checks >= MIN_CHECKS,
      "total_checks=%d < %d" % (total_checks, MIN_CHECKS))

shutil.rmtree(tmp)
print()
if failures:
    print("FAILED: %s" % ", ".join(failures))
    sys.exit(1)
print("All checks passed.")
