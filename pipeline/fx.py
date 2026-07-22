"""ECB reference rates. Downloads eurofxref-hist once, caches in data/fx/.

Rates are EUR-based: 1 EUR = rate x CUR. Conversion: eur = original / rate.
Weekends/holidays fall back to the previous published business day (max 10 days).
"""
import csv
import io
import urllib.request
import zipfile
from datetime import date, datetime, timedelta

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


def _load():
    global _rates
    if _rates is not None:
        return _rates
    if not CACHE.exists():
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


def rate(currency, day):
    """Rate for currency on ISO date `day`, falling back to previous business days."""
    currency = currency.upper()
    if currency == "EUR":
        return 1.0
    rates = _load()
    d = datetime.strptime(day, "%Y-%m-%d").date()
    newest = max(rates)
    if day > newest:
        _refresh_if_stale(d)
        rates = _rates
        newest = max(rates)
    for _ in range(10):
        r = rates.get(d.isoformat())
        if r and currency in r:
            return r[currency]
        d -= timedelta(days=1)
    raise LookupError("No ECB rate for %s near %s (cache newest: %s). Run 'fx-update' online." % (currency, day, newest))


def _refresh_if_stale(needed_day):
    global _rates
    if needed_day <= date.today():
        _download()
        _rates = None
        _load()


def to_eur(amount, currency, day):
    r = rate(currency, day)
    return round(amount / r, 2), r
