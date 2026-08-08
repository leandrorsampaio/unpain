"""The only place money becomes a number.

Every monetary amount is an integer of minor units — cents — for as long as it is being
calculated. Floats appear at two boundaries and nowhere else: reading a bank file, and
writing an API response. In between, nothing rounds twice and nothing accumulates the
error that makes two screens disagree about the same euro.

This is not theoretical tidiness. Settlement already lost a cent this way: money was
summed as floats and each person's share rounded on its own, so a shared cent paid from
the joint account became two cents of "paid". The fix was integer cents. This module
exists so the next module to multiply, allocate or convert cannot rediscover the same
bug in its own corner.

What is *not* money: ratios, percentages and exchange rates. A ratio is a proportion —
rounding it to cents is a category error, and doing so is exactly how a three-cent joint
salary produced a 57/43 income split. Those use `Decimal` or exact integer arithmetic.

Rounding policy: **half to even**, at the input boundary only. That is what
`round(x * 100)` has always done here — Python's built-in `round` is banker's rounding —
and it is preserved deliberately rather than quietly improved to the more natural-looking
half-up, which would move every historical half-cent case (plan Decision A). Writing this
module against half-up first and diffing it against the old implementation is how the
actual policy was found out.

The before/after report that decision needs, measured rather than assumed:

    2-decimal amounts (what the store holds)   200,000 random + 2,822 real:  0 differ
    3-decimal amounts (never stored)                     50,000 random:    347 differ

So nothing in the ledger moves. The remaining difference is confined to inputs with more
precision than any stored amount, where the old path multiplied a float by 100 first and
the answer therefore depended on binary representation rather than on any policy — 8257.175
became 825717 because the float sits a hair below the decimal. Going through Decimal makes
those deterministic. That is a strict improvement in a place the ledger does not reach.
"""
import math
from decimal import Decimal, ROUND_HALF_EVEN
from fractions import Fraction

# The rounding the whole application uses, in one place, with a name.
ROUNDING = ROUND_HALF_EVEN
ROUNDING_POLICY_VERSION = 1

# Every currency the app supports has two minor digits. That is a fact about EUR/BRL/USD
# and not about currencies in general (JPY has none, KWD has three), so the scale is
# named rather than assumed by scattering `* 100` through the code.
DEFAULT_MINOR_UNITS = 2
MINOR_UNITS = {"EUR": 2, "BRL": 2, "USD": 2, "GBP": 2, "CHF": 2}


def minor_units(currency):
    return MINOR_UNITS.get((currency or "EUR").upper(), DEFAULT_MINOR_UNITS)


def to_cents(value, *, field="amount", currency="EUR"):
    """A number of minor units, or a clear refusal.

    Booleans are rejected outright: `isinstance(True, int)` is True in Python, so a bool
    passes every naive numeric check and then behaves as 1. A `True` where an amount
    belongs is corrupt data, not a one-euro transaction.
    """
    if isinstance(value, bool):
        raise ValueError("%s must be a number, not a boolean" % field)
    if isinstance(value, int):
        return value * 10 ** minor_units(currency)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("%s must be a finite amount, got %s" % (field, value))
        return int(_quantize(value, currency))
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("%s must be a finite amount, got %r" % (field, value))
        # Decimal(str(x)) reads the number a human wrote rather than the binary
        # approximation stored for it: str(1.005) is '1.005', while Decimal(1.005) is
        # 1.00499999999999989341858963598497211933135986328125 and rounds the wrong way.
        return int(_quantize(Decimal(str(value)), currency))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("%s must not be empty" % field)
        try:
            return int(_quantize(Decimal(text), currency))
        except Exception:
            raise ValueError("%s is not a number: %r" % (field, value[:40]))
    raise ValueError("%s must be a number, got %s" % (field, type(value).__name__))


def _quantize(value, currency):
    scale = Decimal(10) ** minor_units(currency)
    return (value * scale).quantize(Decimal(1), rounding=ROUNDING)


def from_cents(value, *, currency="EUR"):
    """Back to a float, for an API response or a spreadsheet cell. Output only."""
    return int(value) / float(10 ** minor_units(currency))


def decimal_from_cents(value, *, currency="EUR"):
    """Back to an exact decimal, for anything that must not touch a float."""
    return Decimal(int(value)) / (Decimal(10) ** minor_units(currency))


def sum_cents(values, *, field="amount", currency="EUR"):
    """Convert first, then add. Never add floats and convert the total.

    Summing floats and rounding once is *usually* right and occasionally not: the error
    accumulates with the number of rows, and a year of transactions is thousands of them.
    Converting each row first means the sum is exact by construction.
    """
    return sum(to_cents(value, field=field, currency=currency) for value in values)


def allocate_cents(total, weights):
    """Split an integer amount by weight so the parts add back to the whole.

    Rounding each share on its own creates and destroys money: at 50/50 a single cent
    becomes two (0.005 rounds up twice), and any ratio that is not a clean fraction
    leaves a cent stranded. Largest remainder hands out the floor of every share, then
    gives the leftover cents one each to the largest fractional parts — so the parts sum
    to `total` exactly, for positive totals, negative totals and ties alike.

    Ties break on position, so the same input always produces the same split.
    """
    weights = [Fraction(str(w)) if not isinstance(w, (int, Fraction)) else Fraction(w)
               for w in weights]
    if not weights:
        return []
    denominator = sum(weights)
    if denominator <= 0:
        return [0] * len(weights)
    exact = [Fraction(int(total)) * weight / denominator for weight in weights]
    parts = [math.floor(value) for value in exact]
    # floor never overshoots, so the leftover is between 0 and len-1 whatever the sign
    # of `total`: a refund larger than the spend allocates the same way.
    leftover = int(total) - sum(parts)
    order = sorted(range(len(weights)), key=lambda i: (-(exact[i] - parts[i]), i))
    for index in order[:leftover]:
        parts[index] += 1
    return parts


def convert_minor_units(amount_cents, rate, *, currency="EUR"):
    """Convert an amount at an exchange rate, rounding once, at the end.

    `rate` is 1 EUR = rate x CUR, so the conversion divides. It is a rate and not money:
    passed through Decimal so the division is exact until the single rounding.
    """
    if rate is None:
        raise ValueError("an exchange rate is required to convert")
    exact = Decimal(int(amount_cents)) / Decimal(str(rate))
    return int(exact.quantize(Decimal(1), rounding=ROUNDING))


def percentage(numerator, denominator):
    """A proportion, exactly — not money, and never rounded to cents."""
    if not denominator:
        return None
    return Decimal(int(numerator)) / Decimal(int(denominator))
