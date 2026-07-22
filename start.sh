#!/usr/bin/env bash
# Family Accountability launcher: creates the venv on first run, then starts the app.
#   ./start.sh          serve your real instance on localhost
#   ./start.sh --lan    also bind the local network (no login — trusted networks only)
#   ./start.sh --demo   seed an isolated ./demo with synthetic data and serve that
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/uvicorn ]; then
  echo "First run: creating Python environment (needs python3 >= 3.12)…"
  python3 -m venv .venv
  .venv/bin/pip install --disable-pip-version-check -r requirements.txt
fi

HOST=127.0.0.1
DEMO=0
for arg in "$@"; do
  case "$arg" in
    --lan)  HOST=0.0.0.0 ;;
    --demo) DEMO=1 ;;
    *) echo "Unknown option: $arg (use --lan and/or --demo)" >&2; exit 2 ;;
  esac
done

if [ "$DEMO" = "1" ]; then
  export FA_ROOT="$PWD/demo"
  echo "Seeding isolated demo data in ./demo (your real data is never touched)…"
  .venv/bin/python scripts/seed_demo.py
  echo "Serving the DEMO instance (Alex & Sam · 5 years of synthetic data)."
fi

echo "Open http://localhost:8765 in your browser (Ctrl+C stops the app)."
[ "$HOST" = "127.0.0.1" ] && echo "To use it from another device on your home network, run: ./start.sh --lan"
exec .venv/bin/uvicorn app.server:app --host "$HOST" --port 8765
