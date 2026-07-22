"""Settings endpoints edit config.json knobs while keeping person slugs immutable and
preserving hand-managed keys (_help, base_currency)."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from fastapi import HTTPException


tmp = Path(tempfile.mkdtemp(prefix="fa-settings-test-"))
os.environ["FA_ROOT"] = str(tmp)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import server  # noqa: E402


(tmp / "data").mkdir(parents=True, exist_ok=True)
(tmp / "config.json").write_text(json.dumps({
    "_help": "keep me",
    "people": ["anna", "ben"],
    "person_labels": {"anna": "Anna", "ben": "Ben"},
    "reference_ratio": {"anna": 0.5, "ben": 0.5},
    "items_threshold_eur": 50,
    "transfer_match_window_days": 4,
    "transfer_match_tolerance_cents": 200,
    "base_currency": "EUR",
    "currencies": ["EUR", "USD"],
}), encoding="utf-8")
(tmp / "data" / "accounts.json").write_text(json.dumps({"accounts": []}), encoding="utf-8")
# /api/meta reads the rule seeds; copy the bundled templates so meta() works in this root
examples = Path(__file__).resolve().parent.parent / "examples"
(tmp / "rules").mkdir(parents=True, exist_ok=True)
for seed in ("categories.json", "tax-buckets.json"):
    shutil.copy(examples / seed, tmp / "rules" / seed)


def expect_http(status, label, fn):
    try:
        fn()
    except HTTPException as exc:
        assert exc.status_code == status, "%s: expected %d, got %d" % (label, status, exc.status_code)
        return
    raise AssertionError("%s: expected HTTP %d, nothing raised" % (label, status))


def payload(**over):
    base = {
        "person_labels": {"anna": "Anna", "ben": "Ben"},
        "reference_ratio": {"anna": 0.5, "ben": 0.5},
        "items_threshold_eur": 50,
        "transfer_match_window_days": 4,
        "transfer_match_tolerance_cents": 200,
        "currencies": ["EUR", "USD"],
    }
    base.update(over)
    return server.SettingsUpdate(**base)


# GET returns the current config
assert server.settings_get()["people"] == ["anna", "ben"]

# valid update persists, upper-cases/dedupes currencies, preserves untouched keys
server.settings_update(payload(
    person_labels={"anna": "Anna-Lena", "ben": "Ben"},
    reference_ratio={"anna": 0.55, "ben": 0.45},
    items_threshold_eur=75,
    currencies=["usd", "chf", "EUR"],
))
cfg = json.loads((tmp / "config.json").read_text())
assert cfg["person_labels"]["anna"] == "Anna-Lena", cfg
assert cfg["reference_ratio"] == {"anna": 0.55, "ben": 0.45}, cfg
assert cfg["items_threshold_eur"] == 75, cfg
assert cfg["currencies"] == ["CHF", "EUR", "USD"], cfg
assert cfg["_help"] == "keep me" and cfg["base_currency"] == "EUR", "manual keys must survive"

# people are immutable: an extra 'people' key on the request is ignored by the model
server.settings_update(server.SettingsUpdate(**{
    "people": ["x", "y"],
    "person_labels": {"anna": "Anna", "ben": "Ben"},
    "reference_ratio": {"anna": 0.5, "ben": 0.5},
    "items_threshold_eur": 50,
    "transfer_match_window_days": 4,
    "transfer_match_tolerance_cents": 200,
    "currencies": ["EUR"],
}))
assert json.loads((tmp / "config.json").read_text())["people"] == ["anna", "ben"], "people must be immutable"

# invalid updates are rejected
expect_http(400, "wrong ratio keys", lambda: server.settings_update(payload(reference_ratio={"anna": 1.0})))
expect_http(400, "ratio sum != 100", lambda: server.settings_update(payload(reference_ratio={"anna": 0.6, "ben": 0.6})))
expect_http(400, "empty label", lambda: server.settings_update(payload(person_labels={"anna": "  ", "ben": "Ben"})))
expect_http(400, "label unknown slug", lambda: server.settings_update(payload(person_labels={"zoe": "Zoe"})))
expect_http(400, "items threshold <= 0", lambda: server.settings_update(payload(items_threshold_eur=0)))
expect_http(400, "negative transfer window", lambda: server.settings_update(payload(transfer_match_window_days=-1)))

# household_name: setting it stores stripped value and /api/meta reflects it
server.settings_update(payload(household_name="  Test Home  "))
assert json.loads((tmp / "config.json").read_text())["household_name"] == "Test Home"
assert server.meta()["household_name"] == "Test Home", server.meta()

# blank clears the key; /api/meta falls back to the default
server.settings_update(payload(household_name="   "))
assert "household_name" not in json.loads((tmp / "config.json").read_text()), "blank must clear the key"
assert server.meta()["household_name"] == "Family Accountability", server.meta()

# omitting the field (model default "") also clears it, and load_config never sees a blank value
server.settings_update(payload(household_name="Home"))
assert server.meta()["household_name"] == "Home"
server.settings_update(payload())
assert "household_name" not in json.loads((tmp / "config.json").read_text())

# person_styles: custom partner colour + icon persist and /api/meta reflects them
server.settings_update(payload(person_styles={"anna": {"color": "#123456", "icon": "face"}}))
assert json.loads((tmp / "config.json").read_text())["person_styles"]["anna"] == {"color": "#123456", "icon": "face"}
assert server.meta()["person_styles"]["anna"]["color"] == "#123456"
expect_http(400, "bad person_styles colour", lambda: server.settings_update(payload(person_styles={"anna": {"color": "red"}})))
expect_http(400, "person_styles unknown slug", lambda: server.settings_update(payload(person_styles={"zoe": {"color": "#000000"}})))
# empty styles clears the key
server.settings_update(payload(person_styles={}))
assert "person_styles" not in json.loads((tmp / "config.json").read_text())

# shared_style: custom colour + icon for the shared/together option
server.settings_update(payload(shared_style={"color": "#abcdef", "icon": "diversity_1"}))
assert json.loads((tmp / "config.json").read_text())["shared_style"] == {"color": "#abcdef", "icon": "diversity_1"}
assert server.meta()["shared_style"]["icon"] == "diversity_1"
expect_http(400, "bad shared_style colour", lambda: server.settings_update(payload(shared_style={"color": "nope"})))
server.settings_update(payload(shared_style={}))
assert "shared_style" not in json.loads((tmp / "config.json").read_text())

shutil.rmtree(tmp)
print("Settings passed: GET, valid persist + preserved keys, immutable people, household name set/clear, invalid rejected")
