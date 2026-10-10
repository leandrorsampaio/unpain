"""UnPAIN desktop launcher: the double-click way to run the app.

It runs the same FastAPI server that start.sh runs, with the differences that matter to
somebody who never opens a terminal:

- The data lives in a folder the person can see, ``~/UnPAIN``, never inside the app. The
  app can be replaced, updated or deleted without touching a transaction. It is the home
  folder rather than Documents on purpose: macOS (iCloud "Desktop & Documents") and Windows
  (OneDrive folder backup) both offer to sync Documents to a cloud during setup, and a
  ledger that promises to stay local must not leave because of a checkbox. ``FA_ROOT``
  still overrides it.
- The server binds 127.0.0.1 only, and answers only to its own host names
  (``UNPAIN_ALLOWED_HOSTS``; see ``local_only`` in app/server.py).
- One small window says the app is running and stops it. Closing the window quits.
- One launcher per data folder. Opening the app again opens the browser on the running
  one; two servers on one store would be two writers, and Windows has no cross-process
  write lock (pipeline/mutation_lock.py).

Run from source:  .venv/bin/python desktop/launcher.py          (needs tkinter)
Build:            python desktop/build.py
Smoke test:       <app> --self-test report.json                 (no window; CI runs it on every OS)
"""
import contextlib
import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "UnPAIN"
FROZEN = getattr(sys, "frozen", False)
# Code and bundled files: the PyInstaller bundle, or the repository when run from source.
CODE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
if not FROZEN and str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

# A fixed first choice keeps the browser origin stable, and with it what the UI keeps in
# localStorage (theme, language, selected year). The rest is for when start.sh or some
# other program already holds it.
PORTS = range(8765, 8785)
START_TIMEOUT = 120       # seconds; a first start on a slow disk imports a lot
STOP_TIMEOUT = 15         # longer than uvicorn's own graceful shutdown below
WINDOWS_MUTEX = "UnPAINDesktop"   # desktop/windows/unpain.iss names it as AppMutex

log = logging.getLogger("unpain.desktop")

# Talking to our own server must never go through a proxy. urllib honours the system's,
# and macOS does not exempt 127.0.0.1 by default, so a work laptop's proxy would answer.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# The window follows the UI language chosen in Settings. English needs no table.
STRINGS_DE = {
    "Starting UnPAIN…": "UnPAIN wird gestartet …",
    "UnPAIN is running.": "UnPAIN läuft.",
    "Stopping UnPAIN…": "UnPAIN wird beendet …",
    "UnPAIN could not start.": "UnPAIN konnte nicht starten.",
    "Open UnPAIN": "UnPAIN öffnen",
    "Your data is stored in:": "Deine Daten liegen in:",
    "Show data folder": "Datenordner anzeigen",
    "Open log": "Protokoll öffnen",
    "Quit": "Beenden",
    "Closing this window stops UnPAIN.": "Wenn du dieses Fenster schließt, wird UnPAIN beendet.",
    "UnPAIN is already open. Look for its window.": "UnPAIN ist schon geöffnet. Suche das Fenster von UnPAIN.",
    "No free port for UnPAIN was found.": "Für UnPAIN wurde kein freier Port gefunden.",
}


# ---- where things live ----

def data_root(env=None, home=None):
    """The person's data: ``FA_ROOT`` when set, else ``~/UnPAIN``."""
    env = os.environ if env is None else env
    if env.get("FA_ROOT"):
        return Path(env["FA_ROOT"]).expanduser()
    return Path(home or Path.home()) / APP_NAME


def log_path(platform=None, env=None, home=None):
    """The launcher log, in the place each OS keeps app logs. Never inside the data folder."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    home = Path(home or Path.home())
    if platform == "darwin":
        base = home / "Library" / "Logs" / APP_NAME
    elif platform.startswith("win"):
        base = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local") / APP_NAME / "Logs"
    else:
        base = Path(env.get("XDG_STATE_HOME") or home / ".local" / "state") / "unpain"
    return base / "desktop.log"


def build_version():
    """The version desktop/build.py stamped into the bundle; None when run from source."""
    try:
        info = json.loads((CODE_ROOT / "desktop" / "build_info.json").read_text(encoding="utf-8"))
        return info["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def translator(root):
    try:
        language = json.loads((Path(root) / "config.json").read_text(encoding="utf-8")).get("language")
    except (OSError, ValueError, AttributeError):
        language = None
    table = STRINGS_DE if language == "de" else {}
    return lambda text: table.get(text, text)


# ---- one launcher per data folder ----

class InstanceLock:
    """An OS file lock on a file derived from the data folder.

    The OS drops the lock when the process ends, however it ends, so a crash never leaves
    the app "already running". Like the write lock, the files live in the temp directory:
    outside the data, and per data folder, so separate folders never block each other.
    """

    def __init__(self, root, directory=None):
        key = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]
        directory = Path(directory or tempfile.gettempdir())
        self.lock_path = directory / ("unpain-desktop-%s.lock" % key)
        self.info_path = directory / ("unpain-desktop-%s.json" % key)
        self._handle = None

    def acquire(self):
        handle = open(self.lock_path, "a+b")  # noqa: SIM115 - held for the life of the app
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        # Whatever a crashed run left behind describes a server that no longer exists.
        with contextlib.suppress(OSError):
            self.info_path.unlink()
        return True

    def publish(self, state, port=None):
        """Tell a second launch how this one is doing: "starting", "running" (with the port,
        once the server answers) or "failed"."""
        temp = self.info_path.with_name("%s.%d.tmp" % (self.info_path.name, os.getpid()))
        temp.write_text(json.dumps({"state": state, "port": port, "pid": os.getpid()}), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(temp, self.info_path)
                return
            except PermissionError:       # Windows: a second launch is reading it right now
                if attempt == 4:
                    raise
                time.sleep(0.05)

    def read(self):
        """What the holder last published, or None."""
        try:
            info = json.loads(self.info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return info if isinstance(info, dict) else None

    def release(self):
        if self._handle is None:
            return
        with contextlib.suppress(OSError):
            self.info_path.unlink()
        with contextlib.suppress(OSError):
            if os.name == "nt":
                import msvcrt
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


def _try_bind(port, reuse):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if reuse:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.close()
        return None
    return sock


def _someone_serves(port):
    """Whether something accepts connections on 127.0.0.1:port. Anything but a clear
    refusal counts as yes: a port we are unsure about is a port we do not take."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return True
    except ConnectionRefusedError:
        return False
    except OSError:
        return True


def bind_local_socket(ports=PORTS):
    """A socket bound to 127.0.0.1: the first free preferred port, else any free port.

    Binding here, not in uvicorn, means the port cannot be taken between choosing it and
    serving on it. The catch is our own last run. The server closes the browser's
    connections (idle keep-alives, and all of them on quit), and the closing side keeps
    each one in TIME_WAIT. On macOS that refuses a plain bind for about 30 s, so a quick
    reopen would move to the next port: a new browser origin that has lost what the UI
    keeps in localStorage. SO_REUSEADDR gets past TIME_WAIT. Linux never lets it share a
    port with a live listener, so Linux always sets it. macOS and Windows let it share
    one, so there it is used only after a refused bind, and only when nothing answers on
    the port. The probe waits for a refusal because on Windows a refused loopback connect
    takes about a second. (No SO_EXCLUSIVEADDRUSE on Windows: Microsoft documents that a
    port bound with it cannot be bound again until its old connections are gone, which is
    the delay this function exists to avoid. It only stops a program of the same user from
    taking the port, and such a program can read ~/UnPAIN directly anyway.)
    """
    linux = sys.platform.startswith("linux")
    for port in ports:
        sock = _try_bind(port, reuse=linux)
        if sock is None and not linux and not _someone_serves(port):
            sock = _try_bind(port, reuse=True)
        if sock is not None:
            return sock
    sock = _try_bind(0, reuse=False)
    if sock is None:
        raise OSError("no free local port")
    return sock


def configure_environment(root, port):
    """Must run before app.server is imported: pipeline.util reads FA_ROOT at import."""
    os.environ["FA_ROOT"] = str(root)
    os.environ["UNPAIN_ALLOWED_HOSTS"] = "127.0.0.1:%d,localhost:%d" % (port, port)
    version = build_version()
    if version:
        os.environ.setdefault("UNPAIN_VERSION", version)   # export_meta: no git in an installed app
    if FROZEN:
        # The bundled OpenSSL looks for CA certificates where the build machine kept them:
        # python.org's macOS build only after "Install Certificates.command", Ubuntu's
        # libssl in a path Fedora does not use. Without roots the ECB download fails and
        # so does the first foreign-currency import. A person's own SSL_CERT_FILE wins.
        import certifi
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())


def hold_installer_mutex():
    """Windows: lets the installer see that the app is running instead of overwriting it."""
    if os.name != "nt":
        return None
    import ctypes
    return ctypes.windll.kernel32.CreateMutexW(None, False, WINDOWS_MUTEX)


# ---- the server ----

class AppServer:
    """The FastAPI app in a background thread. The window keeps the main thread, which Tk
    requires on macOS."""

    def __init__(self, sock):
        self.sock = sock
        self.port = sock.getsockname()[1]
        self.url = "http://127.0.0.1:%d/" % self.port
        self.server = None
        self.error = None
        self._stop_requested = False
        self.thread = threading.Thread(target=self._run, name="unpain-server", daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        try:
            import uvicorn
            from app.server import app   # after configure_environment()
            config = uvicorn.Config(
                app, log_config=None, access_log=False, loop="asyncio", http="h11", ws="none",
                lifespan="on", timeout_graceful_shutdown=10)
            self.server = uvicorn.Server(config)
            # Quit can arrive while app.server is still importing (seconds, on a first start
            # that an antivirus is scanning). Then there was no server to tell, so it is
            # told here, before it ever serves. request_stop() covers the moment after.
            if self._stop_requested:
                return
            self.server.run(sockets=[self.sock])
        except BaseException:              # noqa: BLE001 - shown in the window and logged
            self.error = traceback.format_exc()
            log.error("the server stopped with an error:\n%s", self.error)
        finally:
            with contextlib.suppress(OSError):
                self.sock.close()

    @property
    def started(self):
        return bool(self.server is not None and self.server.started)

    @property
    def alive(self):
        return self.thread.is_alive()

    def wait_started(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.alive and not self.started:
            time.sleep(0.05)
        return self.started

    def request_stop(self):
        """Ask the server to stop, whether or not it has started yet."""
        self._stop_requested = True
        if self.server is not None:
            self.server.should_exit = True     # uvicorn finishes in-flight requests first

    def stop(self, timeout=STOP_TIMEOUT):
        self.request_stop()
        self.thread.join(timeout)


# ---- talking to the desktop ----

def _child_env():
    """The environment for a browser or file manager we start. PyInstaller points
    LD_LIBRARY_PATH at its bundle (a child would load our libraries instead of its own)
    and TCL_LIBRARY/TK_LIBRARY at its Tk (a Tk-based child would load ours)."""
    env = dict(os.environ)
    if FROZEN:
        env.pop("TCL_LIBRARY", None)
        env.pop("TK_LIBRARY", None)
        if sys.platform.startswith("linux"):
            original = env.pop("LD_LIBRARY_PATH_ORIG", None)
            if original is None:
                env.pop("LD_LIBRARY_PATH", None)
            else:
                env["LD_LIBRARY_PATH"] = original
    return env


def open_url(url):
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", url], env=_child_env())
            return
        if sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", url], env=_child_env())
            return
    except OSError:
        pass
    webbrowser.open(url)        # Windows, or no xdg-open: the standard library's own search


def reveal(path):
    """Show a folder, or open a file in its default app (the log opens in a text editor)."""
    path = Path(path)
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-t", str(path)] if path.is_file() else ["open", str(path)],
                             env=_child_env())
        elif os.name == "nt":
            os.startfile(str(path))          # noqa: S606 - a local path we created
        else:
            subprocess.Popen(["xdg-open", str(path)], env=_child_env())
    except OSError:
        log.exception("could not open %s", path)


def _answers(port):
    try:
        with _LOCAL.open("http://127.0.0.1:%d/api/meta" % port, timeout=2) as response:
            return response.status == 200
    except OSError:                         # URLError, refused, timed out
        return False


def find_running(lock, timeout=START_TIMEOUT):
    """The port of the launcher that holds ``lock``, once it answers; None if it failed.

    While it is still starting this keeps waiting, as long as the first launch itself is
    allowed to take: a second double-click during a slow first start should open the
    browser when the app is ready, not tell the person to go and look for a window."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = lock.read() or {}
        if info.get("state") == "failed":
            return None
        port = info.get("port")
        if info.get("state") == "running" and isinstance(port, int) and _answers(port):
            return port
        time.sleep(0.25)
    return None


def setup_logging(path):
    """A windowed build has no console (on Windows sys.stdout is None), so everything that
    would have gone there (print() in app.server, tracebacks from threads and Tk callbacks,
    uvicorn's own log) goes to one file instead. Run from a terminal, the console stays."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        if path.stat().st_size > 2_000_000:
            os.replace(path, path.with_name(path.name + ".1"))
    stream = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives as long as the app
    if FROZEN:
        sys.stdout = sys.stderr = stream
    logging.basicConfig(level=logging.INFO, stream=stream, force=True,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return stream


def show_message(text, title=APP_NAME):
    try:
        import tkinter
        from tkinter import messagebox
        window = tkinter.Tk()
        window.withdraw()
        messagebox.showinfo(title, text, parent=window)
        window.destroy()
    except Exception:                       # noqa: BLE001 - no display: the log has it
        log.warning("could not show message: %s", text)


# ---- the window ----

def scaled_font_size(size, delta, minimum=1):
    """A Tk font size ``delta`` steps larger. Tk 8.6 on X11 gives sizes in pixels, as a
    negative number (-12), so the magnitude is scaled and the sign kept: -12 + 8 would be
    a 4-pixel title. 0 means "the platform default"; it is treated as 10."""
    size = size or 10
    sign = -1 if size < 0 else 1
    return sign * max(abs(size) + delta, minimum)


def run_window(server, root, lock, t, log_file):
    import tkinter as tk
    from tkinter import font as tkfont
    from tkinter import ttk

    if os.name == "nt":
        with contextlib.suppress(Exception):
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # sharp text on scaled displays

    window = tk.Tk(className=APP_NAME)
    window.title(APP_NAME)
    window.resizable(False, False)
    with contextlib.suppress(tk.TclError):
        window._icon = tk.PhotoImage(file=str(CODE_ROOT / "desktop" / "icon.png")).subsample(4)
        window.iconphoto(True, window._icon)

    base = tkfont.nametofont("TkDefaultFont")
    title_font = base.copy()
    title_font.configure(size=scaled_font_size(base.cget("size"), 8), weight="bold")
    small_font = base.copy()
    small_font.configure(size=scaled_font_size(base.cget("size"), -1, minimum=9))

    frame = ttk.Frame(window, padding=(28, 22, 28, 20))
    frame.grid(sticky="nsew")
    frame.columnconfigure(0, weight=1)
    ttk.Label(frame, text=APP_NAME, font=title_font).grid(row=0, column=0, columnspan=2, sticky="w")
    status = ttk.Label(frame, text=t("Starting UnPAIN…"))
    status.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 2))
    detail = ttk.Label(frame, text="", wraplength=380, font=small_font)   # the address, or what went wrong
    detail.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 14))
    open_button = ttk.Button(frame, text=t("Open UnPAIN"), state="disabled", default="active",
                             command=lambda: open_url(server.url))
    open_button.grid(row=3, column=0, columnspan=2, sticky="ew", ipady=4)
    ttk.Label(frame, text=t("Your data is stored in:"), font=small_font).grid(
        row=4, column=0, columnspan=2, sticky="w", pady=(16, 0))
    ttk.Label(frame, text=str(root), font=small_font, wraplength=380).grid(
        row=5, column=0, columnspan=2, sticky="w")
    buttons = ttk.Frame(frame)
    buttons.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(14, 0))
    folder_button = ttk.Button(buttons, text=t("Show data folder"), command=lambda: reveal(root))
    folder_button.pack(side="left")
    log_button = ttk.Button(buttons, text=t("Open log"), command=lambda: reveal(log_file))
    quit_button = ttk.Button(buttons, text=t("Quit"))
    quit_button.pack(side="right")
    ttk.Label(frame, text=t("Closing this window stops UnPAIN."), font=small_font).grid(
        row=7, column=0, columnspan=2, sticky="w", pady=(12, 0))

    state = {"opened": False, "stopping": False, "deadline": 0.0}

    def quit_app():
        if state["stopping"]:
            return
        state["stopping"] = True
        state["deadline"] = time.monotonic() + STOP_TIMEOUT
        status.configure(text=t("Stopping UnPAIN…"))
        for button in (open_button, folder_button, quit_button):
            button.configure(state="disabled")
        server.request_stop()
        poll()

    def tell_second_launch(state, port=None):
        try:
            lock.publish(state, port)
        except OSError:                 # never worth leaving the window half-updated for
            log.exception("could not publish the launcher state")

    def reopen():
        window.deiconify()
        window.lift()
        if server.started and not state["stopping"]:
            open_url(server.url)

    def poll():
        if state["stopping"]:
            if not server.alive or time.monotonic() > state["deadline"]:
                window.destroy()
                return
        elif not server.alive:          # never started, or stopped on its own: never say "running"
            status.configure(text=t("UnPAIN could not start."))
            open_button.configure(state="disabled")
            last_line = (server.error or "").strip().splitlines()[-1:] or [str(log_file)]
            detail.configure(text=last_line[0])
            log_button.pack(side="left", padx=(8, 0))
            tell_second_launch("failed")
            return
        elif server.started and not state["opened"]:
            state["opened"] = True
            status.configure(text=t("UnPAIN is running."))
            detail.configure(text=server.url.rstrip("/"))   # typeable into any browser, and bookmarkable
            open_button.configure(state="normal")
            log.info("serving %s from %s", server.url, root)
            open_url(server.url)
            tell_second_launch("running", server.port)
        window.after(150, poll)

    quit_button.configure(command=quit_app)
    window.bind("<Return>", lambda _event: open_button.invoke())
    window.protocol("WM_DELETE_WINDOW", quit_app)
    window.update_idletasks()
    window.geometry("+%d+%d" % ((window.winfo_screenwidth() - window.winfo_reqwidth()) // 2,
                                (window.winfo_screenheight() - window.winfo_reqheight()) // 3))
    if sys.platform == "darwin":
        window.createcommand("tk::mac::Quit", quit_app)                 # Cmd+Q and the app menu
        window.createcommand("tk::mac::ReopenApplication", reopen)      # Dock click, second open
    window.after(150, poll)
    window.mainloop()


# ---- entry points ----

def self_test(report_path):
    """Start the bundled server on a fresh, empty data folder and check what a first run
    needs: the page, the bundled files, the setup wizard's seed files, the bank formats and
    the local-only guard. No window, so CI can run it on every OS. Exit code 0 = passed."""
    report_path = Path(report_path).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    stream = None
    if FROZEN:      # no console on a windowed build: keep the server's output next to the report
        stream = open(report_path.with_suffix(".log"), "w", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stderr = stream
        logging.basicConfig(level=logging.INFO, stream=stream, force=True)
    root = Path(tempfile.mkdtemp(prefix="unpain-selftest-"))
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": str(detail)[:500]})

    sock = bind_local_socket(())
    port = sock.getsockname()[1]
    configure_environment(root, port)
    server = AppServer(sock)
    server.start()
    started = server.wait_started(START_TIMEOUT)
    check("server starts", started, server.error or "")
    base = "http://127.0.0.1:%d" % port

    def call(method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(base + path, data=data, method=method, headers=dict(headers or {}))
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with _LOCAL.open(request, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    if started:
        try:
            status, body = call("GET", "/")
            check("index page", status == 200 and b"<html" in body.lower(), status)
            status, _ = call("GET", "/static/app.js")
            check("bundled static files", status == 200, status)
            status, body = call("GET", "/api/meta")
            check("fresh folder asks for setup", status == 200 and json.loads(body).get("setup_required"), body)
            status, body = call("POST", "/api/setup", {"person1": "Alex", "person2": "Sam"})
            check("setup wizard (bundled seed files)", status == 200, body)
            status, body = call("GET", "/api/meta")
            check("configured after setup", status == 200 and not json.loads(body).get("setup_required"), body)
            from pipeline.formats import load_formats
            check("bank formats bundled", len(load_formats()) > 0, len(load_formats()))
            from app import server as app_server
            check("PDF extractors bundled", len(app_server.PDF_EXTRACTORS) > 0, sorted(app_server.PDF_EXTRACTORS))
            if FROZEN:
                # A build Python without tkinter still builds, and passes every check above,
                # then fails the first time somebody opens it. Tcl() needs no display.
                try:
                    import tkinter
                    check("window toolkit bundled", True, "Tk " + tkinter.Tcl().eval("info patchlevel"))
                except Exception as exc:    # noqa: BLE001 - reported as the failed check
                    check("window toolkit bundled", False, repr(exc))
                # Without CA roots the ECB download, and so every foreign-currency import,
                # fails on the user's machine; see configure_environment().
                import ssl
                roots = ssl.create_default_context().cert_store_stats()["x509_ca"]
                source = os.environ.get("SSL_CERT_FILE") or ssl.get_default_verify_paths().cafile
                check("TLS root certificates bundled", roots > 0, "%d roots from %s" % (roots, source))

            # The local-only guard, as a browser would meet it.
            status, _ = call("GET", "/api/meta", headers={"Host": "attacker.example"})
            check("foreign Host refused", status == 403, status)
            status, _ = call("POST", "/api/setup", {"person1": "A", "person2": "B"},
                             headers={"Origin": "http://attacker.example"})
            check("cross-site write refused", status == 403, status)
            status, _ = call("GET", "/api/meta", headers={"Sec-Fetch-Site": "cross-site"})
            check("cross-site read refused", status == 403, status)
            status, _ = call("GET", "/api/backup", headers={"Sec-Fetch-Site": "cross-site"})
            check("cross-site GET that writes refused", status == 403 and not (root / "backups").exists(), status)
            status, _ = call("GET", "/", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})
            check("a link from elsewhere still opens the page", status == 200, status)
            # ...and the app's own page must still get through: a browser always sends these.
            # (409 = already set up: past the guard, refused by the handler.)
            own = {"Origin": "http://127.0.0.1:%d" % port, "Sec-Fetch-Site": "same-origin"}
            status, _ = call("POST", "/api/setup", {"person1": "A", "person2": "B"}, headers=own)
            check("the app's own page may write", status != 403 and status < 500, status)
            own = {"Host": "localhost:%d" % port, "Origin": "http://localhost:%d" % port,
                   "Sec-Fetch-Site": "same-origin"}
            status, _ = call("POST", "/api/setup", {"person1": "A", "person2": "B"}, headers=own)
            check("the app's own page via localhost may write", status != 403 and status < 500, status)
        except Exception:                   # noqa: BLE001 - a crash is a failed check
            check("self-test ran to the end", False, traceback.format_exc())
    server.stop()
    check("server stops", not server.alive)

    ok = all(item["ok"] for item in checks)
    report = {"ok": ok, "version": build_version(), "frozen": FROZEN, "platform": sys.platform,
              "python": sys.version.split()[0], "checks": checks}
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    shutil.rmtree(root, ignore_errors=True)
    if stream is not None:
        stream.close()
    return 0 if ok else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--self-test"]:
        return self_test(argv[1] if len(argv) > 1 else "unpain-self-test.json")

    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    log_file = log_path()
    setup_logging(log_file)
    t = translator(root)
    log.info("UnPAIN %s starting (data: %s)", build_version() or "from source", root)

    lock = InstanceLock(root)
    if not lock.acquire():
        port = find_running(lock)
        if port:
            log.info("already running on port %d; opening the browser", port)
            open_url("http://127.0.0.1:%d/" % port)
            return 0
        show_message(t("UnPAIN is already open. Look for its window."))
        return 1

    try:
        sock = bind_local_socket()
    except OSError:
        log.exception("no free port")
        show_message(t("No free port for UnPAIN was found."))
        lock.release()
        return 1
    with contextlib.suppress(OSError):
        lock.publish("starting")      # a second launch now waits instead of giving up
    configure_environment(root, sock.getsockname()[1])
    _mutex = hold_installer_mutex()   # noqa: F841 - held until the process exits
    server = AppServer(sock)
    server.start()
    stop_wait = STOP_TIMEOUT          # the window crashed: give in-flight requests their time
    try:
        run_window(server, root, lock, t, log_file)
        stop_wait = 2                 # the window already waited for the server; no silent extra wait
    finally:
        server.stop(stop_wait)
        lock.release()
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
