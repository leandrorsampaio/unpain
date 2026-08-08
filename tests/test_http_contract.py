"""The app as something on the other end of a socket, not as a bag of functions.

Nearly every backend test calls the handler directly, which skips everything between a
request and that call: the lock middleware, the write serialization, Pydantic's
rejection of a malformed body, the status code that comes back, the cookie that carries
a session. Those are the parts a browser actually meets, and a handler that is correct
in isolation can still be unreachable, unserialized, or answering 500 where it meant to
answer 400.

It also covers what only exists between concurrent requests. Read-modify-write over a
whole document is the shape of almost every write here, so two overlapping requests used
to be able to read the same document and let the later one discard the earlier one's
work — silently, which is the part that matters: nobody would have known which decision
went missing.

Usage: .venv/bin/python tests/test_http_contract.py
"""
import http.cookiejar
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sandbox import PROJECT, build_sandbox

tmp = Path(tempfile.mkdtemp(prefix="fa-http-contract-"))
os.environ["FA_ROOT"] = str(tmp)
build_sandbox(tmp)

sys.path.insert(0, str(PROJECT))
from pipeline import ingest, store  # noqa: E402
from pipeline.mutation_lock import mutation_lock  # noqa: E402
from pipeline.util import write_json  # noqa: E402
from app import server  # noqa: E402

ingest.run(verbose=False)

# A real uvicorn process rather than an in-process ASGI client: the point of this file
# is everything between the socket and the handler, and starlette's TestClient needs
# httpx, which this project does not depend on and should not grow a dependency for.
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
PORT = sock.getsockname()[1]
sock.close()
BASE = "http://127.0.0.1:%d" % PORT
SERVER = subprocess.Popen(
    [str(PROJECT / ".venv" / "bin" / "uvicorn"), "app.server:app",
     "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
    cwd=str(PROJECT), env=dict(os.environ, FA_ROOT=str(tmp)),
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


class Response:
    def __init__(self, status, headers, body):
        self.status_code, self.headers, self.content = status, headers, body

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.content)


class Session:
    """Just enough client for this file, on the standard library."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method, path, json_body=None, content=None, files=None, timeout=30):
        headers, data = {}, None
        if json_body is not None:
            data, headers["Content-Type"] = json.dumps(json_body).encode(), "application/json"
        elif content is not None:
            data = content if isinstance(content, bytes) else str(content).encode()
            headers["Content-Type"] = "application/json"
        elif files is not None:
            name, (filename, payload, mime) = next(iter(files.items()))
            boundary = uuid.uuid4().hex
            data = (b"--" + boundary.encode() + b"\r\n"
                    + ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                       % (name, filename)).encode()
                    + ("Content-Type: %s\r\n\r\n" % mime).encode()
                    + payload + b"\r\n--" + boundary.encode() + b"--\r\n")
            headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
        request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return Response(response.status, dict(response.headers), response.read())
        except urllib.error.HTTPError as exc:
            return Response(exc.code, dict(exc.headers), exc.read())

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def cookie(self, name):
        return next((c.value for c in self.jar if c.name == name), None)


client = Session()
deadline = time.time() + 30
while time.time() < deadline:
    try:
        if client.get("/api/meta", timeout=2).status_code == 200:
            break
    except Exception:
        time.sleep(0.2)
else:
    SERVER.kill()
    raise SystemExit("server never came up:\n" + SERVER.stdout.read().decode()[-3000:])

failures = []
total_checks = 0


def check(name, cond, detail=""):
    global total_checks
    total_checks += 1
    if not cond:
        print("  FAIL %s %s" % (name, detail))
        failures.append(name)
    return cond


def finish(code=0):
    SERVER.terminate()
    try:
        SERVER.wait(timeout=10)
    except subprocess.TimeoutExpired:
        SERVER.kill()
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(code)


def set_month(year, month, state):
    return client.post("/api/close", json_body={"year": year, "month": month, "state": state})


YEAR = store.years()[0]
TXNS = store.load_year_raw(YEAR)
CATEGORY = sorted(server._decision_options()[0] - {"auto:items"})[0]


print("== the shape of an answer")
meta = client.get("/api/meta")
check("GET /api/meta answers 200", meta.status_code == 200, str(meta.status_code))
check("and it is JSON with the household in it", "people" in meta.json(), meta.text[:120])
check("the app shell is served", client.get("/").status_code == 200)
check("an unknown API path is 404", client.get("/api/nope").status_code == 404)
check("a GET on a POST-only endpoint is 405",
      client.get("/api/decision").status_code == 405)


print("== a malformed request is rejected by the door, not by a stack trace")
MALFORMED = {
    "a body that is not JSON at all": ("/api/decision", {"content": b"not json"}),
    "a missing required field": ("/api/decision", {"json_body": {"year": YEAR}}),
    "a string where a number belongs": ("/api/decision",
                                        {"json_body": {"year": "soon", "id": "x", "fields": {}}}),
    "a null where an object belongs": ("/api/decision",
                                       {"json_body": {"year": YEAR, "id": "x", "fields": None}}),
    "an unknown decision field": ("/api/decision",
                                 {"json_body": {"year": YEAR, "id": TXNS[0]["id"],
                                                "fields": {"nonsense": 1}}}),
}
for label, (path, kwargs) in sorted(MALFORMED.items()):
    response = client.post(path, **kwargs)
    check("rejected cleanly: %s" % label, response.status_code in (400, 404, 422),
          "got %s: %s" % (response.status_code, response.text[:120]))
    check("and the answer is JSON, not a stack trace: %s" % label,
          response.headers.get("content-type", "").startswith("application/json"),
          response.headers.get("content-type", ""))


print("== the status codes the UI branches on")
missing = client.post("/api/decision", json_body={"year": YEAR, "id": "no-such-id",
                                             "fields": {"category": CATEGORY}})
check("an unknown transaction is 404", missing.status_code == 404, missing.text[:120])

set_month(YEAR, int(TXNS[0]["date"][5:7]), "closed")
locked = client.post("/api/decision", json_body={"year": YEAR, "id": TXNS[0]["id"],
                                            "fields": {"category": CATEGORY}})
check("a closed month is 409 over HTTP too", locked.status_code == 409, locked.text[:160])
check("and the 409 explains itself", "closed" in locked.text.lower(), locked.text[:160])
set_month(YEAR, int(TXNS[0]["date"][5:7]), "open")

bad_rule = client.post("/api/rule", json_body={"pattern": "", "category": None})
check("an empty rule pattern is 400 over HTTP", bad_rule.status_code == 400, bad_rule.text[:120])
bad_part = client.get("/api/backup?parts=dta")
check("an unknown backup part is 400, not a whole-tree backup",
      bad_part.status_code == 400, bad_part.text[:120])


print("== uploads")
empty_upload = client.post("/api/ingest/upload", files={"file": ("x.csv", b"", "text/csv")})
check("an empty upload is refused", empty_upload.status_code >= 400, empty_upload.text[:120])
wrong_type = client.post("/api/ingest/upload",
                         files={"file": ("notes.txt", b"hello", "text/plain")})
check("an unsupported file type is refused", wrong_type.status_code >= 400, wrong_type.text[:120])
normalized = (b"date,amount,currency,counterparty,purpose\n"
              b"2026-03-04,-12.34,EUR,TEST,normalized extraction\n")
ungated = client.post("/api/ingest/upload",
                      files={"file": ("renamed.csv", normalized, "text/csv")})
check("normalized extractor output is refused during preview, not later during Process",
      ungated.status_code == 400 and "reconciliation report" in ungated.text,
      "%s %s" % (ungated.status_code, ungated.text[:200]))


print("== the lock middleware stands between a request and the data")
check("setting a password succeeds",
      client.post("/api/security/set-password",
                  json_body={"new_password": "correct horse", "current_password": ""}).status_code == 200)
try:
    walled = Session()
    denied = walled.get("/api/summary?year=%d" % YEAR)
    check("a locked app answers 401 before reaching the handler",
          denied.status_code == 401, "%s %s" % (denied.status_code, denied.text[:80]))
    check("and a write is refused too",
          walled.post("/api/decision", json_body={"year": YEAR, "id": TXNS[0]["id"],
                                             "fields": {}}).status_code == 401)
    check("the unlock endpoint itself stays reachable",
          walled.post("/api/unlock", json_body={"password": "wrong"}).status_code == 401)
    opened = walled.post("/api/unlock", json_body={"password": "correct horse"})
    check("the right password unlocks", opened.status_code == 200, opened.text[:120])
    check("and hands back a session cookie", bool(walled.cookie("fa_session")),
          str([c.name for c in walled.jar]))
    check("after which reads work again",
          walled.get("/api/summary?year=%d" % YEAR).status_code == 200)
    walled.post("/api/lock")
    check("locking again closes the door",
          walled.get("/api/summary?year=%d" % YEAR).status_code == 401)
finally:
    removed = client.post("/api/security/remove-password",
                          json_body={"current_password": "correct horse"})
    check("the password can be removed again", removed.status_code == 200, removed.text[:160])
check("removing the password reopens the app",
      client.get("/api/summary?year=%d" % YEAR).status_code == 200)


print("== concurrent writes: both survive, or one is told")
# The async middleware must coordinate with a separate CLI process, not only other
# requests inside this uvicorn. Hold the same file lock here and prove a write waits;
# GETs remain outside the lock and can still answer while it does.
with ThreadPoolExecutor(max_workers=1) as pool:
    with mutation_lock():
        waiting = pool.submit(client.post, "/api/rule", json_body={
            "pattern": "CROSS PROCESS LOCK", "category": CATEGORY})
        time.sleep(0.15)
        check("an HTTP mutation waits for the cross-process writer lock", not waiting.done())
        check("reads remain responsive while a mutation waits on another process",
              client.get("/api/summary?year=%d" % YEAR).status_code == 200)
    response = waiting.result(timeout=5)
    check("the waiting mutation completes after the cross-process lock is released",
          response.status_code == 200, response.text[:160])
check("the HTTP inbox command reuses its middleware lock without deadlocking",
      client.post("/api/ingest").status_code == 200)

# Twenty decisions, ten at a time, each on a different transaction. Every one is a
# read-modify-write of the same decisions.json. Before writes were serialized this lost
# entries — not with an error, just with fewer decisions than requests.
targets = [t["id"] for t in TXNS[:20]]
store.save_decisions(YEAR, {})


def decide(txn_id):
    return client.post("/api/decision", json_body={"year": YEAR, "id": txn_id,
                                              "fields": {"category": CATEGORY}}).status_code


with ThreadPoolExecutor(max_workers=10) as pool:
    codes = list(pool.map(decide, targets))
check("every concurrent decision was accepted", set(codes) == {200}, str(sorted(set(codes))))
stored = store.decisions(YEAR)
check("and every one of them survived", all(t in stored for t in targets),
      "%d of %d stored" % (sum(t in stored for t in targets), len(targets)))

# The same pressure on a different document, through a different endpoint.
before_rules = len(client.get("/api/rules").json()["rules"])


def add_rule(index):
    return client.post("/api/rule", json_body={"pattern": "CONCURRENT MERCHANT %02d" % index,
                                          "category": CATEGORY}).status_code


with ThreadPoolExecutor(max_workers=8) as pool:
    codes = list(pool.map(add_rule, range(16)))
check("every concurrent rule was accepted", set(codes) == {200}, str(sorted(set(codes))))
check("and none of the 16 overwrote another",
      len(client.get("/api/rules").json()["rules"]) == before_rules + 16,
      "%d rules, expected %d" % (len(client.get("/api/rules").json()["rules"]), before_rules + 16))
ids = [r["id"] for r in client.get("/api/rules").json()["rules"]]
check("and each got its own id", len(ids) == len(set(ids)))


print("== a failed write leaves the previous state, never a mixture")
decisions_path = tmp / "data" / str(YEAR) / "decisions.json"
before_bytes = decisions_path.read_bytes()
try:
    write_json(decisions_path, {"poisoned": float("nan")})
except ValueError:
    pass
check("a document that cannot be serialized is not published",
      decisions_path.read_bytes() == before_bytes)
check("and no temporary file is left behind",
      not [p for p in decisions_path.parent.glob("*.tmp*")],
      str([p.name for p in decisions_path.parent.glob("*.tmp*")]))

cash_path = tmp / "inbox" / "cash.csv"
before_cash = cash_path.read_bytes() if cash_path.exists() else None
broken = client.post("/api/cash", json_body={"date": "%d-06-15" % YEAR, "account": "no-such-account",
                                        "amount": -5.0, "currency": "EUR",
                                        "description": "should not land", "category": ""})
check("a cash entry for an unknown account is refused", broken.status_code >= 400,
      broken.text[:120])
check("and cash.csv is byte-identical afterwards",
      (cash_path.read_bytes() if cash_path.exists() else None) == before_cash)

# A real cash write, then a rollback proof: the ledger and the CSV move together.
added = client.post("/api/cash", json_body={"date": "%d-06-16" % YEAR, "account": "cash-person1",
                                       "amount": -7.77, "currency": "EUR",
                                       "description": "atomic cash", "category": ""})
check("a valid cash entry is accepted", added.status_code == 200, added.text[:200])
csv_rows = [line for line in cash_path.read_text().splitlines() if "atomic cash" in line]
ledger = [t for y in store.years() for t in store.load_year_by_file(y).get("cash.jsonl", [])
          if t["purpose"] == "atomic cash" or t["counterparty"] == "atomic cash"]
check("the source CSV and the derived ledger agree afterwards",
      len(csv_rows) == 1 and len(ledger) == 1, "%d csv rows, %d ledger rows" % (len(csv_rows), len(ledger)))


print("== files come back as files")
backup = client.get("/api/backup?parts=rules")
check("a backup downloads", backup.status_code == 200, str(backup.status_code))
check("and it is a readable zip holding only what was asked for",
      zipfile.is_zipfile(io.BytesIO(backup.content))
      and all(name.startswith("rules") for name in zipfile.ZipFile(io.BytesIO(backup.content)).namelist()),
      str(zipfile.ZipFile(io.BytesIO(backup.content)).namelist()[:5]))
check("a path outside the served tree is refused",
      client.get("/receipts/../config.json").status_code in (400, 404), "path traversal")


# Anti-shrink guard: exact count at implementation time. May only ever be RAISED
# when checks are added — never lowered (see AGENTS.md: never weaken a test).
MIN_CHECKS = 45
check("suite did not shrink", total_checks >= MIN_CHECKS,
      "total_checks=%d < %d" % (total_checks, MIN_CHECKS))

if failures:
    print("\nFAILED: %s" % ", ".join(sorted(set(failures))))
    finish(1)
print("\nHTTP contract passed: %d checks over a real server process." % total_checks)
finish(0)
