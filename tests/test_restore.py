"""Restore-from-backup: round-trip, replace/merge semantics, safety backup, and guards."""
import asyncio
import io
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
root = Path(tempfile.mkdtemp(prefix="fa-restore-test-"))
os.environ["FA_ROOT"] = str(root)
shutil.copy(PROJECT / "examples" / "config.json", root / "config.json")
(root / "rules").mkdir()
for name in ("categories.json", "merchant-rules.json", "tax-buckets.json"):
    shutil.copy(PROJECT / "examples" / name, root / "rules" / name)
(root / "data" / "2026" / "transactions").mkdir(parents=True)
shutil.copy(PROJECT / "examples" / "accounts.json", root / "data" / "accounts.json")

from app import server  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from starlette.datastructures import UploadFile  # noqa: E402


def mkupload(data: bytes):
    return UploadFile(filename="backup.zip", file=io.BytesIO(data))


def call(data, mode="replace", parts=""):
    return asyncio.run(server.restore(file=mkupload(data), mode=mode, parts=parts))


# A real transaction row, not a stub. Restore now validates the candidate against the
# same schemas the app enforces everywhere else, so a fixture that could never exist in
# a working store would be testing the wrong thing.
ROW = ('{"id":"a","account":"bank1-person1","date":"2026-01-15","amount_original":-10.0,'
       '"currency":"EUR","amount_eur":-10.0,"fx_rate":null,"fx_rate_date":null,'
       '"fx_rate_source":null,"counterparty":"SHOP","purpose":"","counterparty_iban":"",'
       '"force_review":false,"kind":"normal","source":{"file":"jan.csv","format":"test"}}')
LEDGER = root / "data" / "2026" / "transactions" / "jan.jsonl"
LEDGER.write_text(ROW + "\n", encoding="utf-8")

# A backup of the good state (all parts).
backup_path = server._write_backup("test-backup", list(server.BACKUP_PARTS))
good_zip = backup_path.read_bytes()

# ---- REPLACE restores an exact mirror: corrupt + add junk, then restore data only.
LEDGER.write_text("CORRUPTED\n", encoding="utf-8")
junk = root / "data" / "junk.txt"
junk.write_text("stray", encoding="utf-8")
result = call(good_zip, mode="replace", parts="data")
assert LEDGER.read_text(encoding="utf-8") == ROW + "\n", "replace did not restore the ledger"
assert not junk.exists(), "replace must wipe files not in the backup"
assert result["restored"] >= 1 and "data" in result["parts"]

# ---- Safety backup: a pre-restore zip was written before overwriting.
pre = list((root / "backups").glob("pre-restore-*.zip"))
assert pre, "restore must write a pre-restore safety backup"

# ---- MERGE keeps files the backup does not contain.
junk.write_text("keep me", encoding="utf-8")
call(good_zip, mode="merge", parts="data")
assert junk.read_text(encoding="utf-8") == "keep me", "merge must keep files not in the backup"

# ---- Only selected parts are touched: restoring parts=rules leaves an edited config alone.
(root / "config.json").write_text('{"people":["person1","person2"],"household_name":"EDITED"}', encoding="utf-8")
call(good_zip, mode="replace", parts="rules")
import json
assert json.loads((root / "config.json").read_text())["household_name"] == "EDITED", "unselected config must be untouched"

# ---- zip-slip guard: an entry escaping the root is rejected, nothing applied.
evil = io.BytesIO()
with zipfile.ZipFile(evil, "w") as z:
    z.writestr("../evil.txt", "pwned")
try:
    call(evil.getvalue(), mode="replace", parts="")
    raise AssertionError("zip-slip entry was not rejected")
except HTTPException as e:
    assert e.status_code == 400
assert not (root.parent / "evil.txt").exists(), "zip-slip wrote outside the root"

# ---- bad config.json in the archive is rejected before anything is applied.
bad = io.BytesIO()
with zipfile.ZipFile(bad, "w") as z:
    z.writestr("config.json", '{"people":["only-one"]}')
try:
    call(bad.getvalue(), mode="replace", parts="config")
    raise AssertionError("invalid config was not rejected")
except HTTPException as e:
    assert e.status_code == 400
assert json.loads((root / "config.json").read_text())["household_name"] == "EDITED", "rejected restore must not change config"

# ---- a non-zip upload fails cleanly.
try:
    call(b"not a zip", mode="replace", parts="")
    raise AssertionError("non-zip was not rejected")
except HTTPException as e:
    assert e.status_code == 400

shutil.rmtree(root)
print("Restore passed: replace mirror, merge keep, scoped parts, safety backup, zip-slip + bad-config + non-zip rejected")
