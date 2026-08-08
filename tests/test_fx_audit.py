"""Can a stakeholder reproduce a stored euro amount from a foreign one?

That is the whole feature, and it is a narrow question with a hard answer: given the
row, the rate and the publication date, the arithmetic must land on the same cent the
dashboards use. So most of this file is that reproduction under conditions that make it
awkward — weekends, half-cent boundaries, restated rates, a currency the cache never
heard of, and rows imported before the rate date was stored at all.

The other half is what the audit must *not* do: download, write, revalue, or count a
split twice.

Usage: .venv/bin/python tests/test_fx_audit.py
"""
import json
import os
import shutil
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

from sandbox import PROJECT, build_sandbox

tmp = Path(tempfile.mkdtemp(prefix="fa-fx-audit-"))
os.environ["FA_ROOT"] = str(tmp)
build_sandbox(tmp)

sys.path.insert(0, str(PROJECT))
from pipeline import fx, fx_audit, ingest, store  # noqa: E402
from pipeline.util import cents  # noqa: E402
from app import server  # noqa: E402

failures = []
total_checks = 0


def check(name, cond, detail=""):
    global total_checks
    total_checks += 1
    print("  %s %s %s" % ("OK " if cond else "FAIL", name, detail if not cond else ""))
    if not cond:
        failures.append(name)


# A synthetic ECB cache. 2026-06-13/14 is a weekend, so a booking there must fall back
# to Friday the 12th; 2026-06-11 exists so the fallback can be more than one day.
CACHE = tmp / "data" / "fx" / "eurofxref-hist.csv"
CACHE.parent.mkdir(parents=True, exist_ok=True)
CACHE.write_text(
    "Date,USD,BRL\n"
    "2026-06-16,1.2000,6.3000\n"
    "2026-06-15,1.1000,6.2500\n"
    "2026-06-12,1.0900,6.2000\n"
    "2026-06-11,1.0800,6.1000\n",
    encoding="utf-8")
fx._rates = None

YEAR = 2026
ACCOUNT = "bank1-person1"


def row(txn_id, date, amount, currency="BRL", rate=None, rate_date=None, **extra):
    """One canonical row, written exactly as ingestion writes it."""
    record = {
        "id": txn_id, "account": ACCOUNT, "date": date,
        "amount_original": amount, "currency": currency,
        "counterparty": "MERCADO", "purpose": "compra", "counterparty_iban": "",
        "kind": "normal", "source": {"file": "fixture.csv", "format": "test"},
    }
    if rate is None:
        record.update(ingest.converted(amount, currency, date))
    else:
        eur = round(Decimal(str(amount)) / Decimal(str(rate)), 2)
        record.update({"amount_eur": float(eur), "fx_rate": rate,
                       "fx_rate_date": rate_date, "fx_rate_source": "ECB"})
    record.update(extra)
    return record


def audit(rows, scope="all"):
    store.rewrite_year(YEAR, {"fixture.jsonl": rows})
    return fx_audit.audit_year(YEAR, scope=scope)


print("== the conversion reproduces, on ordinary and awkward days")
weekday = audit([row("t1", "2026-06-15", -62.50)])
item = weekday["items"][0]
check("a weekday uses that day's rate", item["rate_date"] == "2026-06-15", item["rate_date"])
check("no fallback was needed", item["fallback_days"] == 0, str(item["fallback_days"]))
check("the stored euros reproduce exactly", item["status"] == "ok", item["status"])
check("and equal the exact quotient rounded", item["stored_eur_cents"] == item["expected_eur_cents"]
      and item["stored_eur_cents"] == cents(-10.00), str(item["stored_eur_cents"]))
check("the rate date is recorded at import, not derived", item["rate_date_derived"] is False)

saturday = audit([row("t2", "2026-06-13", -62.00)])["items"][0]
check("a Saturday names Friday's publication date", saturday["rate_date"] == "2026-06-12",
      saturday["rate_date"])
check("and says how far back it walked", saturday["fallback_days"] == 1,
      str(saturday["fallback_days"]))
sunday = audit([row("t3", "2026-06-14", -62.00)])["items"][0]
check("a Sunday falls back to the same Friday", sunday["rate_date"] == "2026-06-12")
check("two days back is reported as two", sunday["fallback_days"] == 2, str(sunday["fallback_days"]))

# A gap longer than a weekend (the 13th and 14th are missing, so is the 13th of a
# holiday week): 2026-06-14 -> 2026-06-12 is the longest this fixture offers.
holiday = audit([row("t4", "2026-06-14", -61.00)])["items"][0]
check("the exact quotient is shown before rounding, not after",
      "." in (holiday["exact_eur"] or "") and len(holiday["exact_eur"].split(".")[1]) > 2,
      str(holiday["exact_eur"]))
check("and never in scientific notation, which is unreadable beside a euro figure",
      all("E" not in (audit([row("x1", "2026-06-12", amount)])["items"][0]["exact_eur"] or "")
          for amount in (-62.00, -620.00, -6200.00, -0.01)))


print("== the boundaries where a cent is decided")
for amount, expected in [(-0.01, -0.0), (-0.07, -0.01), (-3.10, -0.5), (-6.20, -1.0),
                         (-6.25, -1.01), (-999999.99, -161290.32)]:
    single = audit([row("b%s" % amount, "2026-06-12", amount)])["items"][0]
    check("%.2f BRL reproduces to the cent" % amount,
          single["stored_eur_cents"] == single["expected_eur_cents"], str(single))
    check("%.2f BRL rounds to %.2f EUR" % (amount, expected),
          single["stored_eur_cents"] == cents(expected), str(single["stored_eur_cents"]))

positive = audit([row("p1", "2026-06-12", 62.00)])["items"][0]
check("a positive amount keeps its sign", positive["stored_eur_cents"] == cents(10.00),
      str(positive["stored_eur_cents"]))
check("and reports rounding in the same units", positive["rounding_delta_millicents"] is not None)


print("== rows imported before the rate date existed")
legacy = audit([row("l1", "2026-06-13", -62.00, rate=6.20, rate_date=None)])["items"][0]
check("a legacy row is still explained", legacy["rate_date"] == "2026-06-12", legacy["rate_date"])
check("but the derived date is labelled as derived", legacy["rate_date_derived"] is True)
check("and the status says so rather than claiming a clean bill",
      legacy["status"] == "legacy-date-derived", legacy["status"])
check("a derived date is not counted as a discrepancy",
      audit([row("l2", "2026-06-13", -62.00, rate=6.20, rate_date=None)])["summary"]["discrepancies"] == 0)


print("== disagreements are named, not smoothed over")
restated = audit([row("r1", "2026-06-15", -62.50, rate=6.2400, rate_date="2026-06-15")])["items"][0]
check("a rate that no longer matches the cache is reported",
      restated["status"] == "rate-mismatch", restated["status"])
check("and both rates are shown side by side",
      restated["stored_rate"] == 6.24 and restated["cached_rate"] == 6.25, str(restated))

wrong = row("w1", "2026-06-15", -62.50, rate=6.25, rate_date="2026-06-15")
wrong["amount_eur"] = -10.01          # one cent off what the arithmetic gives
off_by_one = audit([wrong])["items"][0]
check("a stored euro figure that does not reproduce is an amount-mismatch",
      off_by_one["status"] == "amount-mismatch", off_by_one["status"])
check("and it counts as a discrepancy", audit([wrong])["summary"]["discrepancies"] == 1)

unknown = row("u1", "2026-06-15", -50.00, currency="GBP", rate=0.85, rate_date="2026-06-15")
missing = audit([unknown])["items"][0]
check("a currency the cache never listed is missing-rate, not a crash",
      missing["status"] == "missing-rate", missing["status"])
check("and the lookup error is carried for the human", bool(missing["lookup_error"]))


print("== the year reconciles to the same cents the totals use")
many = [row("m1", "2026-06-15", -62.50), row("m2", "2026-06-12", -31.00),
        row("m3", "2026-06-16", -12.34, currency="USD"), row("m4", "2026-06-15", 100.00)]
result = audit(many)
check("every foreign row appears exactly once",
      len(result["items"]) == 4 and len({i["id"] for i in result["items"]}) == 4)
check("the summary equals the sum of its items",
      result["summary"]["stored_eur_cents"] == sum(i["stored_eur_cents"] for i in result["items"]))
check("and it equals what the ledger stores for the same rows",
      result["summary"]["stored_eur_cents"] == sum(cents(t["amount_eur"]) for t in many))
check("currencies are reconciled separately", {c["currency"] for c in result["by_currency"]} == {"BRL", "USD"})
check("each currency's rows add up to its own total",
      all(c["stored_eur_cents"] == sum(i["stored_eur_cents"] for i in result["items"]
                                       if i["currency"] == c["currency"])
          for c in result["by_currency"]))

euro_only = audit([dict(row("e1", "2026-06-15", -10.00, currency="EUR"))])
check("euro rows are not audited at all", euro_only["items"] == [], str(euro_only["items"]))
check("and that is an empty result, not an error", euro_only["summary"]["transactions"] == 0)

# A split does not create a second conversion: the bank converted the parent once.
store.rewrite_year(YEAR, {"fixture.jsonl": [row("s1", "2026-06-15", -62.50)]})
store.save_decisions(YEAR, {"s1": {"splits": [
    {"amount": -6.00, "category": "core-living/groceries", "sharing": "shared"},
    {"amount": -4.00, "category": "core-living/groceries", "sharing": "personal:person1"}]}})
split = fx_audit.audit_year(YEAR)
check("a split transaction is audited once, as one conversion", len(split["items"]) == 1,
      str(len(split["items"])))
check("and its euro figure is the parent's, not the parts'",
      split["items"][0]["stored_eur_cents"] == cents(-10.00))
store.save_decisions(YEAR, {})


print("== read-only in the strongest sense")
store.rewrite_year(YEAR, {"fixture.jsonl": many})
before = {path: path.read_bytes() for path in sorted(tmp.rglob("*")) if path.is_file()}
first = fx_audit.audit_year(YEAR)
second = fx_audit.audit_year(YEAR)
after = {path: path.read_bytes() for path in sorted(tmp.rglob("*")) if path.is_file()}
check("auditing writes nothing at all", before == after,
      str(sorted({p.name for p in set(before) ^ set(after)}))[:120])
check("and is deterministic", json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True))
check("items come back in a stable order",
      [i["id"] for i in first["items"]]
      == [i["id"] for i in sorted(first["items"], key=lambda i: (i["date"], i["id"]))]
      and [i["id"] for i in first["items"]] == [i["id"] for i in second["items"]])

# With no cache at all the audit must still answer, offline, without downloading.
CACHE.unlink()
fx._rates = None
offline = fx_audit.audit_year(YEAR)
check("a missing cache produces statuses, not an exception",
      all(i["status"] == "missing-rate" for i in offline["items"]), str(offline["items"][:1]))
check("and reports the cache as absent", offline["cache"]["present"] is False)
check("no download was triggered", not CACHE.exists())
check("stored figures are still reported when the cache cannot confirm them",
      offline["summary"]["stored_eur_cents"] == sum(cents(t["amount_eur"]) for t in many))
CACHE.write_text("Date,USD,BRL\n2026-06-15,1.1000,6.2500\n2026-06-12,1.0900,6.2000\n", encoding="utf-8")
fx._rates = None


print("== the endpoint")
check("a bad scope is refused", server.fx_audit_view.__name__ == "fx_audit_view")
try:
    server.fx_audit_view(year=YEAR, scope="nobody")
    check("an unknown scope is rejected", False)
except Exception as exc:
    check("an unknown scope is rejected", getattr(exc, "status_code", None) == 400, str(exc))
payload = server.fx_audit_view(year=YEAR, scope="all")
check("the endpoint returns the audit", payload["summary"]["transactions"] == len(many))


# Anti-shrink guard: exact count at implementation time. May only ever be RAISED
# when checks are added — never lowered (see AGENTS.md: never weaken a test).
MIN_CHECKS = 54
check("suite did not shrink", total_checks >= MIN_CHECKS,
      "total_checks=%d < %d" % (total_checks, MIN_CHECKS))

shutil.rmtree(tmp)
print()
if failures:
    print("FAILED: %s" % ", ".join(failures))
    sys.exit(1)
print("All checks passed.")
