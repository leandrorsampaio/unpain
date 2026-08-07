"""Detected internal transfers must be reviewable, and the month lock must apply
only where a verdict actually moves money.

Excluding a transaction as an internal transfer removes it from every total. It is
the one judgement the app makes on its own that money depends on, and until these
endpoints existed a wrong one was unfindable: the transaction stopped appearing
anywhere. So the listing has to show both legs of a pair as one decision, a verdict
has to move both legs together, and a closed month has to block the verdict that puts
money back into settled figures without blocking the one that changes nothing.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
root = Path(tempfile.mkdtemp(prefix="fa-transfer-review-"))
os.environ["FA_ROOT"] = str(root)
shutil.copy(PROJECT / "examples" / "config.json", root / "config.json")
(root / "rules").mkdir()
for name in ("categories.json", "merchant-rules.json", "tax-buckets.json"):
    shutil.copy(PROJECT / "examples" / name, root / "rules" / name)
(root / "data" / "2025" / "transactions").mkdir(parents=True)
shutil.copy(PROJECT / "examples" / "accounts.json", root / "data" / "accounts.json")

from app import server  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from pipeline import doctor, settle, store, transfers  # noqa: E402

accounts = json.loads((root / "data" / "accounts.json").read_text())
ids = [a["id"] for a in (accounts["accounts"] if isinstance(accounts, dict) else accounts)]
owner_of = {a["id"]: a.get("owner")
            for a in (accounts["accounts"] if isinstance(accounts, dict) else accounts)}
# Two accounts with the same owner, so pair matching has a reason to fire at all.
same_owner = [(x, y) for x in ids for y in ids
              if x != y and owner_of[x] == owner_of[y] and owner_of[x] not in (None, "couple")]
assert same_owner, "fixture accounts must include two accounts owned by one person"
ACCOUNT_A, ACCOUNT_B = same_owner[0]


def txn(txn_id, account, amount, date, counterparty):
    return {"id": txn_id, "account": account, "amount_eur": amount, "amount": amount,
            "currency": "EUR", "date": date, "counterparty": counterparty, "purpose": "",
            "kind": "normal", "sharing": "shared", "status": "booked"}


rows = [
    txn("out#1", ACCOUNT_A, -400.00, "2025-03-04", "Own transfer"),
    txn("in#1", ACCOUNT_B, 400.00, "2025-03-05", "Own transfer"),
]
ledger = root / "data" / "2025" / "transactions" / "seed.jsonl"
ledger.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
transfers.mark_internal(2025)

listing = server.transfers_list(year=2025)
assert listing["pending"] == 1, listing            # one movement, not two rows
assert listing["confirmed"] == 0 and listing["rejected"] == 0, listing
group = listing["items"][0]
assert len(group["legs"]) == 2, group
assert {leg["id"] for leg in group["legs"]} == {"out#1", "in#1"}, group
assert group["amount_eur"] == 400.00, group
assert group["month_closed"] is False, group
assert server.transfers_pending_count(year=2025) == {"count": 1}

both = {t["id"]: t for t in store.effective_year(2025)}
assert all(both[i]["kind"] == "internal-transfer" for i in ("out#1", "in#1")), \
    "a pending detection stays excluded, so the totals never swing on an unanswered question"

# A verdict on one leg is a verdict on the movement: answering it two ways is the one
# answer the data cannot hold.
server.transfer_confirm(server.TransferVerdict(year=2025, id="out#1", confirmed=True))
decisions = store.decisions(2025)
assert decisions["out#1"]["kind"] == "internal-transfer", decisions
assert decisions["in#1"]["kind"] == "internal-transfer", decisions
assert server.transfers_list(year=2025)["pending"] == 0

# Rejecting puts money back into the totals.
server.transfer_confirm(server.TransferVerdict(year=2025, id="in#1", confirmed=False))
after = {t["id"]: t for t in store.effective_year(2025)}
assert all(after[i]["kind"] == "normal" for i in ("out#1", "in#1")), after
rejected = server.transfers_list(year=2025)
assert rejected["rejected"] == 1 and rejected["pending"] == 0, rejected

# Closing the month locks the figures. Rejecting changes them; confirming what is
# already excluded changes nothing, and blocking that would make a closed year
# impossible to audit — the opposite of what the lock is for.
store.save_months_state(2025, {"2025-03": "closed"})

try:
    server.transfer_confirm(server.TransferVerdict(year=2025, id="out#1", confirmed=True))
    raise AssertionError("re-excluding money in a closed month must be refused")
except HTTPException as exc:
    assert exc.status_code == 409 and "closed" in exc.detail, exc.detail

store.save_decisions(2025, {"out#1": {"kind": "internal-transfer"},
                            "in#1": {"kind": "internal-transfer"}})
transfers.mark_internal(2025)
server.transfer_confirm(server.TransferVerdict(year=2025, id="out#1", confirmed=True))
assert store.decisions(2025)["out#1"]["kind"] == "internal-transfer"

try:
    server.transfer_confirm(server.TransferVerdict(year=2025, id="out#1", confirmed=False))
    raise AssertionError("rejecting in a closed month must be refused")
except HTTPException as exc:
    assert exc.status_code == 409 and "closed" in exc.detail, exc.detail

try:
    server.transfer_confirm(server.TransferVerdict(year=2025, id="nope#1", confirmed=True))
    raise AssertionError("an unknown transaction must 404")
except HTTPException as exc:
    assert exc.status_code == 404, exc.status_code


# Detection reads categories, splits, kind decisions and the raw rows themselves, so
# every write to those has to ask the question again. It did not, and a categorised
# transaction stayed excluded as a transfer: the category applied to nothing until the
# next ingest, and the money was missing from every total until then.
def seed_pair(prefix, amount, first, second):
    rows = [
        txn(prefix + "-out#1", ACCOUNT_A, -amount, first, "Own transfer"),
        txn(prefix + "-in#1", ACCOUNT_B, amount, second, "Own transfer"),
    ]
    (root / "data" / "2025" / "transactions" / (prefix + ".jsonl")).write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    transfers.mark_internal(2025)
    return [r["id"] for r in rows]


def kinds(*ids):
    view = {t["id"]: t for t in store.effective_year(2025)}
    return [view[i]["kind"] for i in ids]


store.save_decisions(2025, {})
out_id, in_id = seed_pair("cat", 400.00, "2025-07-04", "2025-07-05")
assert kinds(out_id, in_id) == ["internal-transfer"] * 2, "the pair starts excluded"
server.decision(server.Decision(year=2025, id=out_id,
                                fields={"category": "core-living/groceries", "sharing": "shared"}))
assert kinds(out_id, in_id) == ["normal", "normal"], \
    "a category releases the pair immediately, not at the next ingest"
counted = {e["txn"]["id"] for e in settle.entries(store.effective_year(2025))}
assert {out_id, in_id} <= counted, "and both legs are counted from that moment"

# The same through the bulk endpoint, which is how the review queue writes.
store.save_decisions(2025, {})
out_id, in_id = seed_pair("bulk", 300.00, "2025-08-04", "2025-08-05")
assert kinds(out_id, in_id) == ["internal-transfer"] * 2
server.decisions_bulk(server.DecisionsBulk(year=2025, items=[
    {"id": out_id, "fields": {"category": "core-living/groceries", "sharing": "shared"}}]))
assert kinds(out_id, in_id) == ["normal", "normal"], "bulk writes reconcile too"

# Clearing the decision hands the transaction back to automatic detection, which pairs
# it again. "Clear" means restore the automatic answer, not leave a hole.
server.decisions_clear_bulk(server.DecisionsClearBulk(year=2025, ids=[out_id]))
assert kinds(out_id, in_id) == ["internal-transfer"] * 2, \
    "clearing a decision restores the automatic pairing"

# Pairing rests entirely on the rows: opposite amounts, two of our accounts, days
# apart. An edit changes that evidence, and nothing re-asked — a pair corrected from
# 100 to 80 stayed excluded with both halves invisible to every total.
store.save_decisions(2025, {})
out_id, in_id = seed_pair("edit", 100.00, "2025-09-04", "2025-09-05")
assert kinds(out_id, in_id) == ["internal-transfer"] * 2
server.transaction_edit(server.TxnEdit(year=2025, id=out_id, date="2025-09-04",
                                       counterparty="Own transfer", amount_eur=-80.00))
assert kinds(out_id, in_id) == ["normal", "normal"], \
    "an edit that breaks the pairing releases both legs"
server.transaction_edit_reset(server.DecisionClear(year=2025, id=out_id))
assert kinds(out_id, in_id) == ["internal-transfer"] * 2, \
    "and resetting the edit restores the evidence, so the pair returns"

# Detection is deliberately not exempt from closed months — skipping them would leave
# one leg of a boundary-straddling pair unmarked, and make a wrong exclusion inside a
# closed month permanent. What makes that safe is that the change is reported.
store.save_decisions(2025, {})
out_id, in_id = seed_pair("boundary", 100.00, "2025-10-30", "2025-11-02")
assert kinds(out_id, in_id) == ["internal-transfer"] * 2
server.close_month(server.MonthState(year=2025, month=10, state="closed"))
assert not [f for f in doctor.run(2025)["findings"] if f["check"] == "closed-month-drift"]
server.transaction_edit(server.TxnEdit(year=2025, id=in_id, date="2025-11-02",
                                       counterparty="Own transfer", amount_eur=80.00))
assert kinds(out_id, in_id) == ["normal", "normal"], "the pair is released across the boundary"
drift = [f for f in doctor.run(2025)["findings"] if f["check"] == "closed-month-drift"]
assert any("2025-10" in f["message"] for f in drift), \
    "and the closed month it moved is reported rather than changed in silence"


# A kind=normal verdict says this row is not money moving between our own accounts.
# The pairing was the only evidence the other row was either, so both go — leaving one
# excluded and one counted is the single state the data cannot mean. Driven through the
# endpoint, because the production write path is what has to reconcile.
for rejected_leg in ("out", "in"):
    store.save_decisions(2025, {})
    out_id, in_id = seed_pair("verdict-" + rejected_leg, 250.00, "2025-06-04", "2025-06-05")
    assert kinds(out_id, in_id) == ["internal-transfer"] * 2
    server.transfer_confirm(server.TransferVerdict(
        year=2025, id=out_id if rejected_leg == "out" else in_id, confirmed=False))
    assert kinds(out_id, in_id) == ["normal", "normal"], \
        "rejecting the %s leg must release both" % rejected_leg
    counted = {e["txn"]["id"] for e in settle.entries(store.effective_year(2025))}
    assert {out_id, in_id} <= counted, "and both must enter the totals"
    assert not [f for f in doctor.run(2025)["findings"]
                if f["check"] == "orphan-transfer-mark"], "with no orphan left behind"

# An explicit confirmation still outranks the evidence; detection cannot overturn it.
store.save_decisions(2025, {})
out_id, in_id = seed_pair("authority", 275.00, "2025-06-10", "2025-06-11")
server.transfer_confirm(server.TransferVerdict(year=2025, id=out_id, confirmed=True))
server.transaction_edit(server.TxnEdit(year=2025, id=out_id, date="2025-06-10",
                                       counterparty="Own transfer", amount_eur=-99.00))
assert kinds(out_id, in_id) == ["internal-transfer"] * 2, \
    "a confirmed pair survives evidence that no longer supports it — the human decided"

# A pair booked across New Year is one movement in two files. Detection already matches
# across the boundary; the verdict and the listing have to as well, or the far leg stays
# excluded while the near one is counted.
(root / "data" / "2026" / "transactions").mkdir(parents=True, exist_ok=True)
(root / "data" / "2025" / "transactions" / "cross.jsonl").write_text(
    json.dumps(txn("cross-out#1", ACCOUNT_A, -520.00, "2025-12-30", "Own transfer")) + "\n",
    encoding="utf-8")
(root / "data" / "2026" / "transactions" / "cross.jsonl").write_text(
    json.dumps(txn("cross-in#1", ACCOUNT_B, 520.00, "2026-01-02", "Own transfer")) + "\n",
    encoding="utf-8")
store.save_decisions(2025, {})
store.save_decisions(2026, {})
transfers.mark_internal(2025)
across = {t["id"]: t["kind"] for t in store.effective_year(2025) + store.effective_year(2026)}
assert across["cross-out#1"] == across["cross-in#1"] == "internal-transfer", across

cross_group = next(g for g in server.transfers_list(year=2025)["items"]
                   if g["id"] == "cross-out#1" or any(leg["id"] == "cross-out#1" for leg in g["legs"]))
assert {leg["id"] for leg in cross_group["legs"]} == {"cross-out#1", "cross-in#1"}, \
    "the review screen shows one movement, not two unrelated single-leg decisions"

verdict = server.transfer_confirm(server.TransferVerdict(year=2025, id="cross-out#1", confirmed=False))
assert sorted(verdict["updated"]) == ["cross-in#1", "cross-out#1"], verdict
assert store.decisions(2025)["cross-out#1"]["kind"] == "normal"
assert store.decisions(2026)["cross-in#1"]["kind"] == "normal", \
    "the far year's decision file must be written too"
across = {t["id"]: t["kind"] for t in store.effective_year(2025) + store.effective_year(2026)}
assert across["cross-out#1"] == across["cross-in#1"] == "normal", across

server.transfer_confirm(server.TransferVerdict(year=2025, id="cross-in#1", confirmed=True))
assert store.decisions(2026)["cross-in#1"]["kind"] == "internal-transfer"
assert store.decisions(2025)["cross-out#1"]["kind"] == "internal-transfer", \
    "confirming from either side moves both years back"

# A locked month on either leg refuses the whole verdict, so a pair is never half-answered.
store.save_months_state(2026, {"2026-01": "closed"})
try:
    server.transfer_confirm(server.TransferVerdict(year=2025, id="cross-out#1", confirmed=False))
    raise AssertionError("a closed month on the far leg must block the verdict")
except HTTPException as exc:
    assert exc.status_code == 409 and "2026-01" in exc.detail, exc.detail
assert store.decisions(2025)["cross-out#1"]["kind"] == "internal-transfer", \
    "and must leave the near year untouched"
store.save_months_state(2026, {})

shutil.rmtree(root, ignore_errors=True)
print("Transfer review endpoints passed")
