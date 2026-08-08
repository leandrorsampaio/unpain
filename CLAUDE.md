# UnPAIN — agent guide

**Read [PROJECT.md](PROJECT.md) first** — it is the project entry point (users, goals,
domain decisions, current state, working agreements). This file covers the technical layer.

Household expense accountability for a two-person household (Germany, EUR/BRL/USD). Full design
in [PLAN.md](PLAN.md). Everything user-facing is in **English**.

## Architecture in one paragraph

`inbox/` files → `pipeline/ingest.py` (format detection via `pipeline/formats/*.json`, content-hash
dedupe, ECB FX conversion) → JSONL store in `data/<year>/transactions/` → `pipeline/transfers.py`
marks internal transfers (configured markers / conservative pair-match, each pair recording its
`transfer_partner` and awaiting confirmation in the review queue) → categorization is **derived on read**
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
**Overview** (`pipeline/overview.py` → `/api/overview?scope=`) is the landing page (`state.tab`
defaults to `overview`; reachable from the home icon beside the year selector, not from the tab row,
because it is the one page the year selector does not apply to). It is `settle.year_summary` per year
added up — four figures, net worth over every month, income vs expenses by month, and where the money
goes (by main category, and by subcategory with the tail past the top 15 collapsed into one `Other`). Two windows meet on it and are not the same: the ledger starts at the first transaction, liquid
net worth at the first recorded balance, so each is labelled with its own span. Its charts are the
Dashboard's charts: `chartCard`, `moneyTiles`, `drawCategoryPie`, `drawIncomeExpenseLine` and
`netWorthCardHtml`/`fillNetWorthCard` are module-level in `app.js` and take their window as an
argument — a chart shown on both pages lives there or the two will drift apart.
**Settings** is a left-rail of sub-pages (`SETTINGS_AREAS`: Household · Accounts · Balances · Preferences ·
Accounting · Data · Security) rendered by `fillSettingsArea()`; state lives in `state.settingsArea`.
**Balances** (`pipeline/balances.py` → `/api/balances?year=`) is the year grid of recorded balances:
months down, accounts across, one cell per account-month holding either a **recorded** balance (an anchor
from a bank statement, carrying the reconciliation verdict of the span that ends at it) or a **derived**
one (what the ledger computes from the last recorded balance). The two must never blur — there is
deliberately no control that adopts a derived figure as recorded, because that would turn every cell green
while proving nothing. A cell owns a calendar month and shows an anchor dated inside it even when that
date is not the month end. The opening column is the previous December: a year opens where the last one
closed, one number, not two. Writing goes through the existing `/api/anchor` (`replace: true` to correct a
figure) and `/api/anchor-delete`, from a per-account-year dialog, because a statement covers one account
for one year. Accounts moved here from a
top-level tab; backup download + data health check moved here from the app bar (Data area). The config
knobs **autosave** — every field is debounced ~400ms into `commitSettingsSave()` (optimistic, latest-wins
via `state.settingsSaveSeq`, plain `fetch` so failures land in the `#save-status` flag instead of a dialog),
built from the `state.settingsCfg` source-of-truth. There is no Save button. Account row CRUD keeps its own
add/save/delete buttons.

## Commands

- Run server: `.venv/bin/uvicorn app.server:app --port 8765`
- CLI: `.venv/bin/python -m pipeline.cli ingest|status|summary|settle|tax|fx-update`
- Read-only data audit: `.venv/bin/python -m pipeline.cli doctor [year]`
- Watch already-closed months: `.venv/bin/python -m pipeline.cli close-baseline [year]`
- Tests: `./run-tests.sh` (all) / `./run-tests.sh --fast` (skip browser smoke only).
  The repository pre-commit hook chooses the full suite for UI changes and fast otherwise.
  GitHub Actions CI enforces the complete test suite, including the `tests/test_oracle.py` math invariant checker over 5 years of synthetic fixture data, on all branches.

## Invariants — do not break

- LLMs never write to `data/` directly; LLM extraction goes through `inbox/` CSVs and the
  reconciliation gate (see `skills/extract-statement/`).
- Money comparisons use `util.cents()`, never float equality. German CSVs use comma decimals.
- Transaction ids are content hashes + occurrence suffix; re-ingesting a file must be a no-op.
- `sharing: out-of-scope` and `kind: internal-transfer` are invisible to ALL math.
- Detected transfers are never silently final. Each pair stores `transfer_partner`, appears in the
  review queue via `/api/transfers` until a human confirms or rejects it, and both legs move together
  (`/api/transfer-confirm`). A mark whose partner is released is an orphan: `transfers.mark_internal`
  drops it and `doctor` reports `orphan-transfer-mark`. A verdict that changes no total is allowed in a
  closed month (confirming what is already excluded); one that puts money back is not.
- Any check on what the totals contain must read the **effective** view, not `decisions.json` — a
  merchant rule sets category and sharing too, and a decisions-only check is blind to it.
- Settlement ratio: only subcategories with `ratio_income: true` (currently salary), owner
  `couple` counts half to each. Monthly = estimate; annual = binding.
- Closed months (`data/<year>/months.json`) reject decisions (HTTP 409). That lock covers
  decisions and nothing else — a merchant rule, transfer detection and a re-ingest all rewrite a
  closed month freely, and a ratio override moves settlement without touching a transaction at all
  (that endpoint now respects the lock). So closing records the month's totals, **the settlement it
  produces, and a digest of every effective money line** (`pipeline/closings.py` →
  `data/<year>/closings.json`); closing a whole year also records the binding annual settlement under
  `annual`. Totals alone are not enough — sharing, payer, income owner and ratio all move who owes
  whom while income/expenses/count sit still. `doctor` reports `closed-month-drift` when any of it
  moves, `closed-month-stale-baseline` for snapshots predating the settlement fields.
  It reports rather than prevents: a change to a closed month is often a correction, and refusing
  those would preserve a known error. Reopening drops the baseline; `cli close-baseline` adopts
  current figures for months closed before this existed. Nothing ever *computes* from a snapshot.
  A snapshot that predates a field carries `digest_version`; a version mismatch means **reduced
  cover to upgrade, never drift** — widening the digest must not accuse every watched period at
  once. What such a snapshot *does* hold is still compared, so partial cover beats none.
  Drift is surfaced where a person looks: `/api/summary` carries it, the dashboard marks the tab
  red instead of locked and explains what moved, and `/api/closing-accept` adopts the new figures
  without reopening. Detecting drift and never mentioning it is most of the way to not detecting it.
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
