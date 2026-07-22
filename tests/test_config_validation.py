"""load_config() validates config.json and fails with clear, actionable errors."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


tmp = Path(tempfile.mkdtemp(prefix="fa-config-test-"))
os.environ["FA_ROOT"] = str(tmp)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.util import ConfigError, load_config  # noqa: E402


config_path = tmp / "config.json"


def write_config(obj):
    config_path.write_text(json.dumps(obj), encoding="utf-8")


def expect_error(label, obj=None):
    if obj is None:
        config_path.unlink(missing_ok=True)
    else:
        write_config(obj)
    try:
        load_config()
    except ConfigError:
        return
    raise AssertionError("expected ConfigError for %s" % label)


# missing file
expect_error("missing config.json")

# not exactly 2 people
expect_error("three people", {"people": ["a", "b", "c"]})
expect_error("one person", {"people": ["a"]})
expect_error("people not a list", {"people": "a"})

# reserved / malformed slugs
expect_error("reserved slug 'couple'", {"people": ["couple", "b"]})
expect_error("uppercase slug", {"people": ["Anna", "b"]})
expect_error("duplicate slugs", {"people": ["a", "a"]})

# currencies must include EUR when present
expect_error("currencies without EUR", {"people": ["a", "b"], "currencies": ["USD"]})

# person_labels keys must be a subset of people; values non-empty
expect_error("label unknown slug", {"people": ["a", "b"], "person_labels": {"zoe": "Zoe"}})
expect_error("label empty value", {"people": ["a", "b"], "person_labels": {"a": "  "}})

# household_name, when present, must be a non-empty string
expect_error("blank household_name", {"people": ["a", "b"], "household_name": "  "})

# person_styles: keys ⊆ people, colour must be #rrggbb, icon non-empty
expect_error("person_styles unknown slug", {"people": ["a", "b"], "person_styles": {"z": {"color": "#000000"}}})
expect_error("person_styles bad colour", {"people": ["a", "b"], "person_styles": {"a": {"color": "red"}}})
expect_error("person_styles blank icon", {"people": ["a", "b"], "person_styles": {"a": {"icon": "  "}}})

# shared_style: colour must be #rrggbb, icon non-empty
expect_error("shared_style bad colour", {"people": ["a", "b"], "shared_style": {"color": "x"}})
expect_error("shared_style blank icon", {"people": ["a", "b"], "shared_style": {"icon": ""}})

# language: when present must be a non-empty string (empty/blank/non-string rejected)
expect_error("language blank", {"people": ["a", "b"], "language": "  "})
expect_error("language non-string", {"people": ["a", "b"], "language": 5})
# a valid language code loads and round-trips
write_config({"people": ["anna", "ben"], "language": "de"})
assert load_config()["language"] == "de", "valid language code must be preserved"

# valid config returns the parsed dict (incl. optional labels + household_name)
write_config({"people": ["anna", "ben"], "currencies": ["EUR", "USD"],
              "person_labels": {"anna": "Anna"}, "household_name": "Our Household"})
cfg = load_config()
assert isinstance(cfg, dict), "load_config must return a dict"
assert cfg["people"] == ["anna", "ben"], cfg
assert cfg["household_name"] == "Our Household", cfg

# backward compatibility: an ABSENT currencies key defaults to the legacy [EUR, BRL, USD]
# (configs created before the key existed must not silently lose BRL/USD on restart)
write_config({"people": ["anna", "ben"]})
cfg = load_config()
assert cfg["people"] == ["anna", "ben"]
assert cfg["currencies"] == ["EUR", "BRL", "USD"], cfg["currencies"]

# an explicitly PRESENT currencies key means exactly what it says (no legacy injection)
write_config({"people": ["anna", "ben"], "currencies": ["EUR"]})
assert load_config()["currencies"] == ["EUR"], "explicit currencies must be preserved verbatim"

shutil.rmtree(tmp)
print("Config validation passed: missing/invalid configs raise ConfigError, valid config loads")
