"""An operation that writes several files either lands whole, or not at all.

Every single write in this application already publishes atomically, and that was used
as the argument that this was unnecessary. It is the wrong argument: the failures that
happen here span *several* files, and a sequence of atomic steps is not itself atomic.
Closing a month writes a lock, then a baseline; interrupt between the two and the month
is locked with no baseline, which means its drift detection silently does nothing and
nothing in the store says so.

So the tests that matter are the ones that interrupt the body on purpose — by exception
and by simulated crash — and assert the store is byte-for-byte what it was. The last
section is the one that would have caught the real defect: an operation whose *second*
file fails must not leave the first one changed.

Usage: .venv/bin/python tests/test_mutation_bundle.py
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from sandbox import PROJECT, build_sandbox

root = Path(tempfile.mkdtemp(prefix="fa-bundle-"))
os.environ["FA_ROOT"] = str(root)
build_sandbox(root)

sys.path.insert(0, str(PROJECT))
from pipeline import bundle, ingest, store  # noqa: E402
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


def _bundle_still_works(path):
    """A refused bundle must not leave the process unable to open another one."""
    try:
        with bundle.bundle("probe-after-refusal", [path], root=root):
            pass
        return True
    except Exception:                                    # noqa: BLE001
        return False


def tree_hash(where=None):
    """Every byte under the root, ignoring the bundle's own bookkeeping."""
    digest = hashlib.sha256()
    for path in sorted((where or root).rglob("*")):
        if any(part.startswith(".") for part in path.parts):
            continue
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


YEAR = store.years()[0]
MONTHS = root / "data" / str(YEAR) / "months.json"
CLOSINGS = root / "data" / str(YEAR) / "closings.json"


print("== a bundle that completes leaves its work in place and no litter")
before = tree_hash()
with bundle.bundle("probe", [MONTHS, CLOSINGS], root=root):
    write_json(MONTHS, {"%d-01" % YEAR: "closed"})
check("the write survived", MONTHS.exists() and json.loads(MONTHS.read_text()))
check("the journal is gone", not (root / bundle.JOURNAL_NAME).exists())
check("and so is the staging area", not (root / bundle.STAGING_NAME).exists())
MONTHS.unlink()
check("the tree is back to where it started", tree_hash() == before)


print("== a bundle that raises puts every file back")
write_json(MONTHS, {"%d-01" % YEAR: "open"})
write_json(CLOSINGS, {"%d-01" % YEAR: {"income": 1.0, "expenses": -2.0}})
before = tree_hash()

try:
    with bundle.bundle("probe", [MONTHS, CLOSINGS], root=root):
        write_json(MONTHS, {"%d-01" % YEAR: "closed"})
        # The second file fails after the first has already been written. This is the
        # exact shape of the defect: a rollback that only restores untouched paths
        # leaves the store half-changed.
        raise RuntimeError("injected failure after the first write")
except RuntimeError:
    pass
check("the first file is rolled back too", tree_hash() == before,
      "a bundle that only protects what it never wrote protects nothing")
check("no journal is left behind", not (root / bundle.JOURNAL_NAME).exists())
check("no staging is left behind", not (root / bundle.STAGING_NAME).exists())


print("== a path that did not exist before must not exist after a rollback")
CLOSINGS.unlink(missing_ok=True)
before = tree_hash()
try:
    with bundle.bundle("probe", [MONTHS, CLOSINGS], root=root):
        write_json(CLOSINGS, {"invented": True})
        raise RuntimeError("injected")
except RuntimeError:
    pass
check("the invented file is gone", not CLOSINGS.exists(),
      "restoring means the state it was found in, not the nearest thing to it")
check("and the tree is unchanged", tree_hash() == before)


print("== a bundle the process did not survive is rolled back on the next start")
# Simulates a crash: journal and snapshots on disk, body half-applied, no unwinding.
write_json(MONTHS, {"%d-01" % YEAR: "open"})
before = tree_hash()
staging = root / bundle.STAGING_NAME
staging.mkdir(parents=True, exist_ok=True)
shutil.copy2(MONTHS, staging / "000")
bundle._write_journal(root, {
    "stage": "running", "name": "close-month", "started_at": "2026-01-01T00:00:00+00:00",
    "entries": [{"path": str(MONTHS), "snapshot": "000", "existed": True}]})
write_json(MONTHS, {"%d-01" % YEAR: "closed"})     # the half-applied write

outcome = bundle.recover(root)
check("the interrupted bundle is detected", bool(outcome) and outcome["name"] == "close-month",
      str(outcome))
check("and the half-applied write is undone", tree_hash() == before)
check("the journal is cleared", not (root / bundle.JOURNAL_NAME).exists())
check("recovering again is a no-op", bundle.recover(root) is None)
check("recovering a clean root is a no-op", bundle.recover(root) is None)

# Recovery belongs to the boundary itself. A CLI process does not run FastAPI's
# startup hook, so its next mutation must recover the previous one before replacing
# the journal or staging directory.
write_json(MONTHS, {"%d-01" % YEAR: "open"})
staging.mkdir(parents=True, exist_ok=True)
shutil.copy2(MONTHS, staging / "000")
bundle._write_journal(root, {
    "stage": "running", "name": "first-cli-run", "started_at": "2026-01-01T00:00:00+00:00",
    "entries": [{"path": str(MONTHS), "snapshot": "000", "existed": True}]})
write_json(MONTHS, {"%d-01" % YEAR: "closed"})
with bundle.bundle("second-cli-run", [CLOSINGS], root=root):
    recovered_before_body = json.loads(MONTHS.read_text()).get("%d-01" % YEAR) == "open"
    write_json(CLOSINGS, {"annual": {"income": 1}})
check("the next mutation recovers an interrupted predecessor before its body runs",
      recovered_before_body)
check("and the new mutation is still allowed to complete",
      json.loads(CLOSINGS.read_text())["annual"]["income"] == 1)
check("the predecessor's journal and snapshots were not adopted",
      not (root / bundle.JOURNAL_NAME).exists() and not staging.exists())
CLOSINGS.unlink()

# A journal destroyed by the same crash cannot say what to restore. Guessing is exactly
# what this design refuses to do, so it reports instead of inventing.
(root / bundle.JOURNAL_NAME).write_text("{half written", encoding="utf-8")
staging.mkdir(parents=True, exist_ok=True)
(staging / "evidence").write_text("do not erase", encoding="utf-8")
outcome = bundle.recover(root)
check("an unreadable journal is reported, not guessed at",
      bool(outcome) and outcome.get("unreadable"), str(outcome))
try:
    with bundle.bundle("must-not-start", [MONTHS], root=root):
        pass
    refused_unreadable = False
except bundle.BundleRecoveryError:
    refused_unreadable = True
check("an unreadable predecessor blocks a new mutation without erasing evidence",
      refused_unreadable and (staging / "evidence").read_text() == "do not erase")
(root / bundle.JOURNAL_NAME).unlink()
shutil.rmtree(staging)


print("== closing a month is one unit, not three writes that hope")
from app import server  # noqa: E402
before = tree_hash()
server.close_month(server.MonthState(year=YEAR, month=1, state="closed"))
check("closing writes both the lock and the baseline",
      json.loads(MONTHS.read_text()).get("%d-01" % YEAR) == "closed" and CLOSINGS.exists(),
      "the two must move together or drift detection watches nothing")
check("and leaves no bundle behind", not (root / bundle.JOURNAL_NAME).exists()
      and not (root / bundle.STAGING_NAME).exists())
server.close_month(server.MonthState(year=YEAR, month=1, state="open"))
check("reopening withdraws the baseline as well as the lock",
      json.loads(MONTHS.read_text()).get("%d-01" % YEAR) == "open")

original_record = server.closings.record


def refuse(*args, **kwargs):
    raise RuntimeError("injected: the baseline could not be recorded")


server.closings.record = refuse
try:
    server.close_month(server.MonthState(year=YEAR, month=2, state="closed"))
    landed = True
except RuntimeError:
    landed = False
finally:
    server.closings.record = original_record
check("a close whose baseline fails does not leave the month locked",
      not landed and json.loads(MONTHS.read_text()).get("%d-02" % YEAR) != "closed",
      "the lock landed without its baseline, which is the state nothing can detect")


print("== an import that fails leaves the store exactly as it was")
# A real statement, taken back out of processed/, so the run genuinely reaches the point
# where transactions have been written and transfer detection has not yet run.
inbox = root / "inbox"
processed = inbox / "processed"
statement = sorted(processed.glob("*.csv"))[0]
shutil.copy2(statement, inbox / statement.name)
data_before = tree_hash(root / "data")
original_mark = ingest.transfers.mark_internal


def fail_late(*args, **kwargs):
    raise RuntimeError("injected: failure after the transactions were written")


ingest.transfers.mark_internal = fail_late
try:
    ingest.run(verbose=False)
    ran = True
except RuntimeError:
    ran = False
finally:
    ingest.transfers.mark_internal = original_mark
check("the injected failure actually reached the pipeline", not ran,
      "the scenario proved nothing: ingest never got as far as the failure")
check("the failing import leaves data/ exactly as it was", tree_hash(root / "data") == data_before,
      "transactions landed for a run that did not finish")
check("and no bundle state survives it", not (root / bundle.JOURNAL_NAME).exists()
      and not (root / bundle.STAGING_NAME).exists())
(inbox / statement.name).unlink(missing_ok=True)


print("== a bundle inside a bundle is refused, not silently honoured")
# There is one journal per root, so a second bundle's entry ran recovery, found the
# OUTER bundle's journal, and rolled the outer's finished work back — reporting success.
# Silent loss of work that had already landed is the worst possible failure here.
a_file = root / "nesting-a.json"
b_file = root / "nesting-b.json"
write_json(a_file, {"v": "old-a"})
write_json(b_file, {"v": "old-b"})
refused = False
try:
    with bundle.bundle("outer", [a_file], root=root):
        write_json(a_file, {"v": "new-a"})
        with bundle.bundle("inner", [b_file], root=root):
            write_json(b_file, {"v": "new-b"})
except bundle.BundleNestingError:
    refused = True
check("nesting raises rather than quietly undoing the outer bundle", refused)
check("and the outer bundle rolled back cleanly",
      json.loads(a_file.read_text())["v"] == "old-a")
check("no journal survives the refusal", not (root / bundle.JOURNAL_NAME).exists())
check("and the next bundle is not blocked by the failed one",
      _bundle_still_works(a_file))
a_file.unlink()
b_file.unlink()


print("== the crash guarantee does not depend on which entry point was used")
# The bundle used to sit in ingest.run(), which only the CLI calls; the web's "Process
# inbox" button calls _run_locked directly because serialize_writes already holds the
# lock. One import, two doors, and only one of them recoverable.
source = (PROJECT / "pipeline" / "ingest.py").read_text()
locked = source[source.index("def _run_locked"):source.index("def _run_inbox")]
check("_run_locked — the web's entry point — opens the bundle itself",
      "bundle.bundle(" in locked,
      "the button and the command line must get the same guarantee")


print("== appending transactions publishes rather than writes in place")
source = PROJECT / "pipeline" / "store.py"
text = source.read_text()
appender = text[text.index("def append_transactions"):text.index("def rewrite_year")]
check("append_transactions no longer opens the ledger in append mode",
      '"a"' not in appender and "'a'" not in appender,
      "an interrupted append leaves a half-written line in the canonical ledger")
check("and it fsyncs before publishing", "fsync" in appender and "os.replace" in appender)


# Anti-shrink guard: exact count at implementation time. May only ever be RAISED
# when checks are added — never lowered (see AGENTS.md: never weaken a test).
MIN_CHECKS = 28
check("suite did not shrink", total_checks >= MIN_CHECKS,
      "total_checks=%d < %d" % (total_checks, MIN_CHECKS))

shutil.rmtree(root)
print()
if failures:
    print("FAILED: %s" % ", ".join(failures))
    sys.exit(1)
print("All checks passed.")
