"""Real-data tripwire used by run-tests.sh.

Tests must never touch the user's real data — every test runs inside an
FA_ROOT sandbox (tests/sandbox.py). This guard makes that rule physical:
`record` snapshots the real tree before the suite, `verify` fails the suite
if anything changed. The snapshot lives in a caller-provided temp file, never
inside the repository.

Usage: tripwire.py record <snapshot-file> | tripwire.py verify <snapshot-file>
"""
import json
import os
import sys

WATCHED = ["data", "rules", "inbox", "receipts", "config.json"]


def snapshot():
    out = {}
    for base in WATCHED:
        if os.path.isfile(base):
            st = os.stat(base)
            out[base] = [st.st_mtime_ns, st.st_size]
        elif os.path.isdir(base):
            for root, _, files in os.walk(base):
                for name in files:
                    path = os.path.join(root, name)
                    st = os.stat(path)
                    out[path] = [st.st_mtime_ns, st.st_size]
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
