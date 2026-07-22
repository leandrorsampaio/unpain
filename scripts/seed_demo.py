#!/usr/bin/env python3
"""Seed an isolated DEMO instance with synthetic multi-year data (Alex & Sam).

Everything is written under ``$FA_ROOT`` (default ``./demo``) — NEVER the repo's real
``data/``. Reuses the deterministic fixture generator (``scripts/gen_test_fixture.py``) so the
demo shows the same rich, correct dataset the test oracle verifies.

Run via ``./start.sh --demo``, or directly:

    FA_ROOT=./demo .venv/bin/python scripts/seed_demo.py
"""
import json
import os
import shutil
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
EXAMPLES = PROJECT / "examples"
ROOT = Path(os.environ.get("FA_ROOT") or (PROJECT / "demo")).resolve()
MARKER = ".fa-demo"

# --- safety guards: never touch the repo or an unknown (possibly real) data dir ---------
if ROOT == PROJECT:
    sys.exit("refusing to seed demo data into the repo root; set FA_ROOT to a demo directory")
if ROOT.exists() and any(ROOT.iterdir()) and not (ROOT / MARKER).exists():
    sys.exit(f"refusing to overwrite {ROOT}: it is non-empty and not a demo dir "
             f"(no {MARKER} marker). Point FA_ROOT at a fresh directory.")

# make FA_ROOT visible to the pipeline (pipeline.util.ROOT reads it at import time)
os.environ["FA_ROOT"] = str(ROOT)
sys.path.insert(0, str(PROJECT))

# --- fresh demo tree --------------------------------------------------------------------
if ROOT.exists():
    shutil.rmtree(ROOT)
ROOT.mkdir(parents=True)
(ROOT / MARKER).write_text("This directory holds throwaway demo data. Safe to delete.\n", encoding="utf-8")
(ROOT / "rules").mkdir()
(ROOT / "data").mkdir()
(ROOT / "scripts").mkdir()  # gen_test_fixture writes its EXPECTED_RESULTS.md here (harmless in the demo)

# config with friendly display labels (slugs stay person1/person2)
cfg = json.loads((EXAMPLES / "config.json").read_text(encoding="utf-8"))
cfg["people"] = ["person1", "person2"]
cfg["person_labels"] = {"person1": "Alex", "person2": "Sam"}
cfg["household_name"] = "Alex & Sam"
(ROOT / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
shutil.copy(EXAMPLES / "tax-buckets.json", ROOT / "rules" / "tax-buckets.json")

# --- categories covering everything the fixture uses (same construction the oracle uses) --
import scripts.gen_test_fixture as gen  # noqa: E402

cats = {"to-receive": {"type": "income", "subs": []},
        "living-costs": {"type": "expense", "subs": []},
        "core-living": {"type": "expense", "subs": []},
        "recreation": {"type": "expense", "subs": []},
        "living-upgrades": {"type": "expense", "subs": []},
        "donations": {"type": "expense", "subs": []},
        "health": {"type": "expense", "subs": []},
        "sports": {"type": "expense", "subs": []}}
for slug in gen.INCOME_CATS:
    group, sub = slug.split("/")
    cats[group]["subs"].append({"slug": sub, "name": sub, "ratio_income": slug in gen.RATIO_CATS})
for _, cat, _ in gen.RULE_TABLE:
    if cat:
        group, sub = cat.split("/")
        if not any(s["slug"] == sub for s in cats[group]["subs"]):
            cats[group]["subs"].append({"slug": sub, "name": sub})
cats["core-living"]["subs"].append({"slug": "items-over-50", "name": "Items > 50"})
cat_doc = {"categories": [{"slug": k, "name": k, "type": v["type"], "subs": v["subs"]}
                          for k, v in cats.items()]}
(ROOT / "rules" / "categories.json").write_text(json.dumps(cat_doc, indent=2), encoding="utf-8")

# --- generate the fixture (accounts, rules, pinned FX, inbox CSVs, decisions) + ingest ----
gen.main()
from pipeline import ingest  # noqa: E402

ingest.run(verbose=False)

print("\nDemo instance seeded at %s" % ROOT)
print("Start it with:  FA_ROOT=%s .venv/bin/uvicorn app.server:app --port 8765" % ROOT)
print("(or just use ./start.sh --demo, which does both)")
