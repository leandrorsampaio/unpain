"""Optional app lock: password hashing, session/expiry, endpoints and config validation."""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
root = Path(tempfile.mkdtemp(prefix="fa-security-test-"))
os.environ["FA_ROOT"] = str(root)
shutil.copy(PROJECT / "examples" / "config.json", root / "config.json")
(root / "rules").mkdir()
for name in ("categories.json", "merchant-rules.json", "tax-buckets.json"):
    shutil.copy(PROJECT / "examples" / name, root / "rules" / name)
(root / "data").mkdir()
shutil.copy(PROJECT / "examples" / "accounts.json", root / "data" / "accounts.json")

from app import server  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from pipeline.util import ConfigError, load_config  # noqa: E402


class Req:
    """Minimal stand-in for a Starlette Request (only .cookies is read)."""
    def __init__(self, cookies=None):
        self.cookies = cookies or {}


# ---- hashing round-trips and rejects the wrong password / garbage.
stored = server._hash_password("hunter2")
assert stored.startswith("scrypt$")
assert server._verify_password("hunter2", stored)
assert not server._verify_password("wrong", stored)
assert not server._verify_password("x", "not-a-hash")

# ---- lock is OFF by default: _access reports unlocked for any request.
assert server._access(Req()) == (False, True)

# ---- setting a password enables the lock and mints a session cookie.
resp = server.set_password(server.SetPasswordRequest(new_password="secret1"))
token = resp.headers["set-cookie"].split("fa_session=")[1].split(";")[0]
assert (root / "config.json") and load_config()["security"]["password_hash"]
enabled, unlocked = server._access(Req({"fa_session": token}))
assert enabled and unlocked, "the minted session should be unlocked"
assert server._access(Req()) == (True, False), "no cookie → locked"
assert server._access(Req({"fa_session": "bogus"})) == (True, False), "unknown token → locked"

# ---- too-short password is rejected.
try:
    server.set_password(server.SetPasswordRequest(new_password="ab", current_password="secret1"))
    raise AssertionError("short password accepted")
except HTTPException as e:
    assert e.status_code == 400

# ---- changing the password requires the current one.
try:
    server.set_password(server.SetPasswordRequest(new_password="secret2", current_password="WRONG"))
    raise AssertionError("password change without current password")
except HTTPException as e:
    assert e.status_code == 403

# ---- unlock endpoint: wrong password 401s, right one mints a working session.
try:
    server.unlock(server.UnlockRequest(password="nope"))
    raise AssertionError("wrong password unlocked")
except HTTPException as e:
    assert e.status_code == 401
ok = server.unlock(server.UnlockRequest(password="secret1"))
tok2 = ok.headers["set-cookie"].split("fa_session=")[1].split(";")[0]
assert server._access(Req({"fa_session": tok2})) == (True, True)

# ---- auto-lock expiry: with auto_lock on, a stale session becomes invalid.
server.set_auto_lock(server.AutoLockRequest(auto_lock=True, timeout_minutes=1))
sec = load_config()["security"]
assert sec["auto_lock"] and sec["timeout_minutes"] == 1
server._SESSIONS[tok2] = time.time() - 120        # 2 minutes idle > 1 minute timeout
assert not server._session_valid(tok2, sec), "stale session must expire"
# a fresh session stays valid and slides on use
server._SESSIONS[tok2] = time.time()
assert server._session_valid(tok2, sec)

# ---- manual lock drops the session.
server._SESSIONS[tok2] = time.time()
server.lock(Req({"fa_session": tok2}))
assert tok2 not in server._SESSIONS

# ---- removing the password turns the lock off and clears sessions.
try:
    server.remove_password(server.RemovePasswordRequest(current_password="WRONG"))
    raise AssertionError("removed with wrong password")
except HTTPException as e:
    assert e.status_code == 403
server.remove_password(server.RemovePasswordRequest(current_password="secret1"))
assert "security" not in load_config()
assert server._access(Req()) == (False, True)

# ---- config validation rejects malformed security blocks.
import json

BASE = {"people": ["person1", "person2"], "currencies": ["EUR"]}


def expect_bad(security):
    (root / "config.json").write_text(json.dumps({**BASE, "security": security}), encoding="utf-8")
    try:
        load_config()
        raise AssertionError("bad security accepted: %r" % security)
    except ConfigError:
        pass

expect_bad("nope")
expect_bad({"password_hash": ""})
expect_bad({"password_hash": "x", "auto_lock": "yes"})
expect_bad({"password_hash": "x", "timeout_minutes": 0})
expect_bad({"password_hash": "x", "timeout_minutes": 5000})

shutil.rmtree(root)
print("Security passed: hashing, sessions, expiry, unlock/lock/set/remove endpoints, config validation")
