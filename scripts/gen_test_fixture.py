#!/usr/bin/env python3
"""Deterministic test-fixture generator for Family Accountability.

Purpose
-------
Produce a *fully deterministic* synthetic dataset (same output every run) that
exercises the whole pipeline end-to-end, so that after ingesting it into the
app you can compare the dashboards / settlement against a KNOWN expected result
and prove the maths is correct: nothing dropped, nothing double-counted,
internal transfers excluded, out-of-scope invisible, money attributed to the
right partner.

It writes (all under the repo root):
  * data/accounts.json            — the 5 test accounts (2 Alex, 2 Sam, 1 joint)
  * rules/merchant-rules.json     — rules so every txn auto-categorizes the same way
  * data/fx/eurofxref-hist.csv    — PINNED FX rates (1 EUR = 6.25 BRL) for determinism
  * inbox/<account>__fixture.csv  — 5 CSVs, one per account, each in that bank's real format
  * scripts/EXPECTED_RESULTS.md   — the oracle: expected numbers per year + settlement

The expected numbers are computed by SIMPLE, INDEPENDENT arithmetic here (not by
importing pipeline/settle.py), so they are a genuine cross-check of the system.

Design notes that make the result deterministic and trustworthy
---------------------------------------------------------------
* No randomness at all — every transaction is hand-authored, looped over months/years.
* Salary is attributed to a person via the OWNER OF THE ACCOUNT it lands in
  (settle.py falls back to account owner when income_owner is unset), so we never
  need to write manual UI decisions.
* Internal transfers are detected by the real pipeline (markers + opposite-amount
  pair match). To guarantee NO ACCIDENTAL transfer is detected, every amount is
  globally unique EXCEPT the intended transfer pairs, and a self-check that mirrors
  pipeline/transfers.py asserts that only the intended pairs can ever pair-match.

Run:  .venv/bin/python scripts/gen_test_fixture.py
Then: .venv/bin/python -m pipeline.cli ingest   (or the website "Ingest inbox" button)
"""
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date, timedelta

ROOT = os.environ.get("FA_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from pipeline.util import txn_hash as _txn_hash  # noqa: E402  (real id hash so seeded decisions key exactly)
YEARS = [2021, 2022, 2023, 2024, 2025]
BRL_PER_EUR = 6.25  # pinned rate: 1 EUR = 6.25 BRL  => 1 BRL = 0.16 EUR
TRANSFER_WINDOW = 4  # must match config.json transfer_match_window_days

# ---------------------------------------------------------------------------
# Accounts (owner drives salary attribution and 'who paid' in settlement)
# ---------------------------------------------------------------------------
ACCOUNTS = [
    {"id": "db-giro-person1",  "owner": "person1",  "bank": "Deutsche Bank", "type": "giro",        "currency": "EUR", "iban": "DE11111111111111111111", "fmt": "db-giro"},
    {"id": "nubank-person1",   "owner": "person1",  "bank": "Nubank",        "type": "giro",        "currency": "BRL", "iban": None,                     "fmt": "nubank-conta"},
    {"id": "dkb-person2",     "owner": "person2", "bank": "DKB",           "type": "giro",        "currency": "EUR", "iban": "DE22222222222222222222", "fmt": "dkb"},
    {"id": "barclays-person2","owner": "person2", "bank": "Barclays",      "type": "credit-card", "currency": "EUR", "iban": None,                     "fmt": "barclays"},
    {"id": "n26-joint",        "owner": "couple",   "bank": "N26",           "type": "giro",        "currency": "EUR", "iban": "DE33333333333333333333", "fmt": "n26"},
    # cash wallets (no bank export) so the "Add entry" manual-cash form works
    {"id": "cash-person1",     "owner": "person1",  "bank": "Cash",          "type": "cash",        "currency": "EUR", "iban": None,                     "fmt": None},
    {"id": "cash-person2",    "owner": "person2", "bank": "Cash",          "type": "cash",        "currency": "EUR", "iban": None,                     "fmt": None},
]
ACC = {a["id"]: a for a in ACCOUNTS}
OWNER_NAMES = ["Alex", "Sam", "Fischer", "Weber"]
TRANSFER_MARKERS = ["KREDITKARTENABRECHNUNG", "CREDIT CARD SETTLEMENT"]

INCOME_CATS = {  # to-receive/* — mirror of categories.json income group
    "to-receive/salary", "to-receive/salary-extras", "to-receive/reimbursement",
    "to-receive/freelance", "to-receive/sold-items", "to-receive/gifts-received",
    "to-receive/tax-refund", "to-receive/capital-gains",
}
RATIO_CATS = {"to-receive/salary"}  # only salary drives the settlement ratio

# ---------------------------------------------------------------------------
# Merchant rules — one counterparty string -> (category, sharing). Everything a
# transaction needs to categorize deterministically. Transfer lines are NOT
# listed here (the pipeline marks them internal before categorization).
# ---------------------------------------------------------------------------
# (counterparty substring, category, sharing)
RULE_TABLE = [
    ("ARBEITGEBER ALEX",  "to-receive/salary",        "shared"),
    ("ARBEITGEBER SAM", "to-receive/salary",        "shared"),
    ("ARBEITGEBER GEMEINSAM","to-receive/salary",        "shared"),  # joint salary -> couple 50/50 ratio
    ("BONUS ALEX",        "to-receive/salary-extras", "shared"),
    ("FINANZAMT REFUND",     "to-receive/tax-refund",    "shared"),
    ("EBAY VERKAUF",         "to-receive/sold-items",    "shared"),
    ("FREELANCE CLIENT",     "to-receive/freelance",     "shared"),
    ("VERMIETER",            "living-costs/cold-rent",   "shared"),
    ("REWE",                 "core-living/groceries",    "shared"),
    ("STADTWERKE ENERGIE",   "living-costs/energy",      "shared"),
    ("TELEKOM INTERNET",     "living-costs/internet",    "shared"),
    ("RESTAURANT DA MARIO",  "recreation/restaurants",   "shared"),
    ("FITNESS FIRST",        "sports/gym",               "personal:person1"),
    ("DM DROGERIE",          "core-living/makeup",       "personal:person2"),
    ("NETFLIX BRASIL",       "recreation/streaming",     "personal:person1"),
    ("ZARA",                 "core-living/clothing",     "shared"),
    ("IKEA",                 "living-upgrades/furniture","shared"),
    ("SPENDE NGO",           "donations/ngo",            "shared"),
    ("ARZT PRAXIS",          "health/doctors",           "shared"),
    ("HANDWERKER MUELLER",   "living-costs/maintenance", "shared"),
    ("LUFTHANSA",            "recreation/going-out",     "personal:person1"),
    ("MEDIA MARKT",          "living-upgrades/appliances","shared"),  # big one-offs, flagged year_cost
    ("WORK EXPENSE",         None,                       "out-of-scope"),
    ("PRIVATKREDIT",         None,                       "out-of-scope"),
]
# Merchants that always go to the review queue (multi-purpose) and are resolved
# by a seeded split decision instead of a category rule.
REVIEW_MERCHANTS = ["AMAZON"]


def category_of(cp):
    for needle, cat, _ in RULE_TABLE:
        if needle in cp.upper():
            return cat
    return None


def sharing_of(cp):
    for needle, _, sh in RULE_TABLE:
        if needle in cp.upper():
            return sh
    return "shared"


# ---------------------------------------------------------------------------
# Build the ledger. Each posting is a dict; `transfer` flags an intended
# internal transfer (excluded from all maths).
# ---------------------------------------------------------------------------
postings = []


def emit(account, iso, orig, cur, cp, purpose, transfer=False, decision=None):
    postings.append({
        "account": account, "date": iso, "orig": round(orig, 2), "cur": cur,
        "cp": cp, "purpose": purpose, "transfer": transfer, "decision": decision,
    })


def posting_id(p):
    """The canonical transaction id the pipeline will assign (content hash + #1).
    Every seeded posting below is unique within its file, so occurrence is 1."""
    return "%s#1" % _txn_hash(p["account"], p["date"], p["orig"], p["cur"], p["cp"], p["purpose"])


def d(y, m, day):
    return date(y, m, day).isoformat()


# --- recurring MONTHLY items, every month of every year ---------------------
for y in YEARS:
    for m in range(1, 13):
        # income (salary attributed by account owner; joint salary splits 50/50).
        # Split so the ratio is still exactly 60/40 (L 3000 + couple half 600 = 3600;
        # K 1800 + couple half 600 = 2400) while exercising the couple-income branch.
        emit("db-giro-person1", d(y, m, 28), 3000.00, "EUR", "ARBEITGEBER ALEX GMBH", "Gehalt")
        emit("dkb-person2",    d(y, m, 28), 1800.00, "EUR", "ARBEITGEBER SAM AG", "Gehalt")
        emit("n26-joint",       d(y, m, 28), 1200.00, "EUR", "ARBEITGEBER GEMEINSAM GBR", "Gehalt joint")
        # shared expenses (who pays -> settlement)
        emit("n26-joint",       d(y, m, 1),  -1450.00, "EUR", "VERMIETER HAUSVERWALTUNG", "Miete")      # rent (joint)
        emit("db-giro-person1", d(y, m, 5),  -450.00,  "EUR", "REWE MARKT GMBH", "Lebensmittel")        # groceries (Alex)
        emit("dkb-person2",    d(y, m, 6),  -120.00,  "EUR", "STADTWERKE ENERGIE", "Strom")            # energy (Sam)
        emit("n26-joint",       d(y, m, 7),  -40.00,   "EUR", "TELEKOM INTERNET", "Internet")           # internet (joint)
        emit("n26-joint",       d(y, m, 12), -80.00,   "EUR", "RESTAURANT DA MARIO", "Abendessen")      # restaurants (joint)
        emit("barclays-person2", d(y, m, 15), -100.00,"EUR", "ZARA DEUTSCHLAND", "Kleidung")           # clothing on card (Sam)
        # personal expenses (excluded from settlement, still family expense)
        emit("db-giro-person1", d(y, m, 3),  -30.00,   "EUR", "FITNESS FIRST", "Fitnessstudio")         # gym (Alex personal)
        emit("dkb-person2",    d(y, m, 9),  -25.00,   "EUR", "DM DROGERIE MARKT", "Kosmetik")          # makeup (Sam personal)
        emit("nubank-person1",  d(y, m, 10), -312.50,  "BRL", "NETFLIX BRASIL", "Assinatura")           # BRL streaming (Alex personal) = -50 EUR

        # --- INTERNAL TRANSFER (a): credit-card settlement via MARKER ---
        # Sam's giro pays the Barclays card; text contains a transfer marker.
        emit("dkb-person2",    d(y, m, 16), -100.00,  "EUR", "KREDITKARTENABRECHNUNG BARCLAYS", "Kartenausgleich", transfer=True)

    # --- INTERNAL TRANSFER (b): same-owner cross-currency pair, quarterly ---
    # Alex moves 125 EUR from DB giro to Nubank; arrives as 781.25 BRL (=125 EUR).
    for m in (3, 6, 9, 12):
        emit("db-giro-person1", d(y, m, 20), -125.00,  "EUR", "UMBUCHUNG NUBANK ALEX", "Transfer to Nubank", transfer=True)
        emit("nubank-person1",  d(y, m, 20),  781.25,  "BRL", "UMBUCHUNG DB ALEX", "Transfer from DB", transfer=True)

    # --- INTERNAL TRANSFER (c): the yearly equalization between the partners ---
    # Different owners, so it is detected via an owner NAME in the text.
    emit("db-giro-person1", d(y, 12, 30), -233.00, "EUR", "AUSGLEICH ALEX SAM", "Jahresausgleich", transfer=True)
    emit("dkb-person2",    d(y, 12, 30),  233.00, "EUR", "AUSGLEICH ALEX SAM", "Jahresausgleich", transfer=True)

    # --- yearly income extras (non-ratio income) ---
    emit("db-giro-person1", d(y, 12, 20), 2000.00, "EUR", "BONUS ALEX GMBH", "Jahresbonus")           # salary-extras every year

    # --- out-of-scope (must be invisible to ALL maths), once per year ---
    emit("db-giro-person1", d(y, 4, 8),  -200.00, "EUR", "WORK EXPENSE TRAVEL", "reimbursed by employer")  # oos expense
    emit("db-giro-person1", d(y, 8, 8),   550.00, "EUR", "PRIVATKREDIT FREUND", "loan, not income")        # oos income

# --- per-year ONE-OFFS (make year-over-year meaningful + more edge cases) ----
emit("n26-joint",       d(2021, 5, 4), -1200.00, "EUR", "IKEA EINRICHTUNG", "Sofa")                       # 2021 furniture (shared, joint)
emit("db-giro-person1", d(2022, 6, 4),  -500.00, "EUR", "SPENDE NGO EV", "Jahresspende")                  # 2022 donation (tax bucket)
emit("n26-joint",       d(2022, 6, 15), 1000.00, "EUR", "FINANZAMT REFUND", "Steuererstattung")           # 2022 tax refund (couple income, non-ratio)
emit("n26-joint",       d(2023, 3, 4),  -800.00, "EUR", "ARZT PRAXIS DR SCHMIDT", "Behandlung")           # 2023 medical (shared, tax)
emit("dkb-person2",    d(2023, 7, 4),   300.00, "EUR", "EBAY VERKAUF", "Verkauf Fahrrad")                # 2023 sold-items (Sam income, non-ratio)
emit("db-giro-person1", d(2023, 9, 4),    63.45, "EUR", "REWE MARKT GMBH", "Erstattung Pfand")            # 2023 grocery REIMBURSEMENT (+ offsets groceries)
emit("n26-joint",       d(2024, 4, 4),  -600.00, "EUR", "HANDWERKER MUELLER", "Reparatur")                # 2024 maintenance (shared, tax 35a)
emit("db-giro-person1", d(2025, 5, 4),  2500.00, "EUR", "FREELANCE CLIENT GMBH", "Projekt")               # 2025 freelance (Alex income, non-ratio)
emit("db-giro-person1", d(2025, 8, 4), -1000.00, "EUR", "LUFTHANSA FLUG", "Urlaub")                       # 2025 personal travel (Alex personal)

# --- YEAR-COST items (one big appliance per year, flagged year_cost via a seeded
# decision). Amounts avoid every positive magnitude so no accidental transfer. ---
YEAR_COST_AMOUNTS = {2021: 1500.0, 2022: 1600.0, 2023: 1700.0, 2024: 1850.0, 2025: 1950.0}
for _y, _amt in YEAR_COST_AMOUNTS.items():
    emit("n26-joint", d(_y, 10, 18), -_amt, "EUR", "MEDIA MARKT", "Haushaltsgeraet",
         decision={"year_cost": True})

# --- SPLIT item (Amazon order -> review queue, resolved by a seeded split into a
# shared part and a personal part; parts must sum to the total). 2025 only. ---
emit("db-giro-person1", d(2025, 11, 18), -180.00, "EUR", "AMAZON EU SARL", "Bestellung",
     decision={"splits": [
         {"amount": -120.00, "category": "core-living/items-over-50", "sharing": "shared"},
         {"amount": -60.00, "category": "recreation/hobbies", "sharing": "personal:person1"},
     ]})


# ---------------------------------------------------------------------------
# Derived helpers
# ---------------------------------------------------------------------------
def eur_of(p):
    return round(p["orig"] / BRL_PER_EUR, 2) if p["cur"] == "BRL" else round(p["orig"], 2)


def year_of(p):
    return int(p["date"][:4])


# ---------------------------------------------------------------------------
# SELF-CHECK (both directions): faithfully re-run pipeline/transfers.py's
# detection here and assert the set it detects equals EXACTLY the postings we
# intended as transfers. This catches BOTH:
#   * an ACCIDENTAL pair (a normal txn wrongly detected -> would vanish from maths)
#   * a MISSED intended transfer (mistyped marker/amount/date -> would wrongly
#     count as income/expense).
# ---------------------------------------------------------------------------
def _detected_transfer_ids():
    """Mirror of transfers.mark_internal: markers first, then greedy opposite-
    amount pair matching (same owner OR an owner name in the text), per year."""
    names = [n.lower() for n in OWNER_NAMES]
    markers = [m.lower() for m in TRANSFER_MARKERS]
    detected = set()
    for y in YEARS:
        ps = [p for p in postings if year_of(p) == y]
        for p in ps:
            text = (p["cp"] + " " + p["purpose"]).lower()
            if any(mk in text for mk in markers):
                detected.add(id(p))
        candidates = [p for p in ps if id(p) not in detected]  # "normal" remainder
        by_cents = defaultdict(list)
        for p in candidates:
            by_cents[round(abs(eur_of(p)) * 100)].append(p)
        matched = set()
        for amount, group in by_cents.items():
            if amount == 0 or len(group) < 2:
                continue
            negs = [p for p in group if eur_of(p) < 0]
            poss = [p for p in group if eur_of(p) > 0]
            for n in negs:
                if id(n) in matched:
                    continue
                for pp in poss:
                    if id(pp) in matched or pp["account"] == n["account"]:
                        continue
                    if abs((date.fromisoformat(pp["date"]) - date.fromisoformat(n["date"])).days) > TRANSFER_WINDOW:
                        continue
                    same_owner = ACC[pp["account"]]["owner"] == ACC[n["account"]]["owner"]
                    text = " ".join([n["cp"], n["purpose"], pp["cp"], pp["purpose"]]).lower()
                    named = any(nm in text for nm in names)
                    if same_owner or named:
                        detected.update((id(n), id(pp)))
                        matched.update((id(n), id(pp)))
                        break
    return detected


def self_check_transfers():
    detected = _detected_transfer_ids()
    intended = {id(p) for p in postings if p["transfer"]}
    by_id = {id(p): p for p in postings}
    accidental = detected - intended
    missed = intended - detected
    if accidental:
        raise AssertionError("ACCIDENTAL transfer(s) would be detected: "
                             + "; ".join("%s %s %.2f" % (by_id[i]["date"], by_id[i]["cp"], eur_of(by_id[i])) for i in accidental))
    if missed:
        raise AssertionError("Intended transfer NOT detected (would count as income/expense): "
                             + "; ".join("%s %s %.2f" % (by_id[i]["date"], by_id[i]["cp"], eur_of(by_id[i])) for i in missed))
    return len(intended)


# ---------------------------------------------------------------------------
# Compute the EXPECTED oracle (independent arithmetic)
# ---------------------------------------------------------------------------
def compute_expected():
    people = ["person1", "person2"]
    per_year = {}
    for y in YEARS:
        ps = [p for p in postings if year_of(p) == y]
        income = expense = 0.0
        by_cat = defaultdict(float)
        ratio_income = {"person1": 0.0, "person2": 0.0}
        total_shared = 0.0
        shared_paid = {"person1": 0.0, "person2": 0.0}
        counts = {"transfers": 0, "out_of_scope": 0, "income_txns": 0, "expense_txns": 0}
        counts["year_cost"] = counts.get("year_cost", 0)
        yc_expense = 0.0
        yc_by_cat = defaultdict(float)
        yc_ids = set()
        for p in ps:
            if p["transfer"]:
                counts["transfers"] += 1
                continue
            dec = p.get("decision") or {}
            year_cost = bool(dec.get("year_cost"))
            owner = ACC[p["account"]]["owner"]
            # expand splits into (amount, category, sharing) parts; else one part
            if dec.get("splits"):
                parts = [(sp["amount"], sp.get("category"), sp.get("sharing", "shared")) for sp in dec["splits"]]
            else:
                parts = [(eur_of(p), category_of(p["cp"]), sharing_of(p["cp"]))]
            if year_cost:
                counts["year_cost"] += 1
                yc_ids.add(posting_id(p))
            for amt, cat, sharing in parts:
                if sharing == "out-of-scope":
                    counts["out_of_scope"] += 1
                    continue
                by_cat[cat or "uncategorized"] += amt
                is_income = cat in INCOME_CATS
                if is_income:
                    income += amt
                    counts["income_txns"] += 1
                    if cat in RATIO_CATS:
                        if owner == "couple":
                            for q in people:
                                ratio_income[q] += amt / 2
                        else:
                            ratio_income[owner] += amt
                else:
                    expense += amt
                    counts["expense_txns"] += 1
                    if year_cost:
                        yc_expense += amt
                        yc_by_cat[cat or "uncategorized"] += amt
                    if sharing == "shared":
                        total_shared += -amt
                        if owner == "couple":
                            for q in people:
                                shared_paid[q] += (-amt) * 0.5
                        else:
                            shared_paid[owner] += -amt
        total_ratio = ratio_income["person1"] + ratio_income["person2"]
        ratio = {q: ratio_income[q] / total_ratio for q in people} if total_ratio else {q: 0.5 for q in people}
        balance = {q: round(shared_paid[q] - ratio[q] * total_shared, 2) for q in people}
        per_year[y] = {
            "income": round(income, 2),
            "expenses": round(expense, 2),
            "savings": round(income + expense, 2),
            "savings_rate": round((income + expense) / income, 4) if income else None,
            "by_category": {k: round(v, 2) for k, v in sorted(by_cat.items())},
            "ratio": {q: round(ratio[q], 4) for q in people},
            "ratio_income": {q: round(ratio_income[q], 2) for q in people},
            "total_shared": round(total_shared, 2),
            "shared_paid": {q: round(shared_paid[q], 2) for q in people},
            "settlement_balance": balance,
            "year_costs": {"expenses": round(yc_expense, 2), "transactions": len(yc_ids),
                           "by_category": {k: round(v, 2) for k, v in sorted(yc_by_cat.items())}},
            "counts": counts,
        }
    return per_year


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------
def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def write_accounts():
    data = {
        "_help": "TEST FIXTURE accounts (generated by scripts/gen_test_fixture.py).",
        "owner_names": OWNER_NAMES,
        "accounts": [{k: a[k] for k in ("id", "owner", "bank", "type", "currency", "iban")} for a in ACCOUNTS],
        "transfer_markers": TRANSFER_MARKERS,
    }
    write_json(os.path.join(ROOT, "data", "accounts.json"), data)


def write_rules():
    rules = []
    for needle in REVIEW_MERCHANTS:  # multi-purpose merchants -> review queue (resolved by a split decision)
        rules.append({"id": "fixture-review-%s" % needle.lower(),
                      "match": {"field": "any", "contains": needle}, "action": "review"})
    for i, (needle, cat, sharing) in enumerate(RULE_TABLE):
        r = {"id": "fixture-%02d" % i, "match": {"field": "any", "contains": needle}}
        if cat is not None:
            r["category"] = cat
        r["sharing"] = sharing
        rules.append(r)
    write_json(os.path.join(ROOT, "rules", "merchant-rules.json"),
               {"_help": "TEST FIXTURE rules (generated).", "rules": rules})


def write_decisions():
    """Seed data/<year>/decisions.json for the postings that carry a decision
    (year_cost flags and the Amazon split), keyed by the canonical txn id."""
    by_year = defaultdict(dict)
    for p in postings:
        if p.get("decision"):
            by_year[year_of(p)][posting_id(p)] = p["decision"]
    for y, decs in by_year.items():
        write_json(os.path.join(ROOT, "data", str(y), "decisions.json"), decs)
    return {y: len(d) for y, d in by_year.items()}


def write_fx():
    """Pinned ECB cache so BRL conversion is deterministic and offline.
    Run `.venv/bin/python -m pipeline.cli fx-update` later to restore real rates."""
    start, end = date(YEARS[0], 1, 1), date(YEARS[-1], 12, 31)
    rows = []
    cur = start
    while cur <= end:
        rows.append((cur.isoformat(), "1.1000", "%.4f" % BRL_PER_EUR))
        cur += timedelta(days=1)
    path = os.path.join(ROOT, "data", "fx", "eurofxref-hist.csv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "USD", "BRL"])
        w.writerows(rows)


def _fmt_amount(v, decimal):
    s = "%.2f" % v
    return s.replace(".", ",") if decimal == "comma" else s


def _fmt_date(iso, fmt):
    y, m, day = iso.split("-")
    if fmt == "%d.%m.%Y":
        return "%s.%s.%s" % (day, m, y)
    if fmt == "%d.%m.%y":
        return "%s.%s.%s" % (day, m, y[2:])
    return iso  # %Y-%m-%d


def write_csvs():
    inbox = os.path.join(ROOT, "inbox")
    os.makedirs(inbox, exist_ok=True)
    by_acct = defaultdict(list)
    for p in postings:
        by_acct[p["account"]].append(p)

    # each format: (delimiter, decimal, date_fmt, header, row_builder)
    def db_giro_rows(ps):
        yield ["Buchungstag", "Begünstigter / Auftraggeber", "Verwendungszweck", "Betrag", "Währung", "IBAN / Kontonummer"]
        for p in sorted(ps, key=lambda x: x["date"]):
            yield [_fmt_date(p["date"], "%d.%m.%Y"), p["cp"], p["purpose"], _fmt_amount(p["orig"], "comma"), "EUR", ""]

    def dkb_rows(ps):
        yield ["Buchungsdatum", "Zahlungsempfänger*in", "Verwendungszweck", "Betrag (€)", "IBAN"]
        for p in sorted(ps, key=lambda x: x["date"]):
            yield [_fmt_date(p["date"], "%d.%m.%y"), p["cp"], p["purpose"], _fmt_amount(p["orig"], "comma"), ""]

    def barclays_rows(ps):
        yield ["Referenznummer", "Buchungsdatum", "Beschreibung", "Betrag"]
        for i, p in enumerate(sorted(ps, key=lambda x: x["date"])):
            yield ["R%05d" % i, _fmt_date(p["date"], "%d.%m.%Y"), p["cp"] + " " + p["purpose"], _fmt_amount(p["orig"], "comma")]

    def n26_rows(ps):
        yield ["Booking Date", "Partner Name", "Payment Reference", "Amount (EUR)", "Partner Iban"]
        for p in sorted(ps, key=lambda x: x["date"]):
            yield [p["date"], p["cp"], p["purpose"], _fmt_amount(p["orig"], "dot"), ""]

    def nubank_conta_rows(ps):
        # The oracle must enter through a real bank-export shape.  Using the normalized
        # extractor shape here previously forced production to trust that shape without
        # its reconciliation report just to keep a test fixture convenient.
        yield ["Data", "Valor", "Identificador", "Descrição"]
        for index, p in enumerate(sorted(ps, key=lambda x: x["date"]), start=1):
            yield [_fmt_date(p["date"], "%d/%m/%Y"), _fmt_amount(p["orig"], "dot"),
                   "fixture-%05d" % index, (p["cp"] + " " + p["purpose"]).strip()]

    builders = {
        "db-giro": (";", db_giro_rows),
        "dkb": (";", dkb_rows),
        "barclays": (";", barclays_rows),
        "n26": (",", n26_rows),
        "nubank-conta": (",", nubank_conta_rows),
    }
    written = []
    for acct_id, ps in by_acct.items():
        delim, builder = builders[ACC[acct_id]["fmt"]]
        path = os.path.join(inbox, "%s__fixture.csv" % acct_id)
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=delim, quoting=csv.QUOTE_MINIMAL)
            for row in builder(ps):
                w.writerow(row)
        written.append((os.path.basename(path), len(ps)))
    return written


def write_expected(expected):
    lines = ["# Expected results — deterministic test fixture", "",
             "Generated by `scripts/gen_test_fixture.py`. These numbers are computed by",
             "independent arithmetic (not by the app's settle.py) and are the oracle to",
             "compare the website against. All amounts in EUR.", "",
             "**Accounts:** " + ", ".join("%s (%s)" % (a["id"], a["owner"]) for a in ACCOUNTS), "",
             "**FX:** pinned 1 EUR = %.2f BRL." % BRL_PER_EUR, ""]
    tot = defaultdict(float)
    for y in YEARS:
        e = expected[y]
        lines += [
            "## %d" % y, "",
            "| Metric | Value |", "|---|---|",
            "| Income | %.2f |" % e["income"],
            "| Expenses | %.2f |" % e["expenses"],
            "| Savings | %.2f |" % e["savings"],
            "| Savings rate | %s |" % ("%.1f %%" % (e["savings_rate"] * 100) if e["savings_rate"] is not None else "–"),
            "| Settlement ratio (Alex/Sam) | %.4f / %.4f |" % (e["ratio"]["person1"], e["ratio"]["person2"]),
            "| Total shared expenses | %.2f |" % e["total_shared"],
            "| Shared paid — Alex | %.2f |" % e["shared_paid"]["person1"],
            "| Shared paid — Sam | %.2f |" % e["shared_paid"]["person2"],
            "| Settlement balance — Alex | %+.2f |" % e["settlement_balance"]["person1"],
            "| Settlement balance — Sam | %+.2f |" % e["settlement_balance"]["person2"],
            "| Internal transfers (excluded) | %d |" % e["counts"]["transfers"],
            "| Out-of-scope (excluded) | %d |" % e["counts"]["out_of_scope"],
            "| Year-cost items (in annual, out of monthly) | %d, total %.2f |" % (e["year_costs"]["transactions"], e["year_costs"]["expenses"]),
            "",
            "By category:", "",
        ]
        for cat, v in e["by_category"].items():
            lines.append("- `%s`: %.2f" % (cat, v))
        lines.append("")
        tot["income"] += e["income"]
        tot["expenses"] += e["expenses"]
        tot["savings"] += e["savings"]
    lines += ["## 5-year totals", "",
              "- Income: %.2f" % tot["income"],
              "- Expenses: %.2f" % tot["expenses"],
              "- Savings: %.2f" % tot["savings"], "",
              "### Interpretation notes", "",
              "- A positive settlement balance for a person = they paid MORE than their",
              "  fair (income-proportional) share of shared costs, so the other owes them.",
              "- Three transfer TYPES are exercised; **22 postings per year across 17 events**:",
              "  12 credit-card-settlement markers, 4 two-sided DB→Nubank cross-currency",
              "  pairs (8 postings), and 1 two-sided yearly equalization pair (2 postings).",
              "  None may appear in income, expenses, or settlement.",
              "- The two out-of-scope lines per year MUST be invisible everywhere.",
              "- The 2023 REWE reimbursement (+63.45) reduces the groceries category total.",
              "- salary-extras / tax-refund / sold-items / freelance are income but do NOT",
              "  change the settlement ratio (only salary does).",
              "- **Couple income 50/50**: salary is split Alex 3000 + Sam 1800 +",
              "  joint 1200/mo. The joint 1200 lands in the couple account and is attributed",
              "  600/600, so the ratio is still exactly 60/40 while the couple-income branch",
              "  is exercised. (Couple 50/50 on the *paid* side is also tested: the joint",
              "  account pays rent/internet/restaurants.)", "",
              "### Covered via seeded decisions (data/<year>/decisions.json)", "",
              "- **year_cost**: one appliance per year (MEDIA MARKT) is flagged year_cost —",
              "  included in annual totals, excluded from the monthly picture, and shown on",
              "  the dashboard 'Year costs' tab.",
              "- **splits**: a 2025 Amazon order (−180) is split into a shared part",
              "  (−120 items-over-50) and a personal part (−60 hobbies); parts sum to the total.",
              "", "### Still NOT covered", "",
              "- FX only exercises exact divisions (BRL/6.25); cent-rounding boundaries,",
              "  date-varying rates, and weekend fallback are not tested.",
              "- Other untested branches: uncategorized positive-income fallback, negative",
              "  income / reversals, explicit `income_owner` overrides, zero-salary",
              "  reference-ratio fallback, and closed-month rejection.", ""]
    with open(os.path.join(ROOT, "scripts", "EXPECTED_RESULTS.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    write_json(os.path.join(ROOT, "scripts", "expected.json"), expected)


def main():
    n_intended = self_check_transfers()
    expected = compute_expected()
    write_accounts()
    write_rules()
    write_fx()
    written = write_csvs()
    seeded = write_decisions()
    write_expected(expected)
    print("Self-check passed: detected transfers == intended (%d/year, both directions)." % (n_intended // len(YEARS)))
    print("Seeded decisions (year_cost + split): %s" % dict(sorted(seeded.items())))
    print("Postings: %d across %s" % (len(postings), YEARS))
    for name, n in sorted(written):
        print("  inbox/%-32s %d rows" % (name, n))
    print("Wrote data/accounts.json, rules/merchant-rules.json, data/fx cache, scripts/EXPECTED_RESULTS.md, scripts/expected.json")
    print("\nIMPORTANT: the FX cache was replaced with pinned rates. If a dev server is")
    print("running, RESTART it before ingesting — pipeline/fx.py caches rates in memory.")
    print("Restore real ECB rates later with: .venv/bin/python -m pipeline.cli fx-update")
    print("\nNext: .venv/bin/python -m pipeline.cli ingest   (then check dashboards vs EXPECTED_RESULTS.md)")


if __name__ == "__main__":
    main()
