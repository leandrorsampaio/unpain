# Family Accountability — agent guide

**Read [PROJECT.md](PROJECT.md) first** — it is the project entry point (users, goals,
domain decisions, current state, working agreements). This file covers the technical layer.

Household expense accountability for a two-person household (Germany, EUR/BRL/USD). Full design
in [PLAN.md](PLAN.md). Everything user-facing is in **English**.

## Architecture in one paragraph

`inbox/` files → `pipeline/ingest.py` (format detection via `pipeline/formats/*.json`, content-hash
dedupe, ECB FX conversion) → JSONL store in `data/<year>/transactions/` → `pipeline/transfers.py`
marks internal transfers (configured markers / conservative pair-match) → categorization is **derived on read**
in `pipeline/rules_engine.py`: decision (`data/<year>/decisions.json`) > merchant rule
(`rules/merchant-rules.json`) > needs_review. All math (`pipeline/settle.py`) recomputes from the
effective view; nothing derived is stored. UI: FastAPI (`app/server.py`) + vanilla JS
(`app/static/`), **Material Design 3**: vendored `@material/web` components + generated M3 tokens
(light+dark) — rebuild with `cd app/vendor-build && node scripts/build-theme.mjs` (seed color there).
Chart colors are CVD-validated per mode: light #005bc0/#e37400, dark #3987e5/#c98500 (`--chart-1/2`
in app.css). Fully offline: fonts/components vendored in `app/static/vendor/`.
**i18n**: natural-key translation via the global `T('English source', {vars})` (engine in
`app/static/i18n.js`; German map in `app/static/i18n/de.js`, which self-registers). English needs no
dictionary — a missing key falls back to the English source. UI language lives in `config.json`
(`"language"`, default `en`), is exposed by `/api/meta`, and is chosen in the Settings tab (mirrored to
`localStorage` for flash-free boot). Add a language = ship `i18n/<code>.js` + one line in `I18N.names` +
add the code to `SUPPORTED_LANGUAGES` in `server.py`. Static chrome (index.html) uses `data-i18n[-title|-label|-aria]`
attributes, retranslated by `translateChrome()`.
**Settings** is a left-rail of sub-pages (`SETTINGS_AREAS`: Household · Accounts · Preferences · Accounting ·
Data) rendered by `fillSettingsArea()`; state lives in `state.settingsArea`. Accounts moved here from a
top-level tab; backup download + data health check moved here from the app bar (Data area). The config
knobs **autosave** — every field is debounced ~400ms into `commitSettingsSave()` (optimistic, latest-wins
via `state.settingsSaveSeq`, plain `fetch` so failures land in the `#save-status` flag instead of a dialog),
built from the `state.settingsCfg` source-of-truth. There is no Save button. Account row CRUD keeps its own
add/save/delete buttons.

## Commands

- Run server: `.venv/bin/uvicorn app.server:app --port 8765`
- CLI: `.venv/bin/python -m pipeline.cli ingest|status|summary|settle|tax|fx-update`
- Read-only data audit: `.venv/bin/python -m pipeline.cli doctor [year]`
- Tests: `./run-tests.sh` (all) / `./run-tests.sh --fast` (skip browser smoke only).
  The repository pre-commit hook chooses the full suite for UI changes and fast otherwise.
  GitHub Actions CI enforces the complete test suite, including the `tests/test_oracle.py` math invariant checker over 5 years of synthetic fixture data, on all branches.

## Invariants — do not break

- LLMs never write to `data/` directly; LLM extraction goes through `inbox/` CSVs and the
  reconciliation gate (see `skills/extract-statement/`).
- Money comparisons use `util.cents()`, never float equality. German CSVs use comma decimals.
- Transaction ids are content hashes + occurrence suffix; re-ingesting a file must be a no-op.
- `sharing: out-of-scope` and `kind: internal-transfer` are invisible to ALL math.
- Settlement ratio: only subcategories with `ratio_income: true` (currently salary), owner
  `couple` counts half to each. Monthly = estimate; annual = binding.
- Closed months (`data/<year>/months.json`) reject decisions (HTTP 409).
- Categories: never delete, set `archived: true`. Slugs are stable ids.
- Rule scope: `family` (default) applies everywhere; `<person>` scope applies only to that
  person's accounts and beats family rules. Couple-owned accounts match family rules only.
- Every user-facing string goes through `T(...)`; every `T()` key must exist in `i18n/de.js`
  (`tests/test_i18n.js` fails the build otherwise). Machine values stay English: URLs, category/account
  slugs, `person1`/`person2`, sharing/kind enums. Interpolated user text must still be `esc()`-d.

## Domain decisions (agreed with the users, don't re-litigate)

- Split is income-proportional, computed yearly from salary only — never stored per transaction.
- `year_cost: true` = excluded from monthly picture, included annually (e.g. an e-bike).
- Reimbursements are booked to the expense category they offset (signed sums handle it).
- Amazon/PayPal always go to the review queue (multi-purpose), splittable in the UI.
