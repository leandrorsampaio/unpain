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

from . import rules_engine, store
from .util import cents, load_accounts, load_config


def _d(iso):
    return date.fromisoformat(iso)


# Currency-conversion drift is proportional to the amount, so an absolute
# allowance only makes sense once the amount is large enough for one to be small
# relative to it.
FX_MATCH_MIN_CENTS = 2500     # below 25 EUR, match on the proportional rule alone
FX_MATCH_FLOOR_CENTS = 100    # and never allow more than this in absolute terms


def evidence_holds(a, b, accounts, owner_names, window, account_of=None):
    """Do these two rows still look like one movement of money seen twice?

    Pairing rests entirely on the rows themselves: opposite amounts, on two of our own
    accounts, a few days apart. Editing an amount, a date or an account changes that
    evidence, and nothing re-asked the question — a pair corrected from 100 to 80 EUR
    stayed excluded, with both sides invisible to every total.
    """
    # The effective account, so a corrected one counts. Two legs moved onto a single
    # account cannot be a transfer between two, and reading the raw rows let that
    # pairing survive its own correction.
    account_of = account_of or (lambda t: t["account"])
    account_a, account_b = account_of(a), account_of(b)
    if a["id"] == b["id"] or account_a == account_b:
        return False
    ac, bc = cents(a["amount_eur"]), cents(b["amount_eur"])
    if (ac < 0) == (bc < 0):
        return False    # both the same direction: not two sides of one movement
    if abs((_d(a["date"]) - _d(b["date"])).days) > window:
        return False
    owner_a = accounts.get(account_a, {}).get("owner")
    owner_b = accounts.get(account_b, {}).get("owner")
    text = " ".join(filter(None, [a.get("counterparty"), a.get("purpose"),
                                  b.get("counterparty"), b.get("purpose")])).lower()
    if not ((owner_a is not None and owner_a == owner_b)
            or any(name in text for name in owner_names)):
        return False
    left, right = abs(ac), abs(bc)
    if "fx-tolerant" in (str(a.get("transfer_reason", "")) + str(b.get("transfer_reason", ""))):
        if max(left, right) < FX_MATCH_MIN_CENTS:
            return False
        return abs(left - right) <= max(FX_MATCH_FLOOR_CENTS, int(round(max(left, right) * .01)))
    return left == right


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

    def account_of(t):
        """The account after any manual correction, which is what the totals use."""
        return decision_for(t).get("account") or t["account"]

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
        for txn, other in ((n, p), (p, n)):
            txn["kind"] = "internal-transfer"
            txn["transfer_reason"] = reason
            # Which transaction this one is paired against. Without it a mark
            # outlives its own justification: when the other leg is released the
            # survivor keeps excluding money on the strength of a pair that no
            # longer exists (see release_orphans).
            txn["transfer_partner"] = other["id"]
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
            t.pop("transfer_partner", None)
            changed(t)

    # A pair is the whole justification for excluding either leg, so a mark whose
    # partner is gone has nothing left holding it up. This happens whenever one leg
    # is released above: the survivor would otherwise keep money out of the totals
    # on the strength of a pairing that no longer exists.
    def release_orphans():
        by_id = {t["id"]: t for t in all_txns}

        def still_paired(t):
            # The effective kind, through the one definition of that precedence — not
            # the stored row. A kind=normal decision releases a leg the instant it is
            # saved, but the stored value is not rewritten until a later pass in this
            # same function, so reading it here saw the pair as intact and left the
            # partner excluded for one whole run: one leg counted, one not.
            return rules_engine.effective_kind(t, decision_for(t)) == "internal-transfer"

        # Marks written before partners were recorded carry no id to check, so the
        # partner is re-identified the same way it was chosen: an opposite amount
        # on another account inside the window, itself still excluded.
        claimed = {t["transfer_partner"] for t in all_txns if t.get("transfer_partner")}
        legacy = [t for t in all_txns
                  if str(t.get("transfer_reason", "")).startswith("pair:")
                  and not t.get("transfer_partner") and still_paired(t)]
        for t in legacy:
            amount = cents(t["amount_eur"])
            best = None
            for other in legacy:
                if other["id"] == t["id"] or other["id"] in claimed:
                    continue
                if (cents(other["amount_eur"]) < 0) == (amount < 0):
                    continue
                if not pair_context(t, other) and not pair_context(other, t):
                    continue
                diff = abs(abs(cents(other["amount_eur"])) - abs(amount))
                tolerance = 0 if t.get("transfer_reason") != "pair:fx-tolerant" else \
                    max(FX_MATCH_FLOOR_CENTS, int(round(abs(amount) * .01)))
                if diff <= tolerance:
                    distance = abs((_d(other["date"]) - _d(t["date"])).days)
                    if best is None or (distance, diff) < best[0]:
                        best = ((distance, diff), other)
            if best:
                t["transfer_partner"] = best[1]["id"]
                best[1]["transfer_partner"] = t["id"]
                claimed.update((t["id"], best[1]["id"]))
                changed(t)
                changed(best[1])

        for t in all_txns:
            if decision_for(t).get("kind") == "internal-transfer":
                continue  # the user said so; detection does not get a vote
            if not still_paired(t):
                continue
            if not str(t.get("transfer_reason", "")).startswith("pair:"):
                continue
            partner = by_id.get(t.get("transfer_partner") or "")
            # Both that the partner is still excluded, and that the two rows still
            # look like one movement. An edit to either side can leave the pairing
            # arithmetically impossible while both halves stay quietly excluded.
            if (partner is not None and still_paired(partner)
                    and evidence_holds(t, partner, accounts, owner_names, window,
                                       account_of=account_of)):
                continue
            if partner is not None and still_paired(partner) \
                    and decision_for(partner).get("kind") != "internal-transfer":
                partner["kind"] = "normal"
                partner.pop("transfer_reason", None)
                partner.pop("transfer_partner", None)
                changed(partner)
            t["kind"] = "normal"
            t.pop("transfer_reason", None)
            t.pop("transfer_partner", None)
            changed(t)

    release_orphans()

    for t in all_txns:
        decided_kind = decision_for(t).get("kind")
        if decided_kind == "normal":
            if t.get("kind") == "internal-transfer":
                t["kind"] = "normal"
                # The reason survives an explicit rejection: it is the record that
                # detection proposed this and a human said no, which is what lets the
                # verdict be reviewed later and undone. Detection cannot re-claim it —
                # a decided kind removes it from the candidate pool below.
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
            # Nearest in time, not merely the first found. Three equal amounts can be
            # in flight at once (a cash deposit, and a personal->joint transfer of the
            # same size days later); taking the first candidate paired the deposit
            # with the transfer and left the real opposite leg counted as income.
            # A same-owner pair also beats a name-in-the-text pair at equal distance.
            choices = []
            for p in poss:
                if p["id"] in matched:
                    continue
                context = pair_context(n, p)
                if not context:
                    continue
                distance = abs((_d(p["date"]) - _d(n["date"])).days)
                choices.append(((distance, 0 if context == "same-owner" else 1), p, context))
            if not choices:
                continue
            _, p, context = min(choices, key=lambda item: item[0])
            mark_pair(n, p, "pair:" + context)
            matched.update((n["id"], p["id"]))

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


def detected(year):
    """Every transaction detection has excluded from the totals, with its status.

    Automatic exclusion is the one operation here that removes money from every
    figure without anybody agreeing to it, so it is the one that most needs to be
    visible. 'pending' rows are detection's own guess; 'confirmed' and 'rejected'
    carry a decision, which already outranks detection everywhere else.
    """
    raw = store.load_year_raw(year)
    # Partners are resolved across adjacent years, matching detection's own scope: a
    # transfer booked on 30 December and landing on 2 January is one movement, and
    # looking only inside one year showed it as two unrelated single-leg decisions.
    neighbours = [y for y in (year - 1, year, year + 1) if y in store.years()]
    by_id = {t["id"]: t for y in neighbours for t in store.load_year_raw(y)}
    decisions = store.decisions(year)   # only this year's rows carry a status here
    out = []
    for t in raw:
        decided = decisions.get(t["id"], {}).get("kind")
        reason = t.get("transfer_reason")
        if t.get("kind") != "internal-transfer" and decided != "internal-transfer":
            # A rejected detection is still worth showing: it explains why a
            # transfer-looking row is counted.
            if not (reason and decided == "normal"):
                continue
        partner = by_id.get(t.get("transfer_partner") or "")
        out.append({
            "id": t["id"],
            "date": t["date"],
            "account": t.get("account"),
            "amount_eur": t.get("amount_eur"),
            "counterparty": t.get("counterparty"),
            "purpose": t.get("purpose"),
            "reason": reason or "decision",
            "status": "confirmed" if decided == "internal-transfer"
                      else "rejected" if decided == "normal" else "pending",
            "partner_id": t.get("transfer_partner"),
            "partner": None if partner is None else {
                "id": partner["id"], "date": partner["date"],
                "account": partner.get("account"), "amount_eur": partner.get("amount_eur"),
                "counterparty": partner.get("counterparty"),
            },
        })
    out.sort(key=lambda r: (r["status"] != "pending", r["date"], r["id"]))
    return out
