#!/bin/zsh

# Double-click launcher for macOS. It is safe to run when the app is already on:
# the existing process on the app's port is stopped before a fresh server starts.

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT=8765
URL="http://localhost:${PORT}"
LOG_FILE="${TMPDIR:-/tmp}/family-accountability-server.log"

cd "$PROJECT_DIR" || exit 1
: >"$LOG_FILE"
git config core.hooksPath .githooks >>"$LOG_FILE" 2>&1

if [[ ! -x ".venv/bin/uvicorn" ]]; then
  osascript -e 'display notification "Preparing the app for its first start…" with title "Family Accountability"'
  python3 -m venv .venv >>"$LOG_FILE" 2>&1
  .venv/bin/pip install --disable-pip-version-check -r requirements.txt >>"$LOG_FILE" 2>&1
  if [[ ! -x ".venv/bin/uvicorn" ]]; then
    osascript -e "display dialog \"The first-time setup failed. The log will open now:\n${LOG_FILE}\" with title \"Family Accountability\" buttons {\"OK\"} default button \"OK\" with icon stop"
    open -a TextEdit "$LOG_FILE"
    exit 1
  fi
fi

# Stop the previous app instance, including one started manually in Terminal.
if lsof -tiTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1; then
  lsof -tiTCP:${PORT} -sTCP:LISTEN 2>/dev/null | while IFS= read -r pid; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null
  done
  for _ in {1..20}; do
    lsof -tiTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 0.1
  done
fi

nohup env PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/fa-pycache" \
  "$PROJECT_DIR/.venv/bin/uvicorn" app.server:app --host 0.0.0.0 --port "$PORT" \
  >"$LOG_FILE" 2>&1 &

# Wait up to ten seconds so the browser never opens on a half-started server.
for _ in {1..40}; do
  if curl --silent --fail --output /dev/null "$URL"; then
    open "$URL"
    osascript -e 'display notification "The site is ready." with title "Family Accountability"'
    exit 0
  fi
  sleep 0.25
done

osascript -e "display dialog \"The server did not start. The log will open now:\n${LOG_FILE}\" with title \"Family Accountability\" buttons {\"OK\"} default button \"OK\" with icon stop"
open -a TextEdit "$LOG_FILE"
exit 1
