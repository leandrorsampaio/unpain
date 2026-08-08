"""Real-data tripwire used by run-tests.sh.

Tests must never touch the user's real data — every test runs inside an
FA_ROOT sandbox (tests/sandbox.py). This guard makes that rule physical:
`record` snapshots the real tree before the suite, `verify` fails the suite
if anything changed. The snapshot lives in a caller-provided temp file, never
inside the repository.

Usage: tripwire.py record <snapshot-file> | tripwire.py verify <snapshot-file>
"""
import hashlib
import json
import os
import sys

WATCHED = ["data", "rules", "inbox", "receipts", "config.json"]

# Anything larger than this is hashed by its first and last megabyte plus its size.
# A statement PDF or a backup zip is the only thing here that gets big, and reading
# every byte of one on every test run would make the guard cost more than the suite.
FULL_HASH_LIMIT = 8 * 1024 * 1024


def digest(path, size):
    """Content hash, so a change that preserves mtime and size is still a change.

    mtime+size is what a restore, a `touch -r`, or an in-place edit of the same
    length all leave untouched — and an in-place edit of the same length is exactly
    what a test writing a corrected figure into real data would look like. The same
    reasoning already governs the checked-hash pycs in run-tests.sh.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if size <= FULL_HASH_LIMIT:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        else:
            h.update(f.read(1024 * 1024))
            f.seek(-1024 * 1024, os.SEEK_END)
            h.update(f.read())
    return h.hexdigest()


def entry(path):
    st = os.stat(path)
    try:
        return [st.st_mtime_ns, st.st_size, digest(path, st.st_size)]
    except OSError as exc:
        # An unreadable file is still watched: "cannot read it" is a stable value that
        # changes the moment it becomes readable again, which is itself worth failing on.
        return [st.st_mtime_ns, st.st_size, "unreadable:%s" % exc.errno]


def snapshot():
    out = {}
    for base in WATCHED:
        if os.path.isfile(base):
            out[base] = entry(base)
        elif os.path.isdir(base):
            for root, _, files in os.walk(base):
                for name in files:
                    path = os.path.join(root, name)
                    out[path] = entry(path)
    return out


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("record", "verify"):
        raise SystemExit(__doc__)
    mode, path = sys.argv[1], sys.argv[2]
    if mode == "record":
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot(), f)
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            before = json.load(f)
    except (FileNotFoundError, ValueError):
        return 0  # no snapshot -> nothing to compare (e.g. CI without real data)
    after = snapshot()
    changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    if changed:
        for p in changed:
            print("Changed: %s" % p)
        print("Tests modified real data — every test must run inside an FA_ROOT sandbox (tests/sandbox.py).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
