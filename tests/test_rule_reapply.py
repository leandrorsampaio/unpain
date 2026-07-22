"""Regression test for applying a live rule over manual historical classifications."""
import os
import shutil
import sys
import tempfile
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
root = Path(tempfile.mkdtemp(prefix="fa-rule-apply-test-"))
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


def txn(txn_id, day):
    return {
        "id": txn_id, "account": "bank1-person1", "date": day,
        "amount_original": -20.0, "currency": "EUR", "amount_eur": -20.0,
        "fx_rate": None, "counterparty": "REWE Markt", "purpose": "groceries",
        "counterparty_iban": "", "kind": "normal", "source": {"file": "test.csv", "format": "test"},
    }


try:
    # The Rules UI legitimately sends null for optional values. Pydantic must
    # accept the exact payload instead of returning a 422 validation error.
    payload = server.RulePayload(
        pattern="Savings plan execution", field="any", category="out-of-scope",
        sharing="shared", scope="person1", note=None, action=None,
    )
    assert payload.note is None and payload.action is None

    store.append_transactions(2026, "rule-apply", [
        txn("eligible", "2026-06-01"), txn("split", "2026-06-02"), txn("closed", "2026-07-01"),
    ])
    store.save_decisions(2026, {
        "eligible": {"category": "sports/equipment", "sharing": "personal:person1",
                     "tax_bucket": "old-tax", "note": "keep me",
                     "attachments": [{"file": "receipt.pdf", "description": "keep"}]},
        "split": {"category": "sports/equipment", "sharing": "shared",
                  "splits": [{"amount": -20.0, "category": "sports/equipment", "sharing": "shared"}]},
        "closed": {"category": "sports/equipment", "sharing": "shared"},
    })
    store.save_months_state(2026, {"2026-07": "closed"})

    # A wrong field must explain cross-field matches, and the rule field can be
    # corrected without deleting/recreating the rule.
    server.rule_update(server.RuleUpdate(id="rewe", field="purpose"))
    wrong_field = server._rule_impact("rewe", 2026)
    assert wrong_field["matched"] == 0
    assert wrong_field["field_matches"] == {"counterparty": 3, "purpose": 0, "any": 3}
    server.rule_update(server.RuleUpdate(id="rewe", field="any"))

    preview = server._rule_impact("rewe", 2026)
    assert preview["matched"] == 3 and preview["eligible"] == 1, preview
    assert preview["skipped_splits"] == 1 and preview["skipped_closed"] == 1, preview

    result = server._rule_impact("rewe", 2026, apply=True)
    assert result["applied"] == 1, result
    decisions = store.decisions(2026)
    assert decisions["eligible"] == {
        "note": "keep me", "attachments": [{"file": "receipt.pdf", "description": "keep"}]
    }
    assert decisions["split"]["category"] == "sports/equipment"
    assert decisions["closed"]["category"] == "sports/equipment"
    effective = {item["id"]: item for item in store.effective_year(2026)}
    assert effective["eligible"]["category"] == "core-living/groceries"
    assert effective["eligible"]["status"] == "rule-matched"
    print("Rule reapply passed: preview -> preserve protected state -> live rule controls entry")
finally:
    shutil.rmtree(root)
