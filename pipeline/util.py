"""Shared helpers: paths, JSON IO, money parsing."""
import json
import hashlib
import os
import re
from pathlib import Path

# FA_ROOT lets tests run against an isolated copy; must be set before first import.
ROOT = Path(os.environ.get("FA_ROOT") or Path(__file__).resolve().parent.parent)
DATA = ROOT / "data"
RULES = ROOT / "rules"
INBOX = ROOT / "inbox"

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
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    tmp.replace(p)


def cents(amount):
    """Money comparison must never use float equality."""
    return int(round(float(amount) * 100))


def parse_amount(raw, decimal="comma", sign=1):
    """Parse a bank amount string. decimal='comma' means German 1.234,56."""
    s = CURRENCY_JUNK.sub("", str(raw)).strip()
    if not s:
        return None
    if decimal == "comma":
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return round(float(s) * sign, 2)
    except ValueError:
        return None


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
            if "icon" in st and (not isinstance(st["icon"], str) or not st["icon"].strip()):
                raise ConfigError("config.json 'person_styles[%r].icon' must be a non-empty string" % (slug,))
    shared = cfg.get("shared_style")
    if shared is not None:
        if not isinstance(shared, dict):
            raise ConfigError("config.json 'shared_style' must be an object")
        if "color" in shared and not (isinstance(shared["color"], str) and re.match(r"^#[0-9a-fA-F]{6}$", shared["color"])):
            raise ConfigError("config.json 'shared_style.color' must be a #rrggbb hex")
        if "icon" in shared and (not isinstance(shared["icon"], str) or not shared["icon"].strip()):
            raise ConfigError("config.json 'shared_style.icon' must be a non-empty string")
    return cfg


def load_accounts():
    data = read_json(DATA / "accounts.json")
    return {a["id"]: a for a in data["accounts"]}, data


def year_dir(year):
    d = DATA / str(year)
    (d / "transactions").mkdir(parents=True, exist_ok=True)
    return d
