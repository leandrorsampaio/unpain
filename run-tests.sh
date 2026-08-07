#!/bin/bash
# Usage: ./run-tests.sh [--fast]   (--fast skips the browser smoke test)
set -e
PY=.venv/bin/python

echo "== static checks"
node --check app/static/app.js
node --check app/static/i18n.js
node --check app/static/i18n/de.js
node --check tests/ui_smoke.js
# checked-hash pycs: mtime+size validation cannot detect a same-second,
# same-size source edit (observed in practice); hash validation always can.
"$PY" -m compileall -q --invalidation-mode checked-hash pipeline app tests

# Ruff, the same check CI gates on. Without it here, a lint-only failure passes locally and
# turns the branch red after the push — which is exactly how E741 reached main.
# Not a hard dependency: a clone that has not installed it still runs the suite, and is told
# that CI will enforce what was skipped.
RUFF=""
if [ -x .venv/bin/ruff ]; then RUFF=".venv/bin/ruff"; elif command -v ruff >/dev/null 2>&1; then RUFF="ruff"; fi
if [ -n "$RUFF" ]; then
  "$RUFF" check .
else
  echo "   ruff not installed — skipping lint (CI still enforces it)."
  echo "   install with: .venv/bin/pip install 'ruff==0.15.22'"
fi

# Real-data tripwire: snapshot the real tree, verify it on exit (even after a
# failure), never mask the tests' own exit code, never leave files behind.
TRIP_SNAPSHOT=$(mktemp "${TMPDIR:-/tmp}/fa-tripwire.XXXXXX")
"$PY" tests/tripwire.py record "$TRIP_SNAPSHOT"
finish() {
  code=$?
  if ! "$PY" tests/tripwire.py verify "$TRIP_SNAPSHOT"; then code=1; fi
  rm -f "$TRIP_SNAPSHOT"
  exit $code
}
trap finish EXIT

for t in tests/test_pipeline.py tests/test_oracle.py tests/test_feedback.py \
         tests/test_config_validation.py tests/test_setup_wizard.py tests/test_settings.py \
         tests/test_restore.py tests/test_delete_year.py tests/test_cash_orphan.py \
         tests/test_networth.py tests/test_balances.py tests/test_asset_versions.py \
         tests/test_security.py \
         tests/test_ingest_pdf_flow.py tests/test_review_count.py \
         tests/test_transfer_review.py \
         tests/test_rules_engine.py tests/test_recurring.py tests/test_fx.py \
         tests/test_rule_reapply.py tests/test_trade_republic_extractor.py \
         tests/test_banco_rendimento_extractor.py tests/test_n26_extractor.py; do
  echo "== $t"; "$PY" "$t"
done
for t in tests/test_i18n.js tests/test_material_ui.js tests/test_typography_ui.js \
         tests/test_heavy_tabs_ui.js tests/test_year_selection.js; do
  echo "== $t"; node "$t"
done
if [ "$1" != "--fast" ]; then echo "== tests/ui_smoke.js"; node tests/ui_smoke.js; fi
echo "ALL TESTS PASSED"
