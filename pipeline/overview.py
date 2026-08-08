"""The all-years picture: every year on record summed into one set of figures.

Every other view in this app answers "how was this year" or "how was this month". This one
answers "how has it been", which is a different question only in its window — the math is
`settle.year_summary` per year, added up. Nothing new is computed here and nothing is stored;
widening the window must not quietly change what a euro means.

Two windows meet on the overview page and they are not the same window: the ledger starts at
the first ingested transaction, while liquid net worth starts at the first recorded balance.
`first_year`/`last_year` describe the ledger one only, and the net-worth chart is fetched
separately and labelled with its own span.

Year costs are the one place where summing needs care. A year total includes them; the twelve
monthly figures inside that year deliberately exclude them (an e-bike should not distort June).
So the monthly series is reported as it stands and each year's `year_costs` bucket travels with
it, letting the caller amortize them across that year's own months — never across another
year's — exactly as the dashboard does.
"""
from datetime import date as date_type

from . import money, settle, store
from .util import cents


def _elapsed_months(year, today):
    """How many months of `year` have actually happened, as of `today`.

    A running year is not twelve months long, and spreading its year costs over twelve
    would push cost into months that do not exist yet.
    """
    if year < today.year:
        return 12
    if year > today.year:
        return 0
    return today.month


def series(scope="all", today=None):
    today = today or date_type.today()
    years = store.years()
    out_years = []
    totals = {"income": 0, "expenses": 0, "savings": 0, "transactions": 0, "needs_review": 0}
    by_category = {}

    for year in years:
        s = settle.year_summary(year, scope=scope)
        yc = s.get("year_costs") or {}
        out_years.append({
            "year": year,
            "income": s["income"],
            "expenses": s["expenses"],
            "savings": s["savings"],
            "transactions": s["transactions"],
            "needs_review": s["needs_review"],
            # The months carry no year costs; this bucket is what was held back from them.
            "year_costs": {"income": yc.get("income", 0.0), "expenses": yc.get("expenses", 0.0)},
            "elapsed_months": _elapsed_months(year, today),
            "months": [{"year": year, "month": m["month"], "income": m["income"],
                        "expenses": m["expenses"], "savings": m["savings"]} for m in s["months"]],
        })
        # Cents, added as integers. Each year's figure is already exact to the cent, so
        # summing them as floats would be the one place a whole-history total could drift
        # away from the years printed beside it.
        for key in ("income", "expenses", "savings"):
            totals[key] += cents(s[key])
        totals["transactions"] += s["transactions"]
        totals["needs_review"] += s["needs_review"]
        for slug, value in s["by_category"].items():
            by_category[slug] = by_category.get(slug, 0) + cents(value)

    for key in ("income", "expenses", "savings"):
        totals[key] = money.from_cents(totals[key])
    # A savings rate needs income to be a rate of; with none, say so rather than print 0 %.
    totals["savings_rate"] = (round(totals["savings"] / totals["income"], 4)
                              if totals["income"] > 0 else None)

    return {
        "scope": scope,
        "years": out_years,
        "totals": totals,
        "by_category": {k: money.from_cents(v) for k, v in sorted(by_category.items())},
        "first_year": years[0] if years else None,
        "last_year": years[-1] if years else None,
    }
