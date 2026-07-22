"""Statement coverage derived from effective transactions."""
from datetime import date

from . import anchors, store
from .util import load_accounts


def coverage(year, today=None):
    """Return effective transaction counts per account and calendar month."""
    year = int(year)
    today = today or date.today()
    accounts, account_doc = load_accounts()
    counts = {account_id: [0] * 12 for account_id in accounts}
    active_months = []

    for txn in store.effective_year(year):
        try:
            month = int(str(txn["date"])[5:7])
        except (KeyError, TypeError, ValueError):
            continue
        if not 1 <= month <= 12:
            continue
        active_months.append(month)
        account_id = txn.get("account")
        if account_id in counts:
            counts[account_id][month - 1] += 1

    active_range = [None, None]
    if active_months:
        active_range = [min(active_months), max(active_months)]
        if year == today.year:
            active_range[1] = min(active_range[1], today.month)

    result_accounts = []
    for account in account_doc["accounts"]:
        low_activity = account.get("low_activity")
        if low_activity is None:
            low_activity = account.get("type") == "cash"
        result_accounts.append({
            "id": account["id"],
            "owner": account["owner"],
            "label": account.get("label"),
            "low_activity": bool(low_activity),
            "months": counts[account["id"]],
        })

    anchor_status = {account["id"]: anchors.summary(account["id"], year=year)
                     for account in account_doc["accounts"]}
    return {"accounts": result_accounts, "active_range": active_range, "anchors": anchor_status}
