"""The app shell stamps its own asset versions, so a CSS change can never ship behind a
cached stylesheet. Hand-written ?v= strings had exactly that failure: new JS, old CSS, and a
page that looks broken for reasons nothing in the app explains."""
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
root = Path(tempfile.mkdtemp(prefix="fa-assets-test-"))
os.environ["FA_ROOT"] = str(root)
shutil.copy(PROJECT / "examples" / "config.json", root / "config.json")

from app import server  # noqa: E402

html = server._index_html()

# ---- Every versioned asset is stamped, and nothing is left on a hand-written string.
for name in server.VERSIONED_ASSETS:
    found = re.findall(r"/static/%s\?v=([0-9a-f]{10})\b" % re.escape(name), html)
    assert found, "%s is not version-stamped in the shell: %s" % (name, name)
    assert found[0] == server._asset_version(name), name
assert "?v=2026" not in html, "a hand-written version string survived the stamping"

# ---- Editing a file changes its stamp: this is the whole point.
css = server.STATIC / "app.css"
before = server._asset_version("app.css")
original = css.read_bytes()
try:
    time.sleep(0.01)
    css.write_bytes(original + b"\n/* touched by tests */\n")
    after = server._asset_version("app.css")
    assert after != before, "a changed stylesheet must get a new version"
    assert ("app.css?v=%s" % after) in server._index_html()
finally:
    css.write_bytes(original)

# ---- The shell is still served as HTML, and is not cached itself.
response = server.index()
assert response.media_type == "text/html", response.media_type
assert response.headers.get("cache-control") == "no-cache", dict(response.headers)
assert b"<html" in bytes(response.body).lower()

shutil.rmtree(root, ignore_errors=True)
print("Asset versions passed: shell stamps app.css/app.js from the files, changes bust the cache")
