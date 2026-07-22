"""First-run setup wizard: unconfigured meta gates to the wizard, POST /api/setup writes a
valid config + seeds from the bundled templates, and a second setup is rejected."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from fastapi import HTTPException


tmp = Path(tempfile.mkdtemp(prefix="fa-setup-test-"))
os.environ["FA_ROOT"] = str(tmp)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import server  # noqa: E402


def expect_http(status, label, fn):
    try:
        fn()
    except HTTPException as exc:
        assert exc.status_code == status, "%s: expected %d, got %d" % (label, status, exc.status_code)
        return
    raise AssertionError("%s: expected HTTP %d, nothing raised" % (label, status))


# unconfigured: meta gates to the wizard, nothing else leaks
assert server.is_configured() is False
assert server.meta() == {"setup_required": True}, server.meta()

# validation rejects and writes nothing
expect_http(400, "same name", lambda: server.setup(server.SetupRequest(person1="Anna", person2="anna")))
expect_http(400, "reserved slug", lambda: server.setup(server.SetupRequest(person1="couple", person2="Ben")))
expect_http(400, "ratio out of range", lambda: server.setup(server.SetupRequest(person1="Anna", person2="Ben", ratio_person1=0)))
assert not (tmp / "config.json").exists(), "failed setup must not write config.json"

# valid setup writes config (people slugs + labels + ratio + currencies) and seed rules
result = server.setup(server.SetupRequest(person1="Anna", person2="Ben", ratio_person1=60, currencies=["EUR", "usd"]))
assert result == {"ok": True}, result

config = json.loads((tmp / "config.json").read_text())
assert config["people"] == ["anna", "ben"], config
assert config["person_labels"] == {"anna": "Anna", "ben": "Ben"}, config
assert config["reference_ratio"] == {"anna": 0.6, "ben": 0.4}, config
assert config["currencies"] == ["EUR", "USD"], config  # deduped, upper-cased, EUR always present

for seed in ("categories.json", "merchant-rules.json", "tax-buckets.json", "recurring-overrides.json"):
    assert (tmp / "rules" / seed).exists(), "missing seed %s" % seed

accounts = json.loads((tmp / "data" / "accounts.json").read_text())
assert accounts["accounts"] == [], accounts
assert accounts["owner_names"] == ["Anna", "Ben"], accounts

# now configured: second setup is rejected, meta returns the full payload
assert server.is_configured() is True
expect_http(409, "double setup", lambda: server.setup(server.SetupRequest(person1="x", person2="y")))

meta = server.meta()
assert "setup_required" not in meta, meta
assert meta["people"] == ["anna", "ben"], meta
assert meta["currencies"] == ["EUR", "USD"], meta

shutil.rmtree(tmp)
print("Setup wizard passed: unconfigured gate, seed install, validation, 409 on re-setup, full meta after")
