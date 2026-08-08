"""How a foreign amount became the euro amount the totals use.

Every non-EUR row in the store carries a euro figure that nothing in the app can
currently explain. Dashboards, settlement and tax all consume it; nobody can point at
a rate, a publication date and an arithmetic step and reproduce it. This module is the
explanation, and only the explanation.

Three distinctions it exists to keep straight, because collapsing any of them produces
a confident wrong answer:

  transaction date is not publication date — the ECB publishes on business days, so a
  Saturday booking is converted at Friday's rate, and saying "converted at the rate of
  <transaction date>" is simply false for roughly two days in seven;

  ECB bookkeeping conversion is not the bank's conversion — UnPAIN books foreign spend
  at the ECB reference rate. A card issuer uses its own rate and spread. A difference
  between them is not an error in either;

  a stale cache is not a wrong past conversion — rates published years ago do not
  change. An out-of-date cache is a reason to run `fx-update` before importing new
  foreign rows, and no reason at all to distrust old ones.

Read-only in the strongest sense: it never downloads, never writes, and its output is
never an input to a total. A discrepancy is reported for a human to act on; nothing
here revalues anything (see the plan's Decision 7).
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from . import fx, settle, store
from .util import cents, load_accounts

# `Decimal` appears here and nowhere else in the pipeline. The audit's whole job is to
# show the exact quotient before rounding — the number a float cannot represent and the
# reason a cent can move. Every cent *comparison* still goes through cents().
CENT = Decimal("0.01")

STATUSES = ("ok", "rate-mismatch", "amount-mismatch", "missing-rate", "legacy-date-derived")


def _exact_conversion(amount_original, rate):
    """The unrounded quotient, as a decimal rather than a float."""
    return Decimal(str(amount_original)) / Decimal(str(rate))


def _plain(value, places=10):
    """A decimal a person can read: never scientific notation, never a wall of zeros.

    Decimal normalizes -62.00/6.20 to -1E+1, which is arithmetically impeccable and
    useless on screen — the whole point of showing the quotient is that someone can
    compare it to the rounded figure below it by eye.
    """
    quantized = value.quantize(Decimal(1).scaleb(-places))
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0")
        whole, _, fraction = text.partition(".")
        text = "%s.%s" % (whole, (fraction + "00")[:max(2, len(fraction))])
    return text


def _status_for(stored_rate, cached_rate, stored_eur_cents, expected_eur_cents, derived_date):
    """One place decides what a row's status is.

    Ordered by how much it matters: a euro figure that cannot be reproduced outranks a
    rate that has since been restated, which outranks not knowing which day was used.
    """
    if cached_rate is None:
        return "missing-rate"
    if stored_eur_cents != expected_eur_cents:
        return "amount-mismatch"
    if stored_rate is not None and cents(stored_rate * 10000) != cents(cached_rate * 10000):
        return "rate-mismatch"
    if derived_date:
        return "legacy-date-derived"
    return "ok"


def audit_transaction(txn, cache_present=True):
    """Explain one stored conversion, step by step.

    `fx_rate_date` is stored from now on. A row imported before that carries only the
    rate, so the publication date is re-derived from the read-only cache and labelled
    `legacy-date-derived` — derived is not the same as recorded, and pretending it was
    recorded at import would be the audit telling its own small lie.
    """
    currency = (txn.get("currency") or "EUR").upper()
    stored_rate = txn.get("fx_rate")
    stored_date = txn.get("fx_rate_date")
    amount_original = txn.get("amount_original")
    stored_eur_cents = cents(txn.get("amount_eur") or 0)

    details, lookup_error = None, None
    if cache_present:
        try:
            details = fx.rate_details(currency, stored_date or txn["date"], allow_download=False)
        except LookupError as exc:
            lookup_error = str(exc)

    cached_rate = details["rate"] if details else None
    rate_date = details["rate_date"] if details else stored_date
    derived_date = stored_date is None and rate_date is not None
    # How far the rate's publication date sits from the booking date — which is the
    # question a reader has ("why does a Saturday purchase say Friday?"). Measuring it
    # from the *stored* rate date instead, as the raw lookup does, always answers zero:
    # looking up the date you already resolved never has to walk back.
    fallback_days = None
    if rate_date and txn.get("date"):
        try:
            fallback_days = (date.fromisoformat(txn["date"]) - date.fromisoformat(rate_date)).days
        except ValueError:
            fallback_days = None

    # The euro figure is reproduced from the rate that was *stored*, because that is
    # what the conversion actually used. The cached rate is shown beside it so a
    # restated ECB series is visible without being mistaken for an arithmetic error.
    basis_rate = stored_rate if stored_rate else cached_rate
    exact = _exact_conversion(amount_original, basis_rate) if basis_rate else None
    expected_eur_cents = cents(exact.quantize(CENT)) if exact is not None else None

    status = _status_for(stored_rate, cached_rate, stored_eur_cents,
                         expected_eur_cents if expected_eur_cents is not None else stored_eur_cents,
                         derived_date)
    return {
        "id": txn.get("id"),
        "date": txn.get("date"),
        "counterparty": txn.get("counterparty") or "",
        "purpose": txn.get("purpose") or "",
        "account": txn.get("account"),
        "currency": currency,
        "amount_original": amount_original,
        "original_cents": cents(amount_original or 0),
        "requested_rate_date": stored_date or txn.get("date"),
        "rate_date": rate_date,
        "rate_date_derived": derived_date,
        "fallback_days": fallback_days,
        "stored_rate": stored_rate,
        "cached_rate": cached_rate,
        "exact_eur": _plain(exact) if exact is not None else None,
        "expected_eur_cents": expected_eur_cents,
        "stored_eur_cents": stored_eur_cents,
        # Positive means the stored figure is above the exact quotient. Expressed in
        # thousandths of a cent because the interesting rounding is well under one.
        "rounding_delta_millicents": (int((Decimal(stored_eur_cents) / 100 - exact) * 100000)
                                      if exact is not None else None),
        "status": status,
        "source_file": (txn.get("source") or {}).get("file"),
        "source_upload": (txn.get("source") or {}).get("upload"),
        "lookup_error": lookup_error,
    }


def summarize_by_currency(items):
    """Per-currency reconciliation, in integer cents.

    The stored total here must equal what the dashboards add up for the same rows —
    that equality is the point of the whole view.
    """
    groups = defaultdict(list)
    for item in items:
        groups[item["currency"]].append(item)
    out = []
    for currency in sorted(groups):
        rows = groups[currency]
        dates = sorted(row["date"] for row in rows)
        rate_dates = sorted(row["rate_date"] for row in rows if row["rate_date"])
        out.append({
            "currency": currency,
            "transactions": len(rows),
            "original_cents": sum(row["original_cents"] for row in rows),
            "stored_eur_cents": sum(row["stored_eur_cents"] for row in rows),
            "expected_eur_cents": sum(row["expected_eur_cents"] or row["stored_eur_cents"]
                                      for row in rows),
            "rounding_delta_millicents": sum(row["rounding_delta_millicents"] or 0 for row in rows),
            "discrepancies": sum(1 for row in rows if row["status"] not in ("ok", "legacy-date-derived")),
            "date_min": dates[0], "date_max": dates[-1],
            "rate_date_min": rate_dates[0] if rate_dates else None,
            "rate_date_max": rate_dates[-1] if rate_dates else None,
        })
    return out


def audit_year(year, scope="all"):
    """Every non-EUR conversion in one year, explained and reconciled.

    Reads the effective view so an account correction shows the account the rest of the
    app shows — but every conversion fact comes from the canonical row, because that is
    what the conversion was performed on. Splits add no conversions: the bank converted
    the parent once, and counting parts would report the same euro twice.
    """
    accounts, _ = load_accounts()
    income_cats = settle.income_categories()
    cache = fx.cache_info()
    items = []
    for txn in store.effective_year(year):
        if (txn.get("currency") or "EUR").upper() == "EUR":
            continue
        if scope != "all":
            lines = [line for line in settle.entries([txn])
                     if settle.in_scope(line, scope, income_cats, accounts)]
            if not lines:
                continue
        items.append(audit_transaction(txn, cache_present=cache["present"]))
    # Deterministic order: date, then id. Same store, same answer, every time.
    items.sort(key=lambda row: (row["date"] or "", row["id"] or ""))
    by_currency = summarize_by_currency(items)
    covered = all(row["cached_rate"] is not None for row in items)
    return {
        "year": int(year),
        "scope": scope,
        "cache": dict(cache, coverage_complete=covered),
        "summary": {
            "transactions": len(items),
            "currencies": len(by_currency),
            "stored_eur_cents": sum(row["stored_eur_cents"] for row in items),
            "expected_eur_cents": sum(row["expected_eur_cents"] or row["stored_eur_cents"]
                                      for row in items),
            "discrepancies": sum(1 for row in items
                                 if row["status"] not in ("ok", "legacy-date-derived")),
            "legacy_dates": sum(1 for row in items if row["rate_date_derived"]),
        },
        "by_currency": by_currency,
        "items": items,
    }
