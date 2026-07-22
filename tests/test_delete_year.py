"""Delete-a-year: phrase enforcement, safety backup, actual removal, and guards."""
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
root = Path(tempfile.mkdtemp(prefix="fa-delyear-test-"))
os.environ["FA_ROOT"] = str(root)
shutil.copy(PROJECT / "examples" / "config.json", root / "config.json")
(root / "rules").mkdir()
for name in ("categories.json", "merchant-rules.json", "tax-buckets.json"):
    shutil.copy(PROJECT / "examples" / name, root / "rules" / name)
(root / "data" / "2025" / "transactions").mkdir(parents=True)
(root / "data" / "2026" / "transactions").mkdir(parents=True)
shutil.copy(PROJECT / "examples" / "accounts.json", root / "data" / "accounts.json")

from app import server  # noqa: E402
from fastapi import HTTPException  # noqa: E402


LEDGER_25 = root / "data" / "2025" / "transactions" / "jan.jsonl"
LEDGER_25.write_text('{"id":"a","amount_eur":-10}\n', encoding="utf-8")
(root / "data" / "2025" / "decisions.json").write_text('{"a":{"note":"x"}}', encoding="utf-8")
RECEIPT_25 = root / "receipts" / "2025" / "r.pdf"
RECEIPT_25.parent.mkdir(parents=True)
RECEIPT_25.write_bytes(b"%PDF fake")
LEDGER_26 = root / "data" / "2026" / "transactions" / "jan.jsonl"
LEDGER_26.write_text('{"id":"b","amount_eur":-5}\n', encoding="utf-8")


def call(year, confirm):
    return server.delete_year(server.DeleteYear(year=year, confirm=confirm))


# ---- Wrong / empty phrase is rejected and deletes nothing.
for bad in ("", "delete", "delete 2024", "Delete 2025", " delete 2025 x"):
    try:
        call(2025, bad)
        assert False, "wrong phrase %r must be rejected" % bad
    except HTTPException as e:
        assert e.status_code == 400
assert LEDGER_25.exists(), "a rejected delete must not remove any data"

# ---- Unknown year is a 404.
try:
    call(2099, "delete 2099")
    assert False, "deleting a non-existent year must 404"
except HTTPException as e:
    assert e.status_code == 404

# ---- Correct phrase: safety backup written, then year + receipts removed.
result = call(2025, "delete 2025")
assert result["ok"] and result["year"] == 2025
assert not (root / "data" / "2025").exists(), "data/<year> must be gone"
assert not (root / "receipts" / "2025").exists(), "receipts/<year> must be gone"
assert result["years"] == [2026], "years list must drop the deleted year"

# ---- The safety backup contains the deleted year's data + receipts, restorable by path.
zpath = root / "backups" / result["safety_backup"]
assert zpath.exists(), "a safety backup zip must be written"
names = zipfile.ZipFile(zpath).namelist()
assert any(n.startswith("data/2025/") for n in names), "safety zip must include data/2025"
assert any(n.startswith("receipts/2025/") for n in names), "safety zip must include receipts/2025"

# ---- The untouched year is intact.
assert LEDGER_26.exists() and server.store.years() == [2026]

print("Delete-year passed: phrase enforced, 404 on unknown, data+receipts removed, safety backup written")
