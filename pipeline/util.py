"""Shared helpers: paths, JSON IO, money parsing."""
import json
import hashlib
import math
import os
import re
import tempfile
from pathlib import Path

# FA_ROOT lets tests run against an isolated copy; must be set before first import.
ROOT = Path(os.environ.get("FA_ROOT") or Path(__file__).resolve().parent.parent)
DATA = ROOT / "data"
RULES = ROOT / "rules"
INBOX = ROOT / "inbox"

# Transactions are filed under data/<year>, and store.years() discovers those directories
# by a four-digit name. A date that parses to year 1 writes data/1, which is then invisible
# to every summary, every dashboard and the doctor — the import reports success and the
# money is gone. No bank statement dates anything outside this range, so a date that does
# is a mis-parse, and the file is refused rather than half-imported.
MIN_YEAR, MAX_YEAR = 1900, 2999

# An icon field holds a Material Symbol name and nothing else. The UI renders it between
# <md-icon> tags, so any other character there is markup arriving from somewhere a person
# can type — a settings field, an imported config, a restored backup. The UI validates it
# again on the way out; this stops it being stored in the first place.
ICON_NAME = re.compile(r"^[a-z0-9_]{1,40}$")

CURRENCY_JUNK = re.compile(r"[€$\s ]|R\$|EUR|BRL|USD")


def read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        if default is not None:
            return default
        raise FileNotFoundError(p)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    """Publish a JSON document atomically, and never publish a non-finite number.

    A fixed '<name>.tmp' is one shared scratch file: two writers saving the same
    document at the same moment interleave their bytes into it, and whichever
    replace lands last publishes the mixture as if it were a whole file. A unique
    temporary name in the same directory keeps each writer's bytes its own, and the
    fsync means the published file is on the disk rather than only in the page
    cache. allow_nan=False refuses NaN/Infinity outright — Python would happily
    write them, no JSON reader has to accept them back, and a stored NaN silently
    poisons every total computed from that file.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, p)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def cents(amount):
    """Money comparison must never use float equality."""
    value = float(amount)
    if not math.isfinite(value):
        raise ValueError("amount %r is not a finite number" % (amount,))
    return int(round(value * 100))


def parse_amount(raw, decimal="comma", sign=1):
    """Parse a bank amount string. decimal='comma' means German 1.234,56.

    Returns None when the text is not money. 'NaN' and 'Infinity' are valid input
    to float() and would otherwise travel all the way into the store, where every
    sum they touch becomes NaN and no comparison against them is ever true.
    """
    s = CURRENCY_JUNK.sub("", str(raw)).strip()
    if not s:
        return None
    if decimal == "comma":
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        value = float(s) * sign
    except (ValueError, OverflowError):
        return None
    return round(value, 2) if math.isfinite(value) else None


def txn_hash(account, date, amount, currency, counterparty, purpose):
    key = "|".join([account, date, "%.2f" % amount, currency, counterparty or "", purpose or ""])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Configs created before the "currencies" key existed historically offered EUR/BRL/USD in the
# cash/edit forms. When the key is ABSENT, keep that behavior so a restart never silently drops
# BRL/USD. An explicitly present key means exactly what it says. This is the single source of
# the fallback: load_config() writes it into the returned dict so every consumer reads a
# populated "currencies" key.
LEGACY_CURRENCIES = ["EUR", "BRL", "USD"]


class ConfigError(ValueError):
    """config.json is missing or invalid. The message tells the user how to fix it."""


def load_config():
    path = ROOT / "config.json"
    if not path.exists():
        raise ConfigError(
            "config.json not found at %s. Start the web app and complete the setup wizard, "
            "or copy examples/config.json and edit it." % path)
    cfg = read_json(path)
    people = cfg.get("people")
    if not isinstance(people, list) or len(people) != 2:
        raise ConfigError("config.json 'people' must list exactly 2 person slugs, e.g. [\"anna\", \"ben\"] (got: %r)" % (people,))
    for p in people:
        if not isinstance(p, str) or not _SLUG.match(p) or p == "couple":
            raise ConfigError("config.json person slug %r is invalid: lowercase letters/digits/hyphens only, and 'couple' is reserved" % (p,))
    if len(set(people)) != 2:
        raise ConfigError("config.json 'people' must be two different slugs")
    if "currencies" not in cfg:
        cfg["currencies"] = list(LEGACY_CURRENCIES)  # backward compat for pre-key configs
    currencies = cfg["currencies"]
    if not isinstance(currencies, list) or "EUR" not in [str(c).upper() for c in currencies]:
        raise ConfigError("config.json 'currencies' must be a list that includes EUR (the base currency)")
    ratio = cfg.get("reference_ratio")
    if ratio is not None:
        # The settlement falls back to this whenever there is no salary to derive a ratio
        # from, and it then decides how every shared cost is divided. The Settings form
        # checks it; a hand-edited or restored config never passed through that form, and
        # a share that is not a fraction produces a fair share larger than the whole cost.
        if not isinstance(ratio, dict) or set(ratio) != set(people):
            raise ConfigError("config.json 'reference_ratio' must have exactly the keys %r" % (people,))
        values = []
        for slug, share in ratio.items():
            if isinstance(share, bool) or not isinstance(share, (int, float)) \
                    or not math.isfinite(share) or not 0 <= share <= 1:
                raise ConfigError("config.json 'reference_ratio[%r]' must be a fraction between 0 and 1 (got %r)"
                                  % (slug, share))
            values.append(float(share))
        if int(round(sum(values) * 100)) != 100:
            raise ConfigError("config.json 'reference_ratio' must sum to 1 (got %r)" % (sum(values),))
    labels = cfg.get("person_labels")
    if labels is not None:
        if not isinstance(labels, dict) or not set(labels) <= set(people):
            raise ConfigError("config.json 'person_labels' keys must be a subset of people %r" % (people,))
        for slug, name in labels.items():
            if not isinstance(name, str) or not name.strip():
                raise ConfigError("config.json 'person_labels[%r]' must be a non-empty display name" % (slug,))
    household = cfg.get("household_name")
    if household is not None and (not isinstance(household, str) or not household.strip()):
        raise ConfigError("config.json 'household_name' must be a non-empty string when set")
    language = cfg.get("language")
    if language is not None and (not isinstance(language, str) or not language.strip()):
        raise ConfigError("config.json 'language' must be a non-empty language code (e.g. 'en', 'de') when set")
    security = cfg.get("security")
    if security is not None:
        if not isinstance(security, dict):
            raise ConfigError("config.json 'security' must be an object")
        ph = security.get("password_hash")
        if ph is not None and (not isinstance(ph, str) or not ph):
            raise ConfigError("config.json 'security.password_hash' must be a non-empty string when set")
        if "auto_lock" in security and not isinstance(security["auto_lock"], bool):
            raise ConfigError("config.json 'security.auto_lock' must be a boolean")
        tm = security.get("timeout_minutes")
        if tm is not None and (not isinstance(tm, int) or isinstance(tm, bool) or not 1 <= tm <= 1440):
            raise ConfigError("config.json 'security.timeout_minutes' must be an integer between 1 and 1440")
    styles = cfg.get("person_styles")
    if styles is not None:
        if not isinstance(styles, dict) or not set(styles) <= set(people):
            raise ConfigError("config.json 'person_styles' keys must be a subset of people %r" % (people,))
        for slug, st in styles.items():
            if not isinstance(st, dict):
                raise ConfigError("config.json 'person_styles[%r]' must be an object" % (slug,))
            if "color" in st and not (isinstance(st["color"], str) and re.match(r"^#[0-9a-fA-F]{6}$", st["color"])):
                raise ConfigError("config.json 'person_styles[%r].color' must be a #rrggbb hex" % (slug,))
            if "icon" in st and not ICON_NAME.match(str(st.get("icon") or "")):
                raise ConfigError("config.json 'person_styles[%r].icon' must be a Material Symbol name (lowercase letters, digits, underscores)" % (slug,))
    shared = cfg.get("shared_style")
    if shared is not None:
        if not isinstance(shared, dict):
            raise ConfigError("config.json 'shared_style' must be an object")
        if "color" in shared and not (isinstance(shared["color"], str) and re.match(r"^#[0-9a-fA-F]{6}$", shared["color"])):
            raise ConfigError("config.json 'shared_style.color' must be a #rrggbb hex")
        if "icon" in shared and not ICON_NAME.match(str(shared.get("icon") or "")):
            raise ConfigError("config.json 'shared_style.icon' must be a Material Symbol name (lowercase letters, digits, underscores)")
    return cfg


def load_accounts():
    data = read_json(DATA / "accounts.json")
    return {a["id"]: a for a in data["accounts"]}, data


def year_dir(year):
    d = DATA / str(year)
    (d / "transactions").mkdir(parents=True, exist_ok=True)
    return d
