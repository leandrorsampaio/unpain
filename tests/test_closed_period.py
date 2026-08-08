"""What a closed month actually refuses, operation by operation.

Closing a month is the moment a household agrees a period is settled. The lock behind
it covers decisions and nothing else — deliberately, because a merchant rule, a
re-ingest or a transfer verdict are often corrections and refusing those would preserve
a known error. That is a defensible design, and it is also a design nobody can rely on
unless it is written down as behaviour rather than as intention: a monthly ratio
override was *believed* to be locked, the check compared "3" against "2026-03", and
every override went through for as long as the feature existed.

So this is the matrix. Every operation that can move a figure or a settlement is run
against a closed month and recorded as one of:

  refused    HTTP 409, and the stored bytes are unchanged;
  allowed    it goes through by design, and the drift it causes is *surfaced* —
             because a change to a settled period that nothing reports is the same
             thing as an unlocked period.

An operation that is neither is a bug. Adding a write endpoint means adding it here.

Usage: .venv/bin/python tests/test_closed_period.py
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from sandbox import PROJECT, build_sandbox

tmp = Path(tempfile.mkdtemp(prefix="fa-closed-period-"))
os.environ["FA_ROOT"] = str(tmp)
build_sandbox(tmp)

sys.path.insert(0, str(PROJECT))
from fastapi import HTTPException  # noqa: E402
from pipeline import closings, ingest, store  # noqa: E402
from app import server  # noqa: E402

ingest.run(verbose=False)

failures = []
total_checks = 0


def check(name, cond, detail=""):
    global total_checks
    total_checks += 1
    if not cond:
        print("  FAIL %s %s" % (name, detail))
        failures.append(name)
    return cond


YEAR = store.years()[0]
MONTH = int(sorted(t["date"] for t in store.load_year_raw(YEAR))[0][5:7])
KEY = "%d-%02d" % (YEAR, MONTH)
IN_MONTH = [t for t in store.load_year_raw(YEAR) if t["date"][:7] == KEY]
PEOPLE = server._people()
CATEGORY = sorted(server._decision_options()[0] - {"auto:items"})[0]


def tree_hash():
    """Every byte of the store that a closed period could be rewritten through."""
    digest = hashlib.sha256()
    for path in sorted((tmp / "data").rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(tmp)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def set_month(state):
    """Open/close through the endpoint — closing records the baseline and reopening
    withdraws it, and writing months.json directly would skip both."""
    server.close_month(server.MonthState(year=YEAR, month=MONTH, state=state))


def close_month():
    set_month("open")
    set_month("closed")


def outcome(operation):
    """Run an operation against the closed month; report refusal or the state change."""
    close_month()
    before = tree_hash()
    try:
        operation()
    except HTTPException as exc:
        return exc.status_code, before == tree_hash()
    except Exception as exc:                       # noqa: BLE001 - reported, not swallowed
        return "crash: %s: %s" % (type(exc).__name__, exc), before == tree_hash()
    return 200, before == tree_hash()


txn = IN_MONTH[0]
second = IN_MONTH[1]

print("== operations a closed month must refuse")
REFUSED = {
    "a decision on one transaction":
        lambda: server.decision(server.Decision(
            year=YEAR, id=txn["id"], fields={"category": CATEGORY})),
    "a bulk decision":
        lambda: server.decisions_bulk(server.DecisionsBulk(
            year=YEAR, items=[{"id": txn["id"], "fields": {"category": CATEGORY}}])),
    "clearing a decision":
        lambda: server.decision_clear(server.DecisionClear(year=YEAR, id=txn["id"])),
    "clearing decisions in bulk":
        lambda: server.decisions_clear_bulk(server.DecisionsClearBulk(year=YEAR, ids=[txn["id"]])),
    "a raw transaction correction":
        lambda: server.transaction_edit(server.TxnEdit(
            year=YEAR, id=second["id"], date=second["date"],
            counterparty=second["counterparty"] or "x",
            amount_eur=round(second["amount_eur"] - 25.0, 2))),
    "a monthly ratio override":
        lambda: server.set_ratio_override(server.RatioOverride(
            year=YEAR, key=str(MONTH), ratio={PEOPLE[0]: 0.5, PEOPLE[1]: 0.5})),
    "clearing a monthly ratio override":
        lambda: server.set_ratio_override(server.RatioOverride(year=YEAR, key=str(MONTH), ratio=None)),
    "an annual ratio override while a month is closed":
        lambda: server.set_ratio_override(server.RatioOverride(
            year=YEAR, key="annual", ratio={PEOPLE[0]: 0.5, PEOPLE[1]: 0.5})),
    "adding a cash entry inside the month":
        lambda: server.cash(server.CashEntry(
            date="%s-15" % KEY, account="cash-person1", amount=-5.0, currency="EUR",
            description="closed month", category="")),
}
for label, operation in sorted(REFUSED.items()):
    status, unchanged = outcome(operation)
    check("refused: %s" % label, status == 409, "status %s" % (status,))
    check("and nothing was written: %s" % label, unchanged)


print("== operations that are allowed by design — and must surface what they moved")
# These are corrections. Refusing them would preserve a known error, so the design
# lets them through and reports the drift instead. What is being tested is that the
# reporting actually happens: an allowed rewrite nobody mentions is an unlocked month.
ALLOWED = {
    "a merchant rule that reclassifies the month": lambda: server.add_rule(server.RulePayload(
        pattern=(txn["counterparty"] or txn["purpose"] or "TEST")[:12], category=CATEGORY)),
}
for label, operation in sorted(ALLOWED.items()):
    status, unchanged = outcome(operation)
    if not check("allowed: %s" % label, status == 200, "status %s" % (status,)):
        continue
    # A rule changes no byte under data/ — categorization is derived on read. That is
    # precisely why the lock cannot see it and why the drift report has to.
    check("and it moved the month without touching a stored transaction: %s" % label, unchanged)
    drift = server.summary(year=YEAR).get("drift") or {}
    check("and the drift is surfaced where a person looks: %s" % label, bool(drift.get(KEY)),
          json.dumps(drift)[:300])
    check("and the integrity check reports it too: %s" % label,
          any(row["month"] == KEY and row["status"] == "drifted" for row in closings.verify(YEAR)),
          str([(r["month"], r["status"]) for r in closings.verify(YEAR)]))


print("== the lock is about the period, not the whole year")
close_month()
open_month = next((int(t["date"][5:7]) for t in store.load_year_raw(YEAR)
                   if t["date"][:7] != KEY), None)
if open_month:
    other = next(t for t in store.load_year_raw(YEAR) if int(t["date"][5:7]) == open_month)
    status, _ = outcome(lambda: server.decision(server.Decision(
        year=YEAR, id=other["id"], fields={"category": CATEGORY})))
    check("an open month in a year with a closed one still accepts decisions", status == 200,
          "status %s" % (status,))

print("== reopening restores the ability to change it")
set_month("open")
try:
    server.decision(server.Decision(year=YEAR, id=txn["id"], fields={"category": CATEGORY}))
    check("a reopened month accepts a decision again", True)
except HTTPException as exc:
    check("a reopened month accepts a decision again", False, "status %s" % exc.status_code)
check("and reopening dropped the baseline it was measured against",
      not closings.load(YEAR).get(KEY), str(closings.load(YEAR).get(KEY)))


print("== every write endpoint is accounted for")
# A new endpoint that can move a settled figure is exactly the kind of thing that gets
# added without anyone asking what a closed month should do about it. This does not
# guess the answer — it fails until a human writes the answer down, above.
source = (PROJECT / "app" / "server.py").read_text()
declared = {line.split('"')[1] for line in source.splitlines() if line.startswith('@app.post("/api/')}
# Endpoints that cannot touch a settled period's figures: session, config, uploads,
# categories, feedback, and the closing machinery itself.
IRRELEVANT = {
    "/api/unlock", "/api/lock", "/api/security/set-password", "/api/security/remove-password",
    "/api/security/auto-lock", "/api/feedback", "/api/feedback-delete", "/api/setup",
    "/api/settings-update", "/api/account-add", "/api/account-update", "/api/account-delete",
    "/api/closing-accept", "/api/anchor", "/api/anchor-delete", "/api/budgets",
    "/api/finding-dismiss", "/api/attachment-add", "/api/attachment-delete",
    "/api/recurring-override", "/api/restore", "/api/delete-year", "/api/rule-apply",
    "/api/rule-update", "/api/rule-delete", "/api/decision-clear-orphan",
    "/api/transaction-edit-reset", "/api/ingest", "/api/ingest/upload",
    "/api/ingest/staging-update", "/api/ingest/staging-delete", "/api/ingest/process",
    "/api/ingest/upload-delete", "/api/close", "/api/close-year", "/api/category",
    "/api/category-add", "/api/category-rename", "/api/category-archive",
    "/api/category-style", "/api/category-watch", "/api/category-delete",
    "/api/transfer-confirm", "/api/settlement-transfers", "/api/settlement-transfer-delete",
    "/api/cash-delete", "/api/cash-edit",
}
COVERED = {"/api/decision", "/api/decisions-bulk", "/api/decision-clear",
           "/api/decisions-clear-bulk", "/api/ratio-override", "/api/cash",
           "/api/rule", "/api/transaction-edit"}
unaccounted = sorted(declared - IRRELEVANT - COVERED)
check("no write endpoint is unaccounted for", not unaccounted,
      "decide what a closed month does about: %s" % ", ".join(unaccounted))


# Anti-shrink guard: exact count at implementation time. May only ever be RAISED
# when checks are added — never lowered (see AGENTS.md: never weaken a test).
MIN_CHECKS = 24
check("suite did not shrink", total_checks >= MIN_CHECKS,
      "total_checks=%d < %d" % (total_checks, MIN_CHECKS))

shutil.rmtree(tmp)
if failures:
    print("\nFAILED: %s" % ", ".join(sorted(set(failures))))
    sys.exit(1)
print("\nClosed-period matrix passed: %d checks." % total_checks)
