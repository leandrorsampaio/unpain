"""Deterministic categorization: decision > merchant rule > needs_review.

Rules live in rules/merchant-rules.json, first match wins. Special category
'auto:items' resolves to core-living/items-up-to-50 or items-over-50 by amount.
Confirmations from the review UI append new rules via add_rule().
"""
import re

from .util import RULES, cents, load_config, read_json, write_json

RULES_FILE = RULES / "merchant-rules.json"
TAX_FILE = RULES / "tax-buckets.json"


def load_rules():
    return read_json(RULES_FILE)["rules"]


def add_rule(rule):
    data = read_json(RULES_FILE)
    data["rules"].append(rule)
    write_json(RULES_FILE, data)


def update_rule(rule_id, fields):
    data = read_json(RULES_FILE)
    for r in data["rules"]:
        if r["id"] == rule_id:
            for k, v in fields.items():
                if k in ("field", "contains"):
                    r.setdefault("match", {})[k] = v
                    continue
                if v is None:
                    r.pop(k, None)  # None means "clear this field"
                else:
                    r[k] = v
            write_json(RULES_FILE, data)
            return True
    return False


def remove_rule(rule_id):
    data = read_json(RULES_FILE)
    index = next((i for i, rule in enumerate(data["rules"]) if rule["id"] == rule_id), None)
    if index is None:
        return False
    del data["rules"][index]
    write_json(RULES_FILE, data)
    return True


def _matches(rule, txn):
    m = rule["match"]
    needle = m["contains"].lower()
    field = m.get("field", "any")
    if field == "any":
        hay = " ".join([txn.get("counterparty") or "", txn.get("purpose") or ""])
    else:
        hay = txn.get(field) or ""
    if m.get("regex"):
        return re.search(m["contains"], hay, re.IGNORECASE) is not None
    return needle in hay.lower()


def matches(rule, txn):
    """Public rule-match predicate for previews and administrative tooling."""
    return _matches(rule, txn)


def _resolve_auto(category, eur_amount, config=None):
    if category == "auto:items":
        limit = (config or load_config()).get("items_threshold_eur", 50)
        return "core-living/items-up-to-50" if abs(eur_amount) <= limit else "core-living/items-over-50"
    return category


def _tax_for_category(category, tax_buckets=None):
    if not category:
        return None
    for bucket in tax_buckets if tax_buckets is not None else read_json(TAX_FILE)["buckets"]:
        if category in bucket.get("category_map", []):
            return bucket["slug"]
    return None


def effective(txn, decision, rules, owner=None, config=None, tax_buckets=None):
    """Return the merged transaction view used by all downstream math.

    Rule scoping: scope 'family' (default) applies to every account; scope
    '<person>' applies only to that person's accounts and takes precedence
    over family rules. Couple-owned accounts match family rules only.
    """
    t = dict(txn)
    t.setdefault("kind", "normal")
    t["category"] = None
    t["sharing"] = "shared"
    t["tax_bucket"] = None
    t["tax_bucket_source"] = None
    t["tax_confirmed"] = False
    t["tax_owner"] = None
    t["year_cost"] = False
    t["splits"] = None
    t["status"] = "needs_review"
    t["matched_rule"] = None

    # A manual kind decision is authoritative. In particular, kind=normal is
    # how a user restores a false-positive transfer to the accounting view.
    if decision and "kind" in decision:
        t["kind"] = decision["kind"]
    if t.get("kind") == "internal-transfer":
        t["sharing"] = "out-of-scope"
        t["status"] = "auto"
        return t

    # A decision can force a transaction back into the review queue (UI "Send to review"),
    # overriding both any matching rule and any prior categorization.
    if decision and decision.get("force_review"):
        t["force_review"] = True
    personal = next((r for r in rules if owner and r.get("scope") == owner and _matches(r, t)), None)
    rule = personal or next((r for r in rules if r.get("scope", "family") == "family" and _matches(r, t)), None)
    if t.get("force_review"):
        rule = None
    if rule is not None:
        if rule.get("action") == "review":
            t["matched_rule"] = rule["id"]
        else:
            t["category"] = _resolve_auto(rule.get("category"), t["amount_eur"], config)
            t["sharing"] = rule.get("sharing", "shared")
            mapped_tax = _tax_for_category(t["category"], tax_buckets)
            t["tax_bucket"] = rule.get("tax_bucket") or mapped_tax
            if rule.get("tax_bucket"):
                t["tax_bucket_source"] = "rule"
                t["tax_confirmed"] = True
            elif mapped_tax:
                t["tax_bucket_source"] = "category-map"
            t["status"] = "rule-matched"
            t["matched_rule"] = rule["id"]
            if "note" in rule:
                t["note"] = rule["note"]

    if decision:
        for key in ("category", "sharing", "income_owner", "tax_bucket", "tax_confirmed", "tax_owner", "year_cost", "splits", "note", "receipt", "attachments", "account"):
            if key in decision:
                t[key] = decision[key]
        if "category" in decision:
            t["category"] = _resolve_auto(t.get("category"), t["amount_eur"], config)
        if t.get("splits"):
            t["splits"] = [dict(part, category=_resolve_auto(part.get("category"), part["amount"], config))
                           for part in t["splits"]]
        if "tax_bucket" in decision:
            t["tax_bucket_source"] = "decision" if decision.get("tax_bucket") else None
            # Decisions created before tax confirmation existed were explicit
            # manual tags, so preserve them as confirmed.
            if "tax_confirmed" not in decision:
                t["tax_confirmed"] = bool(decision.get("tax_bucket"))
        elif "category" in decision:
            t["tax_bucket"] = _tax_for_category(t["category"], tax_buckets)
            t["tax_bucket_source"] = "category-map" if t["tax_bucket"] else None
            if "tax_confirmed" not in decision:
                t["tax_confirmed"] = False
        has_split_tax = any(p.get("tax_bucket") for p in (t.get("splits") or []))
        if not t.get("tax_bucket") and not has_split_tax:
            t["tax_confirmed"] = False
            t["tax_owner"] = None
        if t.get("splits"):
            total = sum(cents(s["amount"]) for s in t["splits"])
            if total != cents(t["amount_eur"]):
                t["splits"] = None
                t["status"] = "needs_review"
                t["error"] = "invalid split"
        
        if not t.get("error") and (decision.get("category") or decision.get("splits")
                                   or decision.get("sharing") == "out-of-scope"):
            t["status"] = "confirmed"
        elif "category" in decision or "splits" in decision:
            t["status"] = "needs_review"
            
        if t.get("sharing") == "out-of-scope":
            t["status"] = "auto"

    # "Send to review" keeps the chosen category/sharing (still counted by all math) but is
    # surfaced in the review queue — unless it was set out-of-scope (invisible to review + math).
    if t.get("force_review") and t.get("sharing") != "out-of-scope":
        t["status"] = "needs_review"

    if t["status"] == "needs_review" and t["amount_eur"] > 0 and t.get("category") is None:
        t.setdefault("income_owner", None)

    return t
