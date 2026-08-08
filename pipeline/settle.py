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

from . import money, store
from .util import DATA, RULES, cents, load_accounts, load_config, read_json


def allocate_cents(total_cents, weights, keys):
    """Split an integer amount between named people so the parts add back to the whole.

    The arithmetic lives in `money.allocate_cents` — largest remainder over exact
    fractions — because allocation is the operation that most easily loses a cent and
    there must be exactly one of it. This is the keyed wrapper the settlement wants:
    ties break on `keys` order, which is the order people are configured in, so the same
    statement always produces the same split.
    """
    ordered = list(keys)
    parts = money.allocate_cents(total_cents, [weights[key] for key in ordered])
    return dict(zip(ordered, parts))


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
    by_category = defaultdict(int)
    income_by_owner = defaultdict(int)
    income = expense = 0
    ids = set()
    for e in entries(txns):
        if not e["year_cost"] or not in_scope(e, scope, income_cats, accounts):
            continue
        amount = cents(e["amount"])
        ids.add(e["txn"]["id"])
        by_category[e["category"] or "uncategorized"] += amount
        if _is_income(e, income_cats):
            income += amount
            income_by_owner[_entry_owner(e, accounts)] += amount
        else:
            expense += amount
    return {
        "income": money.from_cents(income),
        "expenses": money.from_cents(expense),
        "savings": money.from_cents(income + expense),
        "by_category": {k: money.from_cents(v) for k, v in sorted(by_category.items())},
        "income_by_owner": {k: money.from_cents(v) for k, v in sorted(income_by_owner.items())},
        "transactions": len(ids),
    }


def _summary(txns, monthly, scope="all", accounts=None):
    if accounts is None:
        accounts, _ = load_accounts()
    income_cats = income_categories()
    es = [e for e in entries(txns) if in_scope(e, scope, income_cats, accounts)]
    # A year cost is deliberately absent from the monthly picture, so it must be absent
    # from the monthly count as well. Counting it there described the figures beside it
    # as covering more than they do — the count and the totals have to be about the
    # same set of money, or the count is quietly answering a different question.
    counted = [e for e in es if not (monthly and e["year_cost"])]
    # Integer cents throughout. Every line is converted once and the totals are sums of
    # integers, so a year of transactions cannot accumulate the float error that lets
    # two screens disagree about the same euro. Floats reappear on the way out, for the
    # API, and nowhere before it.
    by_category = defaultdict(int)
    income_by_owner = defaultdict(int)
    income = expense = year_costs = 0
    for e in es:
        amount = cents(e["amount"])
        if monthly and e["year_cost"]:
            year_costs += amount
            continue
        by_category[e["category"] or "uncategorized"] += amount
        if _is_income(e, income_cats):
            income += amount
            income_by_owner[_entry_owner(e, accounts)] += amount
        else:
            expense += amount
    needs_review = sum(1 for t in txns if t["status"] == "needs_review")
    return {
        "income": money.from_cents(income),
        "expenses": money.from_cents(expense),
        "savings": money.from_cents(income + expense),
        "year_costs_excluded": money.from_cents(year_costs),
        "by_category": {k: money.from_cents(v) for k, v in sorted(by_category.items())},
        "income_by_owner": {k: money.from_cents(v) for k, v in sorted(income_by_owner.items())},
        "transactions": len({e["txn"]["id"] for e in counted}),
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

    def payer_account(t):
        account = accounts.get(t["account"])
        if not account:
            raise ValueError("Transaction %s references missing account '%s'" % (t["id"], t["account"]))
        return account

    # Everything below is integer cents until the moment it is reported. Money one
    # person owes the other has to add up exactly, and a float can only get close.
    #
    # Couple-owned money is pooled and halved ONCE, at the end. Halving each payment
    # as it arrives rounds a hundred times instead of once, and every odd cent lands
    # on the same side of the split: a year of joint groceries quietly walks one
    # person's "paid" figure away from the truth, and seven cents of joint salary
    # produced a 57/43 income ratio instead of 50/50. One rounding of the pool is off
    # by at most a cent no matter how many payments went into it.
    half = {p: 1 for p in people}
    shared_paid = {p: 0 for p in people}
    ratio_income = {p: 0 for p in people}
    couple_paid = couple_ratio_income = 0
    total_shared = 0
    for e in es:
        t = e["txn"]
        amount = cents(e["amount"])
        if _is_income(e, income_cats):
            if e["category"] in ratio_cats:
                owner = _entry_owner(e, accounts)
                if owner == "couple":
                    couple_ratio_income += amount
                else:
                    ratio_income[owner] += amount
        elif e["sharing"] == "shared":
            spent = -amount
            owner = payer_account(t)["owner"]
            if owner == "couple":
                couple_paid += spent
            else:
                shared_paid[owner] += spent
            total_shared += spent
    for p, share in allocate_cents(couple_paid, half, people).items():
        shared_paid[p] += share

    # The ratio is a proportion, not an amount, so it is derived from half-cents that
    # were never rounded — a person's own salary doubled, plus the whole couple pool,
    # which is that pool's half in these units. Deriving it from the rounded display
    # figures instead turned a three-cent joint salary, exactly 50/50, into 67/33.
    # Rounding now happens in exactly one place: the fair-share allocation below.
    income_half = {p: 2 * ratio_income[p] + couple_ratio_income for p in people}
    for p, share in allocate_cents(couple_ratio_income, half, people).items():
        ratio_income[p] += share      # display only; the ratio above does not use it

    total_income = sum(income_half.values())
    override = ratio_override(year, month, people)
    negative_income = [p for p in people if income_half[p] < 0]
    ratio_problem = None
    if override:
        ratio = override
        ratio_source = "manual override"
    elif negative_income:
        # A negative annual salary total is a booking error, not a small income: it
        # produces a ratio outside 0–100%, so one person's "fair share" exceeds the
        # whole shared cost and the other is owed money for existing. The old code
        # computed that anyway and mentioned it in a sentence appended to ratio_source.
        # There is no honest ratio to derive here, so fall back to the configured
        # reference ratio and report the problem as a fact the UI can phrase and
        # translate — a settlement standing on broken data has to say so where the
        # figures are, not in a string nobody reads.
        ratio = cfg["reference_ratio"]
        ratio_problem = {"kind": "negative-ratio-income", "people": sorted(negative_income)}
        ratio_source = "reference ratio (salary income is negative — see the warning)"
    elif total_income > 0:
        ratio = {p: income_half[p] / total_income for p in people}
        ratio_source = "actual salary income"
    else:
        ratio = cfg["reference_ratio"]
        ratio_source = "reference ratio (no salary data found)"

    fair_share = allocate_cents(total_shared, ratio, people)
    balances = {p: shared_paid[p] - fair_share[p] for p in people}
    # Conservation is the whole point of the cent arithmetic above, so it is checked
    # rather than assumed: a future change that reintroduces a float here fails here.
    if sum(shared_paid.values()) != total_shared or sum(fair_share.values()) != total_shared \
            or sum(balances.values()) != 0:
        raise AssertionError("settlement lost cents: paid=%r fair=%r total=%d"
                             % (shared_paid, fair_share, total_shared))
    # positive balance = paid more than fair share = should receive
    creditor = max(balances, key=lambda p: balances[p])
    debtor = min(balances, key=lambda p: balances[p])
    return {
        "period": "%d" % year if month is None else "%d-%02d" % (year, month),
        "binding": month is None,
        "ratio": {p: round(ratio[p], 4) for p in people},
        "ratio_source": ratio_source,
        "ratio_problem": ratio_problem,
        "ratio_income": {p: ratio_income[p] / 100.0 for p in people},
        "total_shared_expenses": total_shared / 100.0,
        "paid": {p: shared_paid[p] / 100.0 for p in people},
        "fair_share": {p: fair_share[p] / 100.0 for p in people},
        "balances": {p: balances[p] / 100.0 for p in people},
        "transfer": {"from": debtor, "to": creditor, "amount": balances[creditor] / 100.0}
        if balances[creditor] > 0 else None,
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
            "total": money.from_cents(money.sum_cents(x["amount"] for x in all_items)),
            "confirmed_total": money.from_cents(
                money.sum_cents(x["amount"] for x in all_items if x["confirmed"])),
            "candidate_count": sum(not x["confirmed"] for x in all_items),
            "confirmed_count": sum(x["confirmed"] for x in all_items),
            "ready_count": sum(x["ready"] for x in all_items),
            "missing_evidence_count": sum(x["confirmed"] and not x["ready"] for x in all_items),
            "owners": {o: {
                "total": money.from_cents(money.sum_cents(x["amount"] for x in items)),
                "confirmed_total": money.from_cents(
                    money.sum_cents(x["amount"] for x in items if x["confirmed"])),
                "items": items,
            }
                       for o, items in per_owner.items()},
        })
    return report
