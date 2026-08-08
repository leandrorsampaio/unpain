"""Restore is the one operation that can destroy a household's whole ledger.

It used to write a safety backup, delete the live folders, and only then copy the
archive in file by file. Everything after that delete was unprotected: a subtly corrupt
backup replaced good data with bad, and a crash halfway left neither.

So there is one assertion behind almost every check here, and it is not about the error
message. Before each rejected restore the entire live tree is hashed; afterwards it is
hashed again; the two must be identical, byte for byte. An archive that is refused must
leave no trace at all — not a deleted folder, not a partially written file, not a
staging directory.

The archive itself is the only file in this application that arrives from outside it, so
it is attacked accordingly: traversal in both separator styles, absolute paths,
case-collisions, symlinks, CRC damage and decompression bombs.

Usage: .venv/bin/python tests/test_restore_safety.py
"""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from sandbox import PROJECT, build_sandbox

root = Path(tempfile.mkdtemp(prefix="fa-restore-safety-"))
os.environ["FA_ROOT"] = str(root)
build_sandbox(root)

sys.path.insert(0, str(PROJECT))
from pipeline import ingest, restore as restore_service  # noqa: E402
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


def tree_hash():
    """Every byte of the live tree, excluding the backups a restore legitimately writes."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if "backups" in path.parts or path.name.startswith("."):
            continue
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def attempt(raw, mode="replace", parts="data"):
    """Run a restore and report the rejection code, if any."""
    try:
        restore_service.restore_archive(
            raw, root=root, mode=mode,
            selected_parts=server._selected_parts(parts),
            backup_parts=server.BACKUP_PARTS,
            safety_backup=lambda: server._write_backup("pre-restore", ["data"]))
        return None
    except restore_service.RestoreRejected as exc:
        return exc.code


def must_reject(label, raw, code=None, mode="replace", parts="data"):
    """Refuse the archive AND leave the live tree byte-for-byte identical."""
    before = tree_hash()
    result = attempt(raw, mode=mode, parts=parts)
    after = tree_hash()
    check("refused: %s" % label, result is not None,
          "the archive was accepted")
    if code:
        check("  with code %s" % code, result == code, "got %r" % result)
    check("  and the live tree is untouched", before == after,
          "the store changed while rejecting an archive")
    check("  and no staging folder was left behind",
          not (root / ".restore-candidate").exists())


def zip_of(entries, *, mode_bits=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            info = zipfile.ZipInfo(name)
            if mode_bits is not None:
                info.external_attr = mode_bits << 16
            archive.writestr(info, content)
    return buffer.getvalue()


GOOD = server._write_backup("safety-test", list(server.BACKUP_PARTS)).read_bytes()


print("== a valid backup still restores")
before = tree_hash()
result = restore_service.restore_archive(
    GOOD, root=root, mode="replace", selected_parts=["data"],
    backup_parts=server.BACKUP_PARTS,
    safety_backup=lambda: server._write_backup("pre-restore", ["data"]))
check("a good archive is applied", result["ok"] and result["restored"] > 0, str(result)[:120])
check("and restoring the current state changes nothing", before == tree_hash())
check("the candidate folder is cleaned up", not (root / ".restore-candidate").exists())
check("a safety backup was written", bool(result["safety_backup"]), str(result))


print("== paths that try to escape")
must_reject("a parent-directory traversal", zip_of([("../evil.txt", "pwned")]), "unsafe-path")
must_reject("a Windows-style traversal", zip_of([("..\\evil.txt", "pwned")]), "unsafe-path")
must_reject("a deep traversal", zip_of([("data/../../evil.txt", "pwned")]), "unsafe-path")
must_reject("an absolute path", zip_of([("/etc/passwd", "pwned")]), "unsafe-path")
must_reject("a Windows drive path", zip_of([("C:/windows/system32", "pwned")]), "unsafe-path")
check("and nothing escaped the sandbox", not (root.parent / "evil.txt").exists())


print("== entries that are not files")
must_reject("a symlink", zip_of([("data/link", "/etc/passwd")], mode_bits=0o120777), "unsafe-entry")
must_reject("a device node", zip_of([("data/dev", "")], mode_bits=0o020666), "unsafe-entry")
must_reject("a FIFO", zip_of([("data/pipe", "")], mode_bits=0o010666), "unsafe-entry")


print("== archives that collide with themselves")
duplicate = io.BytesIO()
with zipfile.ZipFile(duplicate, "w") as archive:
    archive.writestr("data/accounts.json", "{}")
    archive.writestr("data/accounts.json", "{}")
must_reject("the same path twice", duplicate.getvalue(), "duplicate-path")

colliding = io.BytesIO()
with zipfile.ZipFile(colliding, "w") as archive:
    archive.writestr("data/Accounts.json", "{}")
    archive.writestr("data/accounts.json", "{}")
must_reject("two paths that differ only in case", colliding.getvalue(), "duplicate-path")


print("== archives that are damaged or hostile")
must_reject("something that is not a zip at all", b"not a zip", "not-a-zip")

corrupt = bytearray(GOOD)
corrupt[len(corrupt) // 2] ^= 0xFF          # flip a byte in the compressed data
must_reject("an archive whose contents fail their checksum", bytes(corrupt))

bomb = io.BytesIO()
with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("data/bomb.json", "0" * (50 * 1024 * 1024))
must_reject("an entry that expands hundreds of times", bomb.getvalue(), "compression-bomb")


print("== archives that are well-formed but would break the app")
must_reject("a config with only one person",
            zip_of([("config.json", json.dumps({"people": ["only-one"]}))]),
            "bad-config", parts="config")
must_reject("a config that is not JSON",
            zip_of([("config.json", "{not json")]), "bad-config", parts="config")

# The heart of it: an archive can be a perfectly valid zip full of perfectly valid JSON
# and still be a store the app cannot compute over. That is what the old order of
# operations could not catch — by the time anyone knew, the good data was deleted.
must_reject("a transaction with no account",
            zip_of([("data/2026/transactions/bad.jsonl",
                     json.dumps({"id": "x", "date": "2026-01-01", "amount_eur": -1.0,
                                 "amount_original": -1.0, "currency": "EUR",
                                 "source": {"file": "x"}}) + "\n")]),
            "invalid-state")
must_reject("a transaction whose amount is not finite",
            zip_of([("data/2026/transactions/bad.jsonl",
                     '{"id":"x","account":"bank1-person1","date":"2026-01-01",'
                     '"amount_original":-1.0,"currency":"EUR","amount_eur":NaN,'
                     '"source":{"file":"x"}}\n')]),
            "invalid-state")
must_reject("a JSONL line that is not JSON",
            zip_of([("data/2026/transactions/bad.jsonl", "{not json\n")]), "invalid-state")
must_reject("a decision for a transaction that does not exist",
            zip_of([("data/2026/decisions.json",
                     json.dumps({"ghost-id": {"category": "core-living/groceries"}}))]),
            "invalid-state")

check("after every rejection the store still loads",
      len(server.summary(year=2026)["by_category"]) >= 0)


print("== an archive with nothing for the selected parts")
must_reject("only entries outside the selection",
            zip_of([("rules/categories.json", "{}")]), "nothing-to-restore", parts="data")


print("== replace means the candidate lacks the old content, not that live data is deleted first")
stray = root / "data" / "stray.txt"
stray.write_text("not in the backup", encoding="utf-8")
restore_service.restore_archive(GOOD, root=root, mode="replace", selected_parts=["data"],
                                backup_parts=server.BACKUP_PARTS)
check("replace drops a file the archive does not carry", not stray.exists())

stray.write_text("keep me", encoding="utf-8")
restore_service.restore_archive(GOOD, root=root, mode="merge", selected_parts=["data"],
                                backup_parts=server.BACKUP_PARTS)
check("merge keeps it", stray.exists() and stray.read_text() == "keep me")
stray.unlink()

untouched = (root / "rules" / "categories.json").read_bytes()
restore_service.restore_archive(GOOD, root=root, mode="replace", selected_parts=["data"],
                                backup_parts=server.BACKUP_PARTS)
check("an unselected area is not touched at all",
      (root / "rules" / "categories.json").read_bytes() == untouched)


print("== a POST handler must not take the lock the middleware already holds")
# flock is per open file description, so a second acquisition on a new handle inside the
# same process blocks against itself — forever, with no error. This has now caught three
# times: pipeline.ingest.run, and restore, and it is invisible until a request hangs.
# serialize_writes holds the lock for every non-GET request, so handlers must not.
source = (PROJECT / "app" / "server.py").read_text()
offenders = []
current = None
for line in source.splitlines():
    stripped = line.strip()
    if stripped.startswith("@app.post("):
        current = stripped
    elif stripped.startswith("@app.get(") or stripped.startswith("@app.middleware("):
        current = None
    elif current and "with mutation_lock()" in stripped:
        offenders.append(current)
check("no POST handler acquires mutation_lock directly", not offenders,
      "these would deadlock against serialize_writes: %s" % offenders)
check("and the GET that writes still takes it",
      "with mutation_lock():" in source, "backup is a GET, so it must lock itself")


print("== the error names the archive, not the machine's temp folder")
before = tree_hash()
message = None
try:
    restore_service.restore_archive(
        zip_of([("data/2026/transactions/bad.jsonl", '{"id":"x","amount_eur":-1}\n')]),
        root=root, mode="replace", selected_parts=["data"], backup_parts=server.BACKUP_PARTS)
except restore_service.RestoreRejected as exc:
    message = str(exc)
check("a rejection explains what is wrong with the archive", bool(message))
check("without leaking the staging path", ".restore-candidate" not in (message or ""),
      (message or "")[:160])
check("and still names the file inside the archive",
      "bad.jsonl" in (message or ""), (message or "")[:160])
check("the live tree is still untouched", before == tree_hash())


print("== the safety backup is taken only once the restore is going to happen")
before_backups = len(list((root / "backups").glob("pre-restore-*.zip")))
attempt(zip_of([("../evil.txt", "x")]))
check("a rejected restore writes no pre-restore backup",
      len(list((root / "backups").glob("pre-restore-*.zip"))) == before_backups,
      "a safety copy of a restore that never happened is just clutter")


# Anti-shrink guard: exact count at implementation time. May only ever be RAISED
# when checks are added — never lowered (see AGENTS.md: never weaken a test).
MIN_CHECKS = 95
check("suite did not shrink", total_checks >= MIN_CHECKS,
      "total_checks=%d < %d" % (total_checks, MIN_CHECKS))

shutil.rmtree(root)
print()
if failures:
    print("FAILED: %s" % ", ".join(failures))
    sys.exit(1)
print("All checks passed.")
