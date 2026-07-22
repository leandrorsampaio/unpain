"""CLI entry point: python -m pipeline.cli <command>"""
import argparse
import json
import sys

from . import doctor, fx, ingest, settle, store
from .util import ConfigError, load_config


def main():
    p = argparse.ArgumentParser(prog="pipeline", description="FamilyAccountability pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest", help="process all files in inbox/")
    sp = sub.add_parser("status", help="review status per year")
    sp.add_argument("year", nargs="?", type=int)
    sp = sub.add_parser("summary", help="year summary JSON")
    sp.add_argument("year", type=int)
    sp = sub.add_parser("settle", help="settlement for a year (annual, binding)")
    sp.add_argument("year", type=int)
    sp.add_argument("--month", type=int, help="monthly estimate instead")
    sp = sub.add_parser("tax", help="tax evidence pack for a year")
    sp.add_argument("year", type=int)
    sub.add_parser("fx-update", help="refresh ECB rates cache")
    sp = sub.add_parser("doctor", help="read-only data integrity check")
    sp.add_argument("year", nargs="?", type=int)
    args = p.parse_args()
    load_config()  # fail fast with a clear message if config.json is missing/invalid

    if args.cmd == "ingest":
        print("Ingesting inbox/ ...")
        ingest.run()
    elif args.cmd == "status":
        years = [args.year] if args.year else store.years()
        for y in years:
            txns = store.effective_year(y)
            review = sum(1 for t in txns if t["status"] == "needs_review")
            internal = sum(1 for t in txns if t.get("sharing") == "out-of-scope")
            print("%d: %5d transactions | %4d need review | %4d out of scope" % (y, len(txns), review, internal))
    elif args.cmd == "summary":
        json.dump(settle.year_summary(args.year), sys.stdout, indent=2, ensure_ascii=False)
    elif args.cmd == "settle":
        json.dump(settle.settlement(args.year, args.month), sys.stdout, indent=2, ensure_ascii=False)
    elif args.cmd == "tax":
        json.dump(settle.tax_report(args.year), sys.stdout, indent=2, ensure_ascii=False)
    elif args.cmd == "fx-update":
        fx._download()
        print("ECB rates updated.")
    elif args.cmd == "doctor":
        result = doctor.run(args.year)
        counts = {severity: sum(item["severity"] == severity for item in result["findings"])
                  for severity in ("error", "warning", "info")}
        print("Doctor: %d errors, %d warnings, %d info; %d years, %d transactions, %d decisions checked" %
              (counts["error"], counts["warning"], counts["info"],
               len(result["checked"]["years"]), result["checked"]["transactions"],
               result["checked"]["decisions"]))
        for severity in ("error", "warning", "info"):
            items = [item for item in result["findings"] if item["severity"] == severity]
            if not items:
                continue
            print("%s:" % severity.upper())
            for item in items:
                ids = " [%s]" % ", ".join(str(value) for value in item["ids"]) if item["ids"] else ""
                scope = str(item["year"]) if item["year"] else "all years"
                print("  %s %s: %s%s" % (item["check"], scope, item["message"], ids))
        return 1 if counts["error"] else 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ConfigError as e:
        print("Configuration problem: %s" % e)
        raise SystemExit(2)
