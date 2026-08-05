"""Read-only whole-store data integrity checks."""
from collections import Counter, defaultdict
from datetime import date

from . import anchors, fx, ingest, rules_engine, store
from .util import DATA, RULES, cents, load_accounts, load_config, read_json


def _finding(severity, check, year, message, ids=None):
    return {"severity": severity, "check": check, "year": int(year),
            "message": message, "ids": list(ids or [])}


def _context(year):
    all_years = store.years()
    years = [int(year)] if year is not None else all_years
    accounts, account_meta = load_accounts()
    categories_doc = read_json(RULES / "categories.json")
    categories = {"%s/%s" % (group["slug"], sub["slug"])
                  for group in categories_doc["categories"] for sub in group.get("subs", [])}
    rules = read_json(RULES / "merchant-rules.json")["rules"]
    config = load_config()
    files_by_year = {item: store.load_year_by_file(item) for item in all_years}
    raw_by_year = {item: [txn for rows in files_by_year[item].values() for txn in rows]
                   for item in all_years}
    decisions = {item: store.decisions(item) for item in years}
    months = {item: store.months_state(item) for item in years}
    anchor_rows = [anchor for item in all_years for anchor in anchors.load(item)]
    conflicts = {item: anchors.load_conflicts(item) for item in years}
    return {
        "all_years": all_years, "years": years, "accounts": accounts,
        "account_meta": account_meta, "people": config.get("people", []),
        "categories": categories, "rules": rules, "config": config,
        "tax_buckets": read_json(RULES / "tax-buckets.json")["buckets"],
        "files": files_by_year, "raw": raw_by_year, "decisions": decisions,
        "months": months, "anchors": anchor_rows, "conflicts": conflicts,
        "budgets": read_json(RULES / "budgets.json", default={"budgets": {}}).get("budgets", {}),
        "uploads": read_json(DATA / "uploads.json", default={"uploads": []}).get("uploads", []),
    }


def _orphan_decisions(ctx):
    out = []
    for year in ctx["years"]:
        raw_ids = {txn.get("id") for txn in ctx["raw"].get(year, [])}
        ids = sorted(set(ctx["decisions"].get(year, {})) - raw_ids)
        if ids:
            out.append(_finding("error", "orphan-decision", year,
                                "%d decision%s reference no raw transaction." %
                                (len(ids), "" if len(ids) == 1 else "s"), ids))
    return out


def _unknown_accounts(ctx):
    out = []
    known = set(ctx["accounts"])
    for year in ctx["years"]:
        for txn in ctx["raw"].get(year, []):
            if txn.get("account") not in known:
                out.append(_finding("error", "unknown-account", year,
                                    "Raw transaction %s references unknown account '%s'." %
                                    (txn.get("id"), txn.get("account")), [txn.get("id")]))
        for txn_id, decision in ctx["decisions"].get(year, {}).items():
            if decision.get("account") is not None and decision.get("account") not in known:
                out.append(_finding("error", "unknown-account", year,
                                    "Decision %s reassigns to unknown account '%s'." %
                                    (txn_id, decision.get("account")), [txn_id]))
    return out


def _unknown_categories(ctx):
    out = []
    valid = ctx["categories"] | {"auto:items"}
    for year in ctx["years"]:
        for txn_id, decision in ctx["decisions"].get(year, {}).items():
            values = []
            if decision.get("category") is not None:
                values.append(decision.get("category"))
            values.extend(part.get("category") for part in (decision.get("splits") or [])
                          if part.get("category") is not None)
            missing = sorted({value for value in values if value not in valid})
            if missing:
                out.append(_finding("error", "unknown-category", year,
                                    "Decision %s references unknown categor%s: %s." %
                                    (txn_id, "y" if len(missing) == 1 else "ies", ", ".join(missing)),
                                    [txn_id]))
    for rule in ctx["rules"]:
        category = rule.get("category")
        if category is not None and category not in valid:
            out.append(_finding("error", "unknown-category", 0,
                                "Rule %s references unknown category '%s'." %
                                (rule.get("id"), category), [rule.get("id")]))
    return out


def _bad_splits(ctx):
    out = []
    for year in ctx["years"]:
        raw = {txn.get("id"): txn for txn in ctx["raw"].get(year, [])}
        for txn_id, decision in ctx["decisions"].get(year, {}).items():
            splits = decision.get("splits")
            if not splits or txn_id not in raw:
                continue
            try:
                split_total = sum(cents(part["amount"]) for part in splits)
                transaction_total = cents(raw[txn_id]["amount_eur"])
            except (KeyError, TypeError, ValueError):
                split_total, transaction_total = None, None
            if split_total != transaction_total:
                out.append(_finding("error", "split-sum", year,
                                    "Stored split for %s does not sum to the transaction amount." % txn_id,
                                    [txn_id]))
    return out


def _duplicate_ids(ctx):
    out = []
    for year in ctx["years"]:
        locations = defaultdict(set)
        for filename, transactions in ctx["files"].get(year, {}).items():
            for txn in transactions:
                locations[txn.get("id")].add(filename)
        for txn_id, filenames in locations.items():
            if len(filenames) > 1:
                out.append(_finding("error", "duplicate-id", year,
                                    "Transaction id %s appears in multiple files: %s." %
                                    (txn_id, ", ".join(sorted(filenames))), [txn_id]))
    return out


def _unknown_sharing_and_owner(ctx):
    out = []
    sharings = {"shared", "out-of-scope"} | {"personal:" + person for person in ctx["people"]}
    for year in ctx["years"]:
        for txn_id, decision in ctx["decisions"].get(year, {}).items():
            values = [decision.get("sharing")] if "sharing" in decision else []
            values.extend(part.get("sharing") for part in (decision.get("splits") or [])
                          if part.get("sharing") is not None)
            bad = sorted({value for value in values if value not in sharings})
            if bad:
                out.append(_finding("error", "unknown-sharing", year,
                                    "Decision %s has invalid sharing: %s." % (txn_id, ", ".join(bad)),
                                    [txn_id]))
            income_owner = decision.get("income_owner")
            tax_owner = decision.get("tax_owner")
            invalid = []
            if income_owner is not None and income_owner not in ctx["people"] + ["couple"]:
                invalid.append("income_owner=%s" % income_owner)
            if tax_owner is not None and tax_owner not in ctx["people"]:
                invalid.append("tax_owner=%s" % tax_owner)
            if invalid:
                out.append(_finding("error", "unknown-owner", year,
                                    "Decision %s has invalid owner field%s: %s." %
                                    (txn_id, "s" if len(invalid) > 1 else "", ", ".join(invalid)),
                                    [txn_id]))
    return out


def _anchor_findings(ctx):
    out = []
    for year in ctx["years"]:
        for conflict in ctx["conflicts"].get(year, []):
            out.append(_finding("error", "anchor-conflict", year,
                                "Conflicting balances were recorded for %s on %s." %
                                (conflict.get("account"), conflict.get("date")), []))
    for account_id, account in ctx["accounts"].items():
        spans = anchors.verify_loaded(account_id, account, ctx["anchors"], ctx["raw"])
        for span in spans:
            span_year = int(span["to"][:4])
            if span_year not in ctx["years"] or span.get("ok") is not False:
                continue
            difference = span["actual_cents"] - span["expected_cents"]
            out.append(_finding("error", "anchor-mismatch", span_year,
                                "Balance span %s to %s for %s differs by %s cents." %
                                (span["from"], span["to"], account_id, difference), []))
    return out


def _cash_desync(ctx):
    out = []
    try:
        expected = defaultdict(Counter)
        for row, _account, txn_id in ingest.cash_rows_with_ids(accounts=ctx["accounts"]):
            expected[int(row["date"][:4])][txn_id] += 1
    except Exception as exc:  # malformed source is itself a desync finding
        target = ctx["years"][0] if len(ctx["years"]) == 1 else 0
        return [_finding("error", "cash-desync", target,
                         "cash.csv could not be compared: %s." % exc, [])]
    for year in ctx["years"]:
        actual = Counter(txn.get("id") for txn in ctx["files"].get(year, {}).get("cash.jsonl", []))
        if actual != expected.get(year, Counter()):
            ids = sorted((actual - expected.get(year, Counter())).keys()
                         | (expected.get(year, Counter()) - actual).keys())
            out.append(_finding("error", "cash-desync", year,
                                "cash.csv ids do not match stored cash transactions.", ids))
    return out


def _review_in_closed_month(ctx):
    out = []
    for year in ctx["years"]:
        decisions = ctx["decisions"].get(year, {})
        for txn in ctx["raw"].get(year, []):
            month_key = txn.get("date", "")[:7]
            if ctx["months"].get(year, {}).get(month_key) != "closed":
                continue
            decision = decisions.get(txn.get("id"))
            effective_account = (decision or {}).get("account") or txn.get("account")
            try:
                effective = rules_engine.effective(
                    txn, decision, ctx["rules"], owner=ctx["accounts"].get(effective_account, {}).get("owner"),
                    config=ctx["config"], tax_buckets=ctx["tax_buckets"])
            except (KeyError, TypeError, ValueError):
                continue
            if effective.get("status") == "needs_review":
                out.append(_finding("warning", "review-in-closed-month", year,
                                    "Transaction %s still needs review in closed month %s." %
                                    (txn.get("id"), month_key), [txn.get("id")]))
    return out


CARD_SETTLEMENT_TERMS = ("credit card", "card settlement", "kreditkarte",
                         "kartenausgleich", "kartenabrechnung")


def _is_card_settlement(txn, ctx):
    """A recognised credit-card payoff. Its opposite leg is the card statement,
    which settles as a lump sum on a fixed billing day and rarely mirrors the
    payment by amount and date, so such a marker is *expected* to look unpaired
    and is not a data defect. Exempt it when the owner actually holds a card
    account (or when no card account is imported at all, i.e. an off-book card)."""
    text = " ".join([txn.get("counterparty") or "", txn.get("purpose") or ""]).lower()
    if not any(term in text for term in CARD_SETTLEMENT_TERMS):
        return False
    owner = ctx["accounts"].get(txn.get("account"), {}).get("owner")
    card_accounts = [item for item in ctx["accounts"].values()
                     if "credit" in (item.get("type") or "").lower()]
    return not card_accounts or any(item.get("owner") == owner for item in card_accounts)


def _unpaired_markers(ctx):
    out = []
    window = int(ctx["config"].get("transfer_match_window_days", 4))
    tolerance = int(ctx["config"].get("transfer_match_tolerance_cents", 200))
    all_transactions = [txn for year in ctx["all_years"] for txn in ctx["raw"].get(year, [])]
    for year in ctx["years"]:
        for txn in ctx["raw"].get(year, []):
            if txn.get("transfer_reason") != "marker":
                continue
            if ctx["decisions"].get(year, {}).get(txn.get("id"), {}).get("kind") == "normal":
                continue
            amount = cents(txn.get("amount_eur", 0))
            paired = any(other.get("account") != txn.get("account")
                         and amount * cents(other.get("amount_eur", 0)) < 0
                         and abs(abs(amount) - abs(cents(other.get("amount_eur", 0)))) <= tolerance
                         and abs((date.fromisoformat(other["date"]) - date.fromisoformat(txn["date"])).days) <= window
                         for other in all_transactions)
            if paired or _is_card_settlement(txn, ctx):
                continue
            out.append(_finding("warning", "unpaired-marker", year,
                                "Marker transfer %s has no opposite account movement within %d days." %
                                (txn.get("id"), window), [txn.get("id")]))
    return out


def _orphan_budgets(ctx):
    return [_finding("info", "orphan-budget", 0,
                     "Budget key '%s' matches no existing category." % category, [category])
            for category in sorted(ctx["budgets"]) if category not in ctx["categories"]]


def _stale_upload_refs(ctx):
    stems = {filename[:-6] for files in ctx["files"].values() for filename in files if filename.endswith(".jsonl")}
    out = []
    for upload in ctx["uploads"]:
        stem = upload.get("source_stem")
        # A statement covering a period with no activity produces no rows, and
        # append_transactions writes nothing for an empty list — so having no file is
        # expected here, not evidence that data went missing. Flagging it produced a
        # finding that could never be cleared, which is worse than not checking.
        if upload.get("total") == 0:
            continue
        if stem and stem not in stems:
            upload_id = upload.get("id") or stem
            out.append(_finding("info", "stale-upload-ref", 0,
                                "Upload %s references missing transaction source '%s'." % (upload_id, stem),
                                [upload_id]))
    return out


def _account_currency_mismatch(ctx):
    """An account whose transactions are not in the currency it declares.

    Nothing is wrong today — every transaction carries its own currency and is
    converted at the rate of its own date. It bites later: a manual balance or a
    balance anchor is read in the ACCOUNT's currency, so a BRL account labelled EUR
    turns a R$1.500 balance into 1.500 €.
    """
    out = []
    for account_id, account in ctx["accounts"].items():
        declared = (account.get("currency") or "EUR").upper()
        seen = {(txn.get("currency") or "EUR").upper()
                for year in ctx["all_years"] for txn in ctx["raw"].get(year, [])
                if txn.get("account") == account_id}
        foreign = sorted(seen - {declared})
        if seen and declared not in seen:
            out.append(_finding("warning", "account-currency", 0,
                                "Account '%s' is set to %s but all its transactions are in %s."
                                % (account_id, declared, ", ".join(sorted(seen))), [account_id]))
        elif foreign:
            out.append(_finding("info", "account-currency", 0,
                                "Account '%s' is set to %s and also holds %s transactions."
                                % (account_id, declared, ", ".join(foreign)), [account_id]))
    return out


def _fx_cache_sanity(ctx):
    """Catch an implausible ECB cache before it silently converts everything wrong.

    A stand-in file once sat here carrying two currencies and one flat rate for
    every date across five years; nothing noticed until a foreign import was
    checked by hand. Real ECB data has dozens of currencies and moves daily.
    """
    needed = {(txn.get("currency") or "EUR").upper()
              for year in ctx["all_years"] for txn in ctx["raw"].get(year, [])} - {"EUR"}
    if not needed:
        return []
    # fx._load() downloads when the cache is absent, and doctor must never write —
    # so the absence is reported rather than repaired.
    if not fx.CACHE.exists():
        return [_finding("warning", "fx-cache", 0,
                         "Foreign-currency transactions exist but no ECB rate cache is present. "
                         "Run 'fx-update'.", [])]
    try:
        rates = fx._load()
    except Exception as exc:  # noqa: BLE001 - an unreadable cache is itself the finding
        return [_finding("warning", "fx-cache", 0,
                         "Foreign-currency transactions exist but ECB rates could not be read: %s. "
                         "Run 'fx-update'." % exc, [])]
    out = []
    currencies = {code for day in rates.values() for code in day}
    if len(currencies) < 10:
        out.append(_finding("warning", "fx-cache", 0,
                            "The ECB rate cache lists only %d currencies. The real series carries "
                            "dozens — this looks like a stand-in file. Run 'fx-update'."
                            % len(currencies), []))
    for code in sorted(needed & currencies):
        series = {day[code] for day in rates.values() if code in day}
        if len(series) == 1:
            out.append(_finding("warning", "fx-cache", 0,
                                "Every cached %s rate is identical (%s). A real series moves daily, "
                                "so conversions are being made against a placeholder. Run 'fx-update'."
                                % (code, series.pop()), []))
    missing = sorted(needed - currencies)
    if missing:
        out.append(_finding("warning", "fx-cache", 0,
                            "Transactions use %s but the ECB cache has no such rate. Run 'fx-update'."
                            % ", ".join(missing), []))
    return out


def _out_of_scope_drift(ctx):
    """A transaction sitting in the out-of-scope CATEGORY without the out-of-scope FLAG.

    The two are different things: the flag removes a transaction from every total,
    the category is just a label. A row carrying only the label is still counted,
    and because that category group is typed as an expense it lands in the expense
    figure — silently, and with the wrong sign for anything income-like.
    """
    out = []
    for year in ctx["years"]:
        drifted = []
        for txn in ctx["raw"].get(year, []):
            decision = ctx["decisions"].get(year, {}).get(txn.get("id"), {})
            category = decision.get("category")
            sharing = decision.get("sharing") or txn.get("sharing")
            # Split parts carry their own sharing, and the parent's label is cosmetic.
            if decision.get("splits"):
                continue
            if category == "out-of-scope/out-of-scope" and sharing != "out-of-scope":
                drifted.append(txn["id"])
        if drifted:
            out.append(_finding("warning", "out-of-scope-drift", year,
                                "%d transactions use the out-of-scope category but are not marked "
                                "out of scope, so they still count in every total." % len(drifted),
                                sorted(drifted)))
    return out


CHECKS = (
    _orphan_decisions, _unknown_accounts, _unknown_categories, _bad_splits,
    _duplicate_ids, _unknown_sharing_and_owner, _anchor_findings, _cash_desync,
    _review_in_closed_month, _unpaired_markers, _orphan_budgets, _stale_upload_refs,
    _account_currency_mismatch, _fx_cache_sanity, _out_of_scope_drift,
)


def _attach_details(findings, ctx):
    """Enrich each id with the human context needed to act on it: the raw
    transaction (date, amount, counterparty, account) when one exists, or the
    stale decision's category for orphan decisions whose transaction is gone."""
    raw_by_id = {txn.get("id"): txn
                 for year in ctx["all_years"] for txn in ctx["raw"].get(year, [])}
    for finding in findings:
        decisions = ctx["decisions"].get(finding["year"], {})
        details = []
        for item_id in finding["ids"]:
            txn = raw_by_id.get(item_id)
            if txn is not None:
                details.append({"id": item_id, "date": txn.get("date"),
                                "amount_eur": txn.get("amount_eur"),
                                "counterparty": txn.get("counterparty"),
                                "account": txn.get("account")})
            elif item_id in decisions:
                details.append({"id": item_id, "category": decisions[item_id].get("category")})
            else:
                details.append({"id": item_id})
        finding["details"] = details
    return findings


def run(year=None):
    """Run every read-only integrity check for one year or the whole store."""
    ctx = _context(year)
    findings = [finding for check in CHECKS for finding in check(ctx)]
    rank = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda item: (rank[item["severity"]], item["check"], item["year"], item["message"]))
    _attach_details(findings, ctx)
    return {
        "findings": findings,
        "checked": {
            "years": ctx["years"],
            "transactions": sum(len(ctx["raw"].get(year, [])) for year in ctx["years"]),
            "decisions": sum(len(ctx["decisions"].get(year, {})) for year in ctx["years"]),
        },
    }
