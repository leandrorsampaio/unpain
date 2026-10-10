"""The desktop launcher: where the data goes, one launcher per data folder, the port it
picks, its window strings, and its self-test run from source.

The packaged app runs the same self-test on every OS in CI (desktop/build.py). This file
keeps the launcher's own logic honest on every commit, without tkinter or PyInstaller.

Usage: .venv/bin/python tests/test_desktop_launcher.py
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
LAUNCHER = PROJECT / "desktop" / "launcher.py"
sys.path.insert(0, str(PROJECT / "desktop"))
import launcher  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="fa-desktop-test-"))

# ---- the data folder is ~/UnPAIN, visible and outside the app; FA_ROOT still overrides it.
assert launcher.data_root(env={}, home="/home/x") == Path("/home/x/UnPAIN")
assert launcher.data_root(env={"FA_ROOT": "/srv/books"}, home="/home/x") == Path("/srv/books")

# ---- the log goes where each OS keeps app logs, never into the data folder (backups zip that).
assert launcher.log_path("darwin", {}, "/Users/x") == Path("/Users/x/Library/Logs/UnPAIN/desktop.log")
assert launcher.log_path("win32", {"LOCALAPPDATA": "/appdata"}, "/u") == Path("/appdata/UnPAIN/Logs/desktop.log")
assert launcher.log_path("win32", {}, "/u") == Path("/u/AppData/Local/UnPAIN/Logs/desktop.log")
assert launcher.log_path("linux", {}, "/home/x") == Path("/home/x/.local/state/unpain/desktop.log")
assert launcher.log_path("linux", {"XDG_STATE_HOME": "/state"}, "/home/x") == Path("/state/unpain/desktop.log")

# ---- the window follows the UI language, and falls back to English on a missing or broken config.
assert launcher.translator(tmp)("Quit") == "Quit"
(tmp / "config.json").write_text(json.dumps({"language": "de"}), encoding="utf-8")
assert launcher.translator(tmp)("Quit") == "Beenden"
(tmp / "config.json").write_text("{not json", encoding="utf-8")
assert launcher.translator(tmp)("Quit") == "Quit"

# Every string the window shows has a German entry, and the table carries nothing dead —
# the same rule tests/test_i18n.js holds the web UI to.
used = set(re.findall(r'\bt\("([^"]+)"\)', LAUNCHER.read_text(encoding="utf-8")))
assert used, "the pattern no longer finds the window's strings"
assert not used - set(launcher.STRINGS_DE), "missing German: %s" % sorted(used - set(launcher.STRINGS_DE))
assert not set(launcher.STRINGS_DE) - used, "unused German: %s" % sorted(set(launcher.STRINGS_DE) - used)

# ---- one launcher per data folder.
locks = tmp / "locks"
locks.mkdir()
first = launcher.InstanceLock(tmp / "data", locks)
second = launcher.InstanceLock(tmp / "data", locks)
other = launcher.InstanceLock(tmp / "other-data", locks)
first.info_path.write_text(json.dumps({"port": 1, "pid": 1}), encoding="utf-8")
assert first.acquire()
assert not first.info_path.exists(), "a crashed run's port must not survive the next start"
assert not second.acquire(), "a second launcher on the same data folder must be refused"
assert other.acquire(), "a different data folder is a different app"
assert second.read() is None, "nothing is published until the launcher says so"
first.publish("running", 8765)
assert second.read()["state"] == "running" and second.read()["port"] == 8765
first.release()
assert not first.info_path.exists()
assert second.acquire(), "released means free"
second.release()
other.release()

# The lock holds across processes too, which is the case that matters on Windows, where the
# write lock is in-process only. And the OS frees it when the holder dies, however it dies.
holder = subprocess.Popen(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, sys.argv[1]); import launcher; "
     "lock = launcher.InstanceLock(sys.argv[2], sys.argv[3]); "
     "print('held' if lock.acquire() else 'refused', flush=True); sys.stdin.read()",
     str(PROJECT / "desktop"), str(tmp / "data"), str(locks)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
assert holder.stdout.readline().strip() == "held"
assert not launcher.InstanceLock(tmp / "data", locks).acquire(), "another process holds it"
holder.kill()
holder.wait()
after_crash = launcher.InstanceLock(tmp / "data", locks)
deadline = time.monotonic() + 10      # Windows frees a dead process's locks "soon", not at once
while not after_crash.acquire():
    assert time.monotonic() < deadline, "a killed holder must not leave the app 'already running'"
    time.sleep(0.1)
after_crash.release()

# ---- the port: 127.0.0.1 only, a busy preferred port is skipped, and any free port is the last resort.
blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
blocker.bind(("127.0.0.1", 0))
blocker.listen()
busy = blocker.getsockname()[1]
sock = launcher.bind_local_socket([busy])
host, port = sock.getsockname()
assert host == "127.0.0.1" and port != busy, (host, port, busy)
sock.close()
blocker.close()

# Quit and reopen must get the same port back. The server closes the browser's connections,
# so the server side holds them in TIME_WAIT, which on macOS refused a plain rebind for ~30 s:
# the reopened app moved to the next port, a new origin with an empty localStorage.
probe = launcher.bind_local_socket(())
preferred = probe.getsockname()[1]
probe.close()
listener = launcher.bind_local_socket([preferred])
assert listener.getsockname()[1] == preferred
listener.listen()
client = socket.create_connection(("127.0.0.1", preferred))
accepted, _ = listener.accept()
accepted.close()                      # the server closes first, as uvicorn does on quit
client.recv(16)                       # the client sees the close...
client.close()                        # ...and answers it, leaving TIME_WAIT on the server side
listener.close()
again = launcher.bind_local_socket([preferred])
assert again.getsockname()[1] == preferred, "a quick reopen moved to port %d" % again.getsockname()[1]
again.close()

# ---- a second launch: waits while the first is starting, opens the running one, gives up at
# once when the first reports a failure, and never asks a proxy about 127.0.0.1.
import http.server  # noqa: E402
import threading  # noqa: E402


class Meta(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/api/meta" else 404)
        self.end_headers()

    def log_message(self, *args):
        pass


fake = http.server.HTTPServer(("127.0.0.1", 0), Meta)
threading.Thread(target=fake.serve_forever, daemon=True).start()
fake_port = fake.server_address[1]
saved_proxy = {key: os.environ.get(key) for key in ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY")}
os.environ.update(http_proxy="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9")
os.environ.pop("no_proxy", None)
os.environ.pop("NO_PROXY", None)
try:
    assert launcher._answers(fake_port), "the probe went through the (dead) proxy"
finally:
    for key, value in saved_proxy.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

holder_lock = launcher.InstanceLock(tmp / "waiting", locks)
watcher = launcher.InstanceLock(tmp / "waiting", locks)
assert holder_lock.acquire()
holder_lock.publish("starting")
threading.Timer(0.4, holder_lock.publish, args=("running", fake_port)).start()
started_at = time.monotonic()
assert launcher.find_running(watcher, timeout=10) == fake_port, "a slow start must be waited for"
assert time.monotonic() - started_at < 5
holder_lock.publish("failed")
started_at = time.monotonic()
assert launcher.find_running(watcher, timeout=10) is None
assert time.monotonic() - started_at < 2, "a failed start must not keep the second launch waiting"
holder_lock.release()
fake.shutdown()

# ---- the window's fonts: Tk 8.6 on X11 sizes in negative pixels, Tk 9 and macOS/Windows in points.
assert launcher.scaled_font_size(-12, 8) == -20, "a pixel size must grow, not shrink to -4"
assert launcher.scaled_font_size(-12, -1, minimum=9) == -11
assert launcher.scaled_font_size(13, 8) == 21
assert launcher.scaled_font_size(10, -1, minimum=9) == 9
assert launcher.scaled_font_size(0, 8) == 18

# ---- the self-test, from source: a real server on a fresh folder, the setup wizard's seed
# files, the bank formats, and the local-only guard refusing a foreign Host and a cross-site
# write. It must not inherit a test sandbox's FA_ROOT: it makes its own empty folder.
report = tmp / "self-test.json"
env = {key: value for key, value in os.environ.items() if key not in ("FA_ROOT", "UNPAIN_ALLOWED_HOSTS")}
result = subprocess.run([sys.executable, str(LAUNCHER), "--self-test", str(report)],
                        env=env, capture_output=True, text=True, timeout=300)
assert report.exists(), result.stdout + result.stderr
outcome = json.loads(report.read_text(encoding="utf-8"))
failed = [check for check in outcome["checks"] if not check["ok"]]
assert result.returncode == 0 and outcome["ok"] and not failed, failed or result.stderr
names = {check["check"] for check in outcome["checks"]}
for required in ("foreign Host refused", "cross-site write refused", "cross-site read refused",
                 "cross-site GET that writes refused", "a link from elsewhere still opens the page",
                 "the app's own page may write", "the app's own page via localhost may write",
                 "setup wizard (bundled seed files)", "bank formats bundled"):
    assert required in names, "the self-test no longer checks %r" % required

shutil.rmtree(tmp, ignore_errors=True)
print("desktop launcher: all checks passed")
