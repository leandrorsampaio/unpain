"""Internal-transfer detection (idempotent pass over a year).

Money moving between our own accounts must never count as income or expense:
credit-card settlements, checking->savings, the equalization transfer itself.

Detection, conservative by design:
 1. counterparty/purpose contains a configured transfer marker
 2. pair match: opposite EUR amounts on two of our accounts within N days,
    where both accounts belong to the same owner OR the text mentions an
    owner name (covers the monthly equalization transfer).
"""
from datetime import date

from . import store
from .util import cents, load_accounts, load_config


def _d(iso):
    return date.fromisoformat(iso)


# Currency-conversion drift is proportional to the amount, so an absolute
# allowance only makes sense once the amount is large enough for one to be small
# relative to it.
FX_MATCH_MIN_CENTS = 2500     # below 25 EUR, match on the proportional rule alone
FX_MATCH_FLOOR_CENTS = 100    # and never allow more than this in absolute terms


def mark_internal(year):
    accounts, meta = load_accounts()
    cfg = load_config()
    window = cfg.get("transfer_match_window_days", 4)
    base_tolerance = cfg.get("transfer_match_tolerance_cents", 200)
    markers = [m.lower() for m in meta.get("transfer_markers", [])]
    owner_names = [n.lower() for n in meta.get("owner_names", [])]

    # Adjacent years are match partners so Dec/Jan bookings still pair. Writes
    # remain scoped to the transaction's own year files.
    years = [y for y in (year - 1, year, year + 1) if y in store.years()]
    by_year = {y: store.load_year_by_file(y) for y in years}
    all_txns = [t for files in by_year.values() for txns in files.values() for t in txns]
    decisions = {y: store.decisions(y) for y in years}

    def decision_for(t):
        return decisions.get(int(t["date"][:4]), {}).get(t["id"], {})

    def pair_context(n, p):
        if p["account"] == n["account"] or abs((_d(p["date"]) - _d(n["date"])).days) > window:
            return None
        po = accounts.get(p["account"], {}).get("owner")
        no = accounts.get(n["account"], {}).get("owner")
        same_owner = po is not None and po == no
        text = " ".join(filter(None, [n.get("counterparty"), n.get("purpose"),
                                      p.get("counterparty"), p.get("purpose")])).lower()
        named = any(name in text for name in owner_names)
        if not (same_owner or named):
            return None
        return "same-owner" if same_owner else "named"

    changed_years = set()

    def changed(t):
        changed_years.add(int(t["date"][:4]))

    def mark_pair(n, p, reason):
        for txn in (n, p):
            txn["kind"] = "internal-transfer"
            txn["transfer_reason"] = reason
            txn.pop("possible_transfer", None)
            txn.pop("possible_transfer_reason", None)
            changed(txn)

    def user_classified(t):
        """Has a human said this is a real, classified transaction?

        Only a category (or a split, which carries categories) counts. Pair
        matching sees nothing but two opposite amounts a few days apart, so a
        50 EUR reimbursement and an unrelated 50 EUR share purchase are
        indistinguishable to it — and marking both as an internal transfer erases
        a judgement someone actually made.

        Sharing deliberately does NOT count. Marking a transaction out of scope
        agrees that it should not appear in the totals, which is exactly what a
        genuine transfer between your own accounts looks like; treating that as a
        contradiction would un-pair real transfers.
        """
        decision = decision_for(t)
        return bool(decision.get("category") or decision.get("splits"))

    # Release anything automatic detection claimed that has since been given a
    # category. Only these are touched: re-deriving every mark from scratch cannot
    # reproduce pairings earlier runs found under different year windows, and would
    # silently release genuine transfers it can no longer re-pair.
    for t in all_txns:
        if decision_for(t).get("kind") == "internal-transfer":
            continue
        if t.get("kind") == "internal-transfer" and t.get("transfer_reason") and user_classified(t):
            t["kind"] = "normal"
            t.pop("transfer_reason", None)
            changed(t)

    for t in all_txns:
        decided_kind = decision_for(t).get("kind")
        if decided_kind == "normal":
            if t.get("kind") == "internal-transfer":
                t["kind"] = "normal"
                t.pop("transfer_reason", None)
                changed(t)
            continue
        if decided_kind == "internal-transfer":
            continue
        if t.get("kind") == "internal-transfer" or user_classified(t):
            continue
        text = ((t.get("counterparty") or "") + " " + (t.get("purpose") or "")).lower()
        if any(m in text for m in markers):
            t["kind"] = "internal-transfer"
            t["transfer_reason"] = "marker"
            t.pop("possible_transfer", None)
            t.pop("possible_transfer_reason", None)
            changed(t)

    # Clear stale hints, then pair-match the remainder.
    candidates = [t for t in all_txns
                  if t.get("kind") == "normal" and not decision_for(t).get("kind")
                  and not user_classified(t)]
    for t in candidates:
        had_hint = "possible_transfer" in t or "possible_transfer_reason" in t
        t.pop("possible_transfer", None)
        t.pop("possible_transfer_reason", None)
        if had_hint:
            changed(t)
    by_cents = {}
    for t in candidates:
        by_cents.setdefault(abs(cents(t["amount_eur"])), []).append(t)
    matched = set()
    for amount, group in by_cents.items():
        if amount == 0 or len(group) < 2:
            continue
        negs = [t for t in group if t["amount_eur"] < 0]
        poss = [t for t in group if t["amount_eur"] > 0]
        for n in negs:
            if n["id"] in matched:
                continue
            for p in poss:
                if p["id"] in matched:
                    continue
                context = pair_context(n, p)
                if not context:
                    continue
                mark_pair(n, p, "pair:" + context)
                matched.update((n["id"], p["id"]))
                break

    # FX bookings legitimately differ after conversion and fees. Match the closest
    # remaining eligible pair, but only where the amount is large enough for an
    # absolute allowance to mean anything (see FX_MATCH_MIN_CENTS).
    negs = [t for t in candidates if t["amount_eur"] < 0 and t["id"] not in matched]
    poss = [t for t in candidates if t["amount_eur"] > 0 and t["id"] not in matched]
    for n in negs:
        choices = []
        for p in poss:
            if p["id"] in matched or not pair_context(n, p):
                continue
            if (n.get("currency") or "EUR").upper() == "EUR" and (p.get("currency") or "EUR").upper() == "EUR":
                continue
            nc, pc = abs(cents(n["amount_eur"])), abs(cents(p["amount_eur"]))
            # A fee-sized allowance is meaningless on a small amount: the configured
            # 2.00 EUR floor is 81% of a 2.47 EUR transaction, so any two small
            # opposite amounts in the window paired up — dividends, subscriptions and
            # airport purchases were all being called transfers between own accounts.
            # Real conversion drift is proportional (a 300 EUR transfer arriving as
            # 298.18), so below this size only the proportional rule applies.
            if max(nc, pc) < FX_MATCH_MIN_CENTS:
                continue
            diff = abs(nc - pc)
            tolerance = max(FX_MATCH_FLOOR_CENTS, int(round(max(nc, pc) * .01)))
            if diff <= tolerance:
                choices.append((diff, p))
        if choices:
            _, p = min(choices, key=lambda item: item[0])
            mark_pair(n, p, "pair:fx-tolerant")
            matched.update((n["id"], p["id"]))

    # An eligible opposite-sign pair just outside tolerance is not silently
    # counted: flag the closest plausible candidate so the UI can ask the user.
    # Keep the cap conservative so ordinary income and spending in the same
    # account window do not become transfer hints.
    remaining_negs = [t for t in candidates if t["amount_eur"] < 0 and t["id"] not in matched]
    remaining_poss = [t for t in candidates if t["amount_eur"] > 0 and t["id"] not in matched]
    hinted = set()
    for n in remaining_negs:
        choices = []
        for p in remaining_poss:
            if p["id"] in hinted or not pair_context(n, p):
                continue
            nc, pc = abs(cents(n["amount_eur"])), abs(cents(p["amount_eur"]))
            diff = abs(nc - pc)
            tolerance = max(base_tolerance, int(round(max(nc, pc) * .01)))
            hint_tolerance = max(5 * tolerance, int(round(max(nc, pc) * .05)))
            if diff <= hint_tolerance:
                choices.append((diff, p))
        if not choices:
            continue
        diff, p = min(choices, key=lambda item: item[0])
        reason = "Opposite account movement differs by %.2f EUR" % (diff / 100.0)
        for txn in (n, p):
            txn["possible_transfer"] = True
            txn["possible_transfer_reason"] = reason
            changed(txn)
            hinted.add(txn["id"])

    for changed_year in changed_years:
        store.rewrite_year(changed_year, by_year[changed_year])
    return bool(changed_years)
