"""Build and ingest the isolated data set used by the browser smoke test."""
import os
import sys
from pathlib import Path

from sandbox import PROJECT, build_sandbox


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: ui_smoke_bootstrap.py <sandbox-dir>")
    root = build_sandbox(Path(sys.argv[1]))
    os.environ["FA_ROOT"] = str(root)
    sys.path.insert(0, str(PROJECT))

    from pipeline import anchors, ingest, store
    from pipeline.util import cents

    results = ingest.run(verbose=False)
    errors = ["%s: %s" % result for result in results if result[1].startswith("ERROR:")]
    if errors:
        raise SystemExit("Sandbox ingest failed: " + " | ".join(errors))

    # Give the coverage screenshot deterministic examples of every anchor state.
    bank1_total = sum(cents(txn["amount_original"]) for txn in store.load_year_raw(2026)
                      if txn["account"] == "bank1-person1") / 100.0
    anchors.add_manual("bank1-person1", "2026-05-31", 0)
    anchors.add_manual("bank1-person1", "2026-06-30", bank1_total)
    anchors.add_manual("bank2-person2", "2026-05-31", 0)
    anchors.add_manual("bank2-person2", "2026-06-30", 0)
    print("Browser sandbox ready at %s" % root)


if __name__ == "__main__":
    main()
