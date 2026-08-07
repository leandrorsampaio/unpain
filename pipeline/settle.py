"""All derived math: monthly/yearly summaries, savings, settlement.

Nothing here is ever stored — recomputed from the effective view on demand,
so changing a rule or a decision recalculates history automatically.

Conventions:
- expenses are negative, income positive; sums are signed, so a reimbursement
  assigned to an expense category naturally offsets it
- 'year_cost' entries (e-bike effect) are excluded from the monthly picture
  and shown/summed at year level
- sharing: shared | personal:<person> | out-of-scope; out-of-scope is invisible
  to all math
- settlement ratio: income in subcategories flagged ratio_income (salary),
  income_owner 'couple' counts half to each person
"""
from collections import defaultdict

from . import store
from .util import DATA, RULES, load_accounts, load_config, read_json


def _overrides_path(year):
    return DATA / str(year) / "ratio-overrides.json"


def ratio_overrides(year):
    """Manual settlement-ratio overrides: {'annual': {person: frac}, '3': {...}}."""
    return read_json(_overrides_path(year), default={})


def ratio_override(year, month, people):
    ov = ratio_overrides(year)
    r = ov.get(str(month) if month is not None else "annual")
    if r and all(p in r for p in people):
        total = sum(r[p] for p in people)
        if total <= 0 or any(r[p] < 0 for p in people):
            return None
        return {p: r[p] / total for p in people}   # normalize to sum 1
    return None


def _categories():
    return read_json(RULES / "categories.json")["categories"]


def income_categories():
    return {"%s/%s" % (c["slug"], s["slug"]) for c in _categories() if c.get("type") == "income" for s in c["subs"]}


def ratio_income_categories():
    return {"%s/%s" % (c["slug"], s["slug"])
            for c in _categories() if c.get("type") == "income"
            for s in c["subs"] if s.get("ratio_income")}


def part_view(txn, part=None):
    """The effective classification of one money line.

    A split part states only what differs from its parent and inherits the rest, so
    reading a part's fields directly is wrong wherever the parent's value is what
    applies. This is the single definition of that inheritance: the totals, the
    spreadsheet export and the integrity checks must all resolve a part the same way,
    or the app disagrees with its own audit trail. Pass part=None for an unsplit
    transaction, which is the same question with a trivial answer.
    """
    if part is None:
        return {"category": txn.get("category"), "sharing": txn.get("sharing", "shared"),
                "tax_bucket": txn.get("tax_bucket"), "year_cost": bool(txn.get("year_cost", False))}
    return {
        "category": part.get("category") if part.get("category") is not None else txn.get("category"),
        "sharing": part.get("sharing") or txn.get("sharing", "shared"),
        "tax_bucket": part.get("tax_bucket") if "tax_bucket" in part else txn.get("tax_bucket"),
        "year_cost": bool(part.get("year_cost", txn.get("year_cost", False))),
    }


def money_lines(txn):
    """The lines a transaction contributes: its split parts, or itself."""
    parts = txn.get("splits")
    return [(txn, part) for part in parts] if parts else [(txn, None)]


def entries(txns):
    """Expand splits; drop out-of-scope."""
    out = []
    for t in txns:
        # No parent-level shortcut. Skipping an out-of-scope parent before looking at
        # its parts threw away a part that said shared — while the spreadsheet, which
        # resolves each part properly, counted it. One split had to be two different
        # numbers depending on who asked. A part states the more specific intention,
        # so it decides; doctor reports the contradiction so a human settles it.
        for _, part in money_lines(t):
            view = part_view(t, part)
            if view["sharing"] == "out-of-scope":
                continue
            out.append(dict(view, txn=t,
                            amount=part["amount"] if part else t["amount_eur"]))
    return out


def _is_income(entry, income_cats):
    if entry["category"]:
        return entry["category"] in income_cats
    return entry["amount"] > 0


def _entry_owner(entry, accounts):
    """Who an entry belongs to: explicit income_owner, else the account owner."""
    t = entry["txn"]
    account = accounts.get(t["account"])
    if not account:
        raise ValueError("Transaction %s references missing account '%s'" % (t["id"], t["account"]))
    return t.get("income_owner") or account["owner"]


def in_scope(entry, scope, income_cats, accounts):
    """Dashboard perspective filter (partition, disjoint):
      'all'      -> everything
      'shared'   -> shared expenses + couple income
      '<person>' -> that person's personal expenses + their own income
    The three non-'all' scopes add up exactly to 'all'."""
    if scope == "all":
        return True
    if _is_income(entry, income_cats):
        owner = _entry_owner(entry, accounts)
        return owner == "couple" if scope == "shared" else owner == scope
    sharing = entry["sharing"]
    return sharing == "shared" if scope == "shared" else sharing == "personal:" + scope


def month_summary(year, month, scope="all"):
    txns = [t for t in store.effective_year(year) if int(t["date"][5:7]) == month]
    return _summary(txns, monthly=True, scope=scope)


def year_summary(year, scope="all"):
    accounts, _ = load_accounts()
    txns = store.effective_year(year)
    result = _summary(txns, monthly=False, scope=scope, accounts=accounts)
    result["months"] = []
    for m in range(1, 13):
        mt = [t for t in txns if int(t["date"][5:7]) == m]
        s = _summary(mt, monthly=True, scope=scope, accounts=accounts)
        s["month"] = m
        result["months"].append(s)
    result["year_costs"] = _year_cost_summary(txns, scope=scope, accounts=accounts)
    return result


def _year_cost_summary(txns, scope="all", accounts=None):
    """The year_cost slice on its own — income/expenses/by_category built from
    only the year_cost entries (the 'excluded' bucket shown as its own screen)."""
    if accounts is None:
        accounts, _ = load_accounts()
    income_cats = income_categories()
    by_category = defaultdict(float)
    income_by_owner = defaultdict(float)
    income = expense = 0.0
    ids = set()
    for e in entries(txns):
        if not e["year_cost"] or not in_scope(e, scope, income_cats, accounts):
            continue
        ids.add(e["txn"]["id"])
        by_category[e["category"] or "uncategorized"] += e["amount"]
        if _is_income(e, income_cats):
            income += e["amount"]
            income_by_owner[_entry_owner(e, accounts)] += e["amount"]
        else:
            expense += e["amount"]
    return {
        "income": round(income, 2),
        "expenses": round(expense, 2),
        "savings": round(income + expense, 2),
        "by_category": {k: round(v, 2) for k, v in sorted(by_category.items())},
        "income_by_owner": {k: round(v, 2) for k, v in sorted(income_by_owner.items())},
        "transactions": len(ids),
    }


def _summary(txns, monthly, scope="all", accounts=None):
    if accounts is None:
        accounts, _ = load_accounts()
    income_cats = income_categories()
    es = [e for e in entries(txns) if in_scope(e, scope, income_cats, accounts)]
    by_category = defaultdict(float)
    income_by_owner = defaultdict(float)
    income = expense = year_costs = 0.0
    for e in es:
        if monthly and e["year_cost"]:
            year_costs += e["amount"]
            continue
        by_category[e["category"] or "uncategorized"] += e["amount"]
        if _is_income(e, income_cats):
            income += e["amount"]
            income_by_owner[_entry_owner(e, accounts)] += e["amount"]
        else:
            expense += e["amount"]
    needs_review = sum(1 for t in txns if t["status"] == "needs_review")
    return {
        "income": round(income, 2),
        "expenses": round(expense, 2),
        "savings": round(income + expense, 2),
        "year_costs_excluded": round(year_costs, 2),
        "by_category": {k: round(v, 2) for k, v in sorted(by_category.items())},
        "income_by_owner": {k: round(v, 2) for k, v in sorted(income_by_owner.items())},
        "transactions": len({e["txn"]["id"] for e in es}),
        "needs_review": needs_review,
    }


def settlement(year, month=None):
    """month=None -> annual (binding). month=N -> estimate for that month."""
    cfg = load_config()
    accounts, _ = load_accounts()
    people = cfg["people"]
    txns = store.effective_year(year)
    if month is not None:
        txns = [t for t in txns if int(t["date"][5:7]) == month]
    es = entries(txns)
    if month is not None:
        es = [e for e in es if not e["year_cost"]]
    income_cats = income_categories()
    ratio_cats = ratio_income_categories()

    def payer_shares(t):
        account = accounts.get(t["account"])
        if not account:
            raise ValueError("Transaction %s references missing account '%s'" % (t["id"], t["account"]))
        owner = account["owner"]
        if owner == "couple":
            return {p: 0.5 for p in people}
        return {owner: 1.0}

    shared_paid = defaultdict(float)
    total_shared = 0.0
    ratio_income = defaultdict(float)
    for e in es:
        t = e["txn"]
        if _is_income(e, income_cats):
            if e["category"] in ratio_cats:
                owner = _entry_owner(e, accounts)
                if owner == "couple":
                    for p in people:
                        ratio_income[p] += e["amount"] / 2
                else:
                    ratio_income[owner] += e["amount"]
        elif e["sharing"] == "shared":
            for p, share in payer_shares(t).items():
                shared_paid[p] += -e["amount"] * share
            total_shared += -e["amount"]

    total_income = sum(ratio_income.values())
    override = ratio_override(year, month, people)
    if override:
        ratio = override
        ratio_source = "manual override"
    elif total_income > 0:
        ratio = {p: ratio_income[p] / total_income for p in people}
        ratio_source = "actual salary income"
        if any(ratio_income[p] < 0 for p in people):
            ratio_source += " (warning: negative salary income produced a ratio outside 0–100%)"
    else:
        ratio = cfg["reference_ratio"]
        ratio_source = "reference ratio (no salary data found)"

    balances = {p: round(shared_paid[p] - ratio[p] * total_shared, 2) for p in people}
    # positive balance = paid more than fair share = should receive
    creditor = max(balances, key=lambda p: balances[p])
    debtor = min(balances, key=lambda p: balances[p])
    return {
        "period": "%d" % year if month is None else "%d-%02d" % (year, month),
        "binding": month is None,
        "ratio": {p: round(ratio[p], 4) for p in people},
        "ratio_source": ratio_source,
        "ratio_income": {p: round(ratio_income[p], 2) for p in people},
        "total_shared_expenses": round(total_shared, 2),
        "paid": {p: round(shared_paid[p], 2) for p in people},
        "fair_share": {p: round(ratio[p] * total_shared, 2) for p in people},
        "balances": balances,
        "transfer": {"from": debtor, "to": creditor, "amount": round(max(balances[creditor], 0), 2)}
        if balances[creditor] > 0.005 else None,
    }


def tax_report(year):
    buckets = {b["slug"]: b for b in read_json(RULES / "tax-buckets.json")["buckets"]}
    accounts, _ = load_accounts()
    out = defaultdict(lambda: defaultdict(list))
    for e in entries(store.effective_year(year)):
        if not e.get("tax_bucket"):
            continue
        t = e["txn"]
        account = accounts.get(t["account"])
        if not account:
            raise ValueError("Transaction %s references missing account '%s'" % (t["id"], t["account"]))
        owner = t.get("tax_owner") or account["owner"]
        attachments = t.get("attachments") or ([{"file": t["receipt"]}] if t.get("receipt") else [])
        confirmed = bool(t.get("tax_confirmed"))
        owner_confirmed = bool(t.get("tax_owner"))
        payment_proof = account.get("type") != "cash"
        has_receipt = bool(attachments)
        out[e["tax_bucket"]][owner].append({
            "date": t["date"], "amount": e["amount"], "counterparty": t["counterparty"],
            "purpose": t["purpose"], "id": t["id"], "receipt": t.get("receipt"),
            "tax_source": t.get("tax_bucket_source"), "confirmed": confirmed,
            "tax_owner": owner, "owner_confirmed": owner_confirmed,
            "has_receipt": has_receipt, "payment_proof": payment_proof,
            "ready": confirmed and owner_confirmed and has_receipt and payment_proof,
        })
    report = []
    for slug, per_owner in sorted(out.items()):
        all_items = [item for items in per_owner.values() for item in items]
        report.append({
            "bucket": slug,
            "name": buckets.get(slug, {}).get("name", slug),
            "total": round(sum(x["amount"] for x in all_items), 2),
            "confirmed_total": round(sum(x["amount"] for x in all_items if x["confirmed"]), 2),
            "candidate_count": sum(not x["confirmed"] for x in all_items),
            "confirmed_count": sum(x["confirmed"] for x in all_items),
            "ready_count": sum(x["ready"] for x in all_items),
            "missing_evidence_count": sum(x["confirmed"] and not x["ready"] for x in all_items),
            "owners": {o: {
                "total": round(sum(x["amount"] for x in items), 2),
                "confirmed_total": round(sum(x["amount"] for x in items if x["confirmed"]), 2),
                "items": items,
            }
                       for o, items in per_owner.items()},
        })
    return report
