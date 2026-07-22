"""Backend safety test for staged PDF extraction through the web workflow."""
import csv
import os
import shutil
import sys
import tempfile
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
root = Path(tempfile.mkdtemp(prefix="fa-pdf-ui-test-"))
os.environ["FA_ROOT"] = str(root)
shutil.copy(PROJECT / "examples" / "config.json", root / "config.json")
(root / "rules").mkdir()
for name in ("categories.json", "merchant-rules.json", "tax-buckets.json"):
    shutil.copy(PROJECT / "examples" / name, root / "rules" / name)
(root / "data").mkdir()
shutil.copy(PROJECT / "examples" / "accounts.json", root / "data" / "accounts.json")
(root / "inbox" / "staging").mkdir(parents=True)

from app import server  # noqa: E402
from pipeline import store  # noqa: E402


def fake_extract(_pdf, output, _allow_review=False):
    with open(output, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(("date", "amount", "currency", "counterparty", "purpose", "counterparty_iban", "force_review"))
        writer.writerow(("2025-01-02", "-12.34", "EUR", "SAFE TEST", "PDF extraction test", "", "false"))
    return {"status": "ok", "period": "2025", "transactions_extracted": 1,
            "transactions_for_review": 0, "discrepancy": 0.0, "issues": [], "fatal_issues": []}


try:
    assert "trade-republic" in server.PDF_EXTRACTORS
    assert server.PDF_EXTRACTORS["trade-republic"]["manifest"].endswith("scripts/trade_republic/extractor.json")
    server.PDF_EXTRACTORS["test"] = {"label": "Test", "extract": fake_extract}
    stored = "s_test__statement.pdf"
    (server.STAGING / stored).write_bytes(b"fake pdf")
    server._save_staging([{
        "id": "s_test", "original_name": "statement.pdf", "stored": stored,
        "kind": "pdf", "size": 8, "hash": "test-hash", "account": "bank1-person1",
        "comment": "", "extractor": "test", "allow_review": False,
    }])
    result = server.ingest_process()
    assert result["results"][0]["status"] == "processed", result
    assert len(store.load_year_raw(2025)) == 1
    assert server._staging() == []
    upload = server._uploads()[0]
    assert upload["status"] == "processed" and upload["source_stem"]
    assert (server.INBOX / "processed" / upload["processed_pdf"]).exists()
    print("PDF UI flow passed: extract -> reconcile -> ingest -> audit files")
finally:
    shutil.rmtree(root)
