"""ECB reference rates. Downloads eurofxref-hist once, caches in data/fx/.

Rates are EUR-based: 1 EUR = rate x CUR. Conversion: eur = original / rate.
Weekends/holidays fall back to the previous published business day (max 10 days).
"""
import csv
import io
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone

from . import money
from .util import DATA

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
CACHE = DATA / "fx" / "eurofxref-hist.csv"

_rates = None  # {date_str: {cur: float}}


def _download():
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(ECB_URL, timeout=60) as resp:
        blob = resp.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = [n for n in z.namelist() if n.endswith(".csv")][0]
        CACHE.write_bytes(z.read(name))


def _load(allow_download=True):
    global _rates
    if _rates is not None:
        return _rates
    if not CACHE.exists():
        if not allow_download:
            return {}      # read-only callers get "nothing cached", never a download
        _download()
    _rates = {}
    with open(CACHE, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            day = row.get("Date")
            if not day:
                continue
            _rates[day] = {
                k.strip(): float(v)
                for k, v in row.items()
                if k and k.strip() not in ("Date", "") and v and v.strip() not in ("N/A", "")
            }
    return _rates


FALLBACK_DAYS = 10   # how far back the walk looks for a published rate


def _walk_back(rates, currency, day):
    """The published rate actually used for `day`, and the date it was published on.

    ECB publishes on business days, so a Saturday booking is converted at Friday's
    rate. Which date that turned out to be is the fact an audit needs and the one
    nothing used to record — `rate()` returned only the number. One walk, used by
    both, so the audit can never describe a different fallback than the conversion.

    Returns (rate, publication_date_iso, fallback_days) or None.
    """
    cursor = datetime.strptime(day, "%Y-%m-%d").date()
    for offset in range(FALLBACK_DAYS):
        published = rates.get(cursor.isoformat())
        if published and currency in published:
            return published[currency], cursor.isoformat(), offset
        cursor -= timedelta(days=1)
    return None


def rate_details(currency, requested_date, *, allow_download=False):
    """Where a rate came from, not just what it was.

    `allow_download=False` (the default) never touches the network and never writes
    the cache — an audit that could rewrite the thing it is auditing is not an audit.
    Raises LookupError with the same message `rate()` does when nothing is found.
    """
    currency = (currency or "").upper()
    if currency == "EUR":
        return {"currency": "EUR", "requested_date": requested_date,
                "rate_date": requested_date, "rate": 1.0, "fallback_days": 0}
    rates = _load(allow_download=allow_download)
    newest = max(rates) if rates else None
    if allow_download and newest is not None and requested_date > newest:
        _refresh_if_stale(datetime.strptime(requested_date, "%Y-%m-%d").date())
        rates = _rates or {}
        newest = max(rates) if rates else None
    found = _walk_back(rates, currency, requested_date)
    if found is None:
        raise LookupError("No ECB rate for %s near %s (cache newest: %s). Run 'fx-update' online."
                          % (currency, requested_date, newest))
    value, rate_date, fallback = found
    return {"currency": currency, "requested_date": requested_date, "rate_date": rate_date,
            "rate": value, "fallback_days": fallback}


def rate(currency, day):
    """Rate for currency on ISO date `day`, falling back to previous business days."""
    return rate_details(currency, day, allow_download=True)["rate"]


def _refresh_if_stale(needed_day):
    global _rates
    if needed_day <= date.today():
        _download()
        _rates = None
        _load()


def cache_info():
    """What the local ECB cache holds, without downloading anything.

    A stale cache is a reason to run `fx-update` before importing *new* foreign rows.
    It is not evidence that rows converted months ago are wrong, so this reports the
    facts and leaves that judgement to the caller.
    """
    if not CACHE.exists():
        return {"present": False, "newest_rate_date": None, "modified_at": None, "days": 0}
    stat = CACHE.stat()
    rates = _load(allow_download=False)
    return {
        "present": True,
        "newest_rate_date": max(rates) if rates else None,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        .replace(microsecond=0).isoformat(),
        "days": len(rates),
    }


def to_eur(amount, currency, day):
    """Convert to EUR. Returns (eur, rate) — see to_eur_details for the rate's date."""
    details = to_eur_details(amount, currency, day)
    return details["eur"], details["rate"]


def to_eur_details(amount, currency, day, *, allow_download=True):
    """Convert, and say which published rate did it.

    The one conversion helper every ingestion path uses, so the three of them cannot
    drift on which rate date they record."""
    details = rate_details(currency, day, allow_download=allow_download)
    # Through the versioned policy, not `round(amount / rate, 2)`. A float division
    # rounded afterwards disagrees with `money.convert_minor_units` by a cent on values
    # that land on a boundary (-999.87 / 6.0 gives -166.65 one way and -166.64 the
    # other), and the whole point of naming a rounding policy is that one module cannot
    # quietly use a different one.
    eur_cents = money.convert_minor_units(money.to_cents(amount, currency=currency),
                                          details["rate"])
    return dict(details, amount_original=amount, eur=eur_cents / 100.0)
