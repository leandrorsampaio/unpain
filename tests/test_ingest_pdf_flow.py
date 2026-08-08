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


ROWS = [("2025-01-02", "-12.34", "EUR", "SAFE TEST", "PDF extraction test", "", "false")]


def write_rows(output, rows):
    with open(output, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(("date", "amount", "currency", "counterparty", "purpose", "counterparty_iban", "force_review"))
        writer.writerows(rows)


def make_extractor(rows=None, **overrides):
    """A fake extractor whose report can be made to disagree with the file it writes."""
    rows = ROWS if rows is None else rows
    report = {"status": "ok", "period": "2025", "transactions_extracted": len(rows),
              "transactions_for_review": 0, "discrepancy": 0.0, "issues": [], "fatal_issues": [],
              "opening_balance": 100.00, "closing_balance": 87.66,
              "sum_of_transactions": -12.34,
              "balance_anchors": [{"date": "2025-01-01", "balance": 100.00},
                                  {"date": "2025-01-02", "balance": 87.66}]}
    report.update(overrides)

    def extract(_pdf, output, _allow_review=False):
        write_rows(output, rows)
        return dict(report)
    return extract


def stage(extractor_id):
    stored = "s_%s__statement.pdf" % extractor_id
    (server.STAGING / stored).write_bytes(b"fake pdf")
    server._save_staging([{
        "id": "s_" + extractor_id, "original_name": "statement.pdf", "stored": stored,
        "kind": "pdf", "size": 8, "hash": "test-hash-" + extractor_id, "account": "bank1-person1",
        "comment": "", "extractor": extractor_id, "allow_review": False,
    }])
    return server.ingest_process()


try:
    assert "trade-republic" in server.PDF_EXTRACTORS
    assert server.PDF_EXTRACTORS["trade-republic"]["manifest"].endswith("scripts/trade_republic/extractor.json")

    # The admission gate is deterministic and independent of the extractor, so every way an
    # extractor can claim success it has not earned is refused here, with nothing written.
    rejected = {
        "no-balances": {"opening_balance": None, "closing_balance": None},
        "wrong-closing": {"closing_balance": 87.65},          # off by one cent
        "wrong-sum": {"sum_of_transactions": -99.99},         # report disagrees with its own file
        "wrong-count": {"transactions_extracted": 7},
        "bad-anchors": {"balance_anchors": [{"date": "2025-01-01", "balance": 100.00},
                                            {"date": "2025-01-02", "balance": 1.00}]},
        "nan-balance": {"closing_balance": float("nan")},
    }
    for name, overrides in rejected.items():
        server.PDF_EXTRACTORS[name] = {"label": name, "extract": make_extractor(**overrides)}
        result = stage(name)
        assert result["results"][0]["status"] == "error", (name, result)
        assert store.load_year_raw(2025) == [], "%s wrote a transaction" % name
        server._save_staging([])

    # A CSV changed after the extractor reconciled it must not pass either: the gate reads the
    # file that is about to be imported, not the one the extractor had in front of it.
    tampered = make_extractor(rows=ROWS + [("2025-01-03", "-5.00", "EUR", "EXTRA", "added later", "", "false")])
    server.PDF_EXTRACTORS["tampered"] = {"label": "tampered", "extract": tampered}
    result = stage("tampered")
    assert result["results"][0]["status"] == "error", result
    assert store.load_year_raw(2025) == []
    server._save_staging([])

    # And the honest one goes all the way through.
    server.PDF_EXTRACTORS["test"] = {"label": "Test", "extract": make_extractor()}
    result = stage("test")
    assert result["results"][0]["status"] == "processed", result
    assert len(store.load_year_raw(2025)) == 1
    assert server._staging() == []
    upload = server._uploads()[0]
    assert upload["status"] == "processed" and upload["source_stem"]
    assert (server.INBOX / "processed" / upload["processed_pdf"]).exists()
    print("PDF UI flow passed: unreconciled extractions refused, honest one ingested and audited")
finally:
    shutil.rmtree(root)
