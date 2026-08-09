# Implementation plan: FX audit, deterministic anomalies, and “What changed?”

Status: implementation handoff — no feature code has been written from this document.  
Prepared: 2026-08-08  
Scope: FEAT-08, FEAT-09, and FEAT-11 from `UnPAIN_Feature_Suggestions.md`.

## 1. Executive summary

These features have a common purpose: make financial results easier to trust and explain without
allowing heuristics to change the ledger.

| Feature | User question | Recommended first version | Risk |
|---|---|---|---|
| FEAT-08 — FX audit | “How did this foreign amount become this EUR amount?” | Read-only year audit and per-transaction explanation | Low |
| FEAT-09 — deterministic anomalies | “Which transactions deserve a second look?” | High-confidence, dismissible review suggestions | Medium, mainly false positives |
| FEAT-11 — What changed? | “Why are today’s figures different from the figures I reviewed?” | Compare semantic checkpoints, not a full mutation journal | Medium |

Recommended implementation order:

1. FEAT-08, because it is read-only and has the smallest failure surface.
2. FEAT-09, initially with conservative checks and no automatic ledger changes.
3. FEAT-11, after agreeing on checkpoint semantics and retention.

All three must preserve the project’s central rules:

- calculations remain deterministic;
- derived figures are recomputed, not used as stored truth;
- suggestions never edit transactions automatically;
- money comparisons use integer cents;
- user text is escaped in every HTML sink;
- UI is Material Design 3, vanilla JavaScript, and uses the existing shared modal/components;
- all English strings are added to `app/static/i18n/de.js`;
- the complete suite and all 12 pages must remain green.

## 2. Shared product principles

### 2.1 Evidence, warnings, and source data are different things

The canonical ledger remains `data/<year>/transactions/*.jsonl` plus decisions and rules. FX audit
results, anomaly suggestions, and change checkpoints are evidence about that ledger. They must never
become inputs to settlement, dashboards, tax, net worth, or recurring calculations.

### 2.2 No automatic correction

- FX audit may report a discrepancy but must not rewrite `amount_eur` or `fx_rate`.
- Anomaly checks may link to Review or Transactions but must not categorize, merge, delete, or mark a
  transaction as a transfer.
- “What changed?” may explain and link to existing actions but must not accept a close or restore an
  old value automatically.

### 2.3 Stable, reproducible outputs

Given the same canonical files, configuration, FX cache, baseline, and `as_of` date, every endpoint
must return the same output in the same order. Time-sensitive checks must accept an explicit `as_of`
internally so tests do not depend on the wall clock.

### 2.4 One reusable presentation pattern

Add shared UI helpers for an audit status chip and a transaction link once, near the existing shared
components in `app/static/app.js`. Do not create three separate badge/table/modal implementations.
Suggested helpers:

- `auditStatusChip(status, label)`
- `transactionLink(id, label, year)`
- `auditEmptyState(icon, title, explanation)`
- use the existing `openModal(...)`, `statTile(...)`, `segControl(...)`, `esc(...)`, and `fmt(...)`.

## 3. FEAT-08 — FX audit view

### 3.1 What the feature is

A read-only explanation of every non-EUR conversion. It must show both what UnPAIN stored and what
the ECB cache says should have been used. This is an audit of UnPAIN’s ECB-based bookkeeping value,
not a claim that the user’s bank converted cash at the ECB rate.

For each foreign-currency transaction, show:

- transaction date and merchant;
- original signed amount and currency;
- requested rate date (the transaction date);
- actual ECB publication date used after weekend/holiday fallback;
- stored ECB rate (`1 EUR = rate × currency`);
- current cached ECB rate for the same publication date;
- exact unrounded EUR quotient;
- expected rounded EUR cents;
- stored EUR amount;
- rounding delta, expressed in fractions of a cent;
- status: `ok`, `rate-mismatch`, `amount-mismatch`, `missing-rate`, or `legacy-date-derived`;
- source statement and transaction id.

At year level, group by currency and show:

- transaction count;
- signed original-currency total;
- signed stored EUR total in integer cents;
- expected EUR total in integer cents;
- total rounding delta;
- number of discrepancies;
- oldest/newest transaction date;
- oldest/newest ECB publication date used.

Also show cache information:

- newest date present in the local ECB cache;
- cache file modification time;
- whether every audited transaction has an applicable cached rate;
- a freshness label that does not imply historical conversions become invalid merely because the
  cache has not been updated recently.

### 3.2 Expected result

A stakeholder can choose any foreign transaction and reproduce the stored EUR amount to the cent.
The year summary reconciles exactly to the same stored EUR cents used by dashboards and settlement.
Historical data without a stored fallback date remains explainable: the endpoint derives the rate
date from the read-only cache and labels it as derived rather than pretending it was stored at import.

The feature must make these distinctions explicit:

- `transaction date` is not always `ECB publication date`;
- `ECB bookkeeping conversion` is not necessarily `bank/card conversion`;
- a stale cache is an update warning, not proof that old stored transactions are wrong;
- sub-cent rounding differences are expected; cent-level disagreement is not.

### 3.3 Backend design

#### New file: `pipeline/fx_audit.py`

Implement:

- `audit_year(year, scope="all") -> dict`
- `audit_transaction(txn, cached_rates, cache_meta) -> dict`
- `summarize_by_currency(items) -> list[dict]`
- `_exact_conversion(amount_original, rate) -> Decimal`
- `_status_for(stored_rate, cached_rate, stored_eur_cents, expected_eur_cents) -> str`

Use `Decimal(str(value))` only for the independent audit explanation and sub-cent delta. Do not turn
this into a Decimal migration of the application. Use `cents()` for every cent equality assertion.

`audit_year` should read `store.effective_year(year)` so account corrections and user-visible text
match Transactions, but conversion facts must come from the canonical transaction fields. Splits do
not create additional FX conversions: the bank converted the parent transaction once.

#### Modify: `pipeline/fx.py`

Add a read-only lookup that returns provenance instead of only the numeric rate:

```python
rate_details(currency, requested_date, *, allow_download=False) -> {
    "currency": "BRL",
    "requested_date": "2026-08-08",
    "rate_date": "2026-08-07",
    "rate": 6.1234,
    "fallback_days": 1,
}
```

Requirements:

- `allow_download=False` must never download or mutate the cache.
- Keep `rate(currency, day)` and `to_eur(...)` backward compatible.
- Refactor the fallback walk into one implementation used by `rate()` and `rate_details()`.
- Add `cache_info()` that reads metadata without downloading.
- Do not use private `_rates` directly from `fx_audit.py`.

For new imports, extend `fx.to_eur(...)` or add `to_eur_details(...)` so ingestion can store
`fx_rate_date`. Existing rows do not need a migration; the audit view can derive the date and mark it
`legacy-date-derived`.

#### Modify: `pipeline/ingest.py`

In `_ingest_file(...)`, `ingest_upload(...)`, and cash regeneration, store:

```json
{
  "fx_rate": 6.1234,
  "fx_rate_date": "2026-08-07",
  "fx_rate_source": "ECB"
}
```

For EUR, keep `fx_rate`, `fx_rate_date`, and `fx_rate_source` null/absent. Reuse one conversion helper
so the three ingestion paths cannot drift.

#### Modify: `app/server.py`

Add:

```text
GET /api/fx-audit?year=2026&scope=all
```

Validate scope through `_check_scope(...)`. The response should contain:

```json
{
  "year": 2026,
  "scope": "all",
  "cache": {
    "present": true,
    "newest_rate_date": "2026-08-07",
    "modified_at": "...",
    "coverage_complete": true
  },
  "summary": {
    "transactions": 12,
    "currencies": 2,
    "stored_eur_cents": -123456,
    "expected_eur_cents": -123456,
    "discrepancies": 0
  },
  "by_currency": [],
  "items": []
}
```

The GET endpoint must remain read-only and must work offline. Missing cache data should produce item
statuses, not a 500 response and not a background download.

### 3.4 UI design

#### Modify: `app/static/app.js`

Place an `FX audit` outlined button in the Transactions toolbar, visible when the selected year has
foreign rows. Do not add a new top-level page for this first version.

Add:

- `openFxAudit()` — fetches `/api/fx-audit`, opens the shared modal;
- `fxAuditHtml(result)` — summary tiles, cache note, currency reconciliation, item table;
- `fxAuditRow(item)` — escaped merchant/source and exact conversion explanation;
- status/currency filters using existing segmented/select components;
- transaction navigation that closes the modal, opens Transactions, and filters/scrolls by id.

Update the existing `fxBadge(t)` tooltip to use `fx_rate_date` when present. Its current use of
`t.date` is wrong for weekends and ECB holidays.

Use a wide shared modal with a bounded scrolling body. On narrow screens, render audit rows as
stacked key/value blocks rather than a table that forces horizontal scrolling.

#### Modify: `app/static/i18n/de.js`

Add every new English key. Machine statuses remain English internally; translate only their labels.

#### Modify only if necessary: `app/static/app.css`

Use existing M3 tokens and semantic typography. Add classes only for layout; do not hardcode colors,
font sizes, or a second chip design.

### 3.5 Tests

#### New: `tests/test_fx_audit.py`

Cover at minimum:

- weekday rate and exact conversion;
- Saturday/Sunday and ECB holiday fallback date;
- stored rate matches cache;
- stored rate differs from cache;
- stored EUR differs by one cent;
- positive and negative amounts;
- very small amounts and half-cent boundaries;
- multiple currencies reconciled separately;
- legacy transaction without `fx_rate_date`;
- missing currency in cache produces `missing-rate`, not a crash;
- missing cache causes no file or network write;
- integer-cent summary equals the sum of item cents;
- deterministic ordering and read-only behavior.

Extend `tests/test_fx.py`, `tests/test_pipeline.py`, and `tests/test_oracle.py` to assert new imports
carry the correct fallback date while all existing financial totals remain unchanged.

Extend UI guards/browser smoke to open the audit and verify one displayed conversion against its API.

### 3.6 Acceptance criteria

- Every non-EUR transaction in the selected year appears exactly once.
- `sum(item.stored_eur_cents)` equals the API summary and the relevant transaction export values.
- Weekend/holiday transactions name the actual ECB publication date.
- The endpoint never downloads rates.
- No button automatically changes a rate or amount.
- Existing rows remain supported without migration.

## 4. FEAT-09 — deterministic anomaly checks

### 4.1 What the feature is

A conservative review assistant using explicit statistical/rule-based checks. It should identify
transactions worth human attention while keeping them in the ledger exactly as they are.

This is not a fraud detector and should not claim to know that a transaction is wrong. User-facing
language must say `Possible duplicate`, `Unusual amount`, or `Expected recurring charge not seen`,
never `Duplicate`, `Fraud`, or `Missing payment` as a fact.

### 4.2 Expected result

The Review page gains a separate `Suggestions` section. A user can:

- understand exactly why each suggestion fired;
- open the relevant transaction(s);
- dismiss a suggestion without changing financial data;
- see the same result after a restart;
- rerun checks deterministically;
- distinguish high-confidence suggestions from informational ones.

Anomaly counts must not be mixed silently with `needs_review` transaction counts or pending transfer
counts. If the Review badge includes them, the page must show the three counts separately.

### 4.3 Detection model and defaults

Run over canonical/effective data but never mutate it. Every result carries:

```json
{
  "id": "stable-hash",
  "check": "near-duplicate",
  "severity": "suggestion",
  "confidence": "high",
  "year": 2026,
  "transaction_ids": ["...", "..."],
  "message": "...",
  "evidence": {},
  "math_impact_cents": -1299,
  "dismissed": false
}
```

Stable id: SHA-256 of check name, sorted transaction ids, expected period when relevant, and detector
version. Do not include display text, category names, or timestamps in the id.

#### Check A — exact duplicate charge

Candidate requirements:

- negative amount;
- same effective account;
- same date;
- same original currency and original cents;
- normalized merchant/purpose key equal;
- different transaction ids or source uploads.

Do not collapse the records. Include source files and upload ids as evidence. If both rows came from
overlapping exports and canonical id dedupe already proved them identical, they should not exist
twice; this check targets duplicates that differ enough to evade the import identity.

#### Check B — near duplicate charge

Recommended conservative default:

- negative amount;
- same account and merchant key;
- same original-currency cents;
- dates no more than 3 days apart;
- not an internal transfer;
- not already in the same exact-duplicate group.

Set confidence `high` for same day, `medium` for 1–3 days. A dismissed pair stays dismissed unless
one of its transaction ids or relevant fields changes.

#### Check C — merchant amount spike

Use absolute EUR cents grouped by normalized merchant key and optionally account. Recommended:

- at least 6 historical charges before judging a new one;
- median and median absolute deviation (MAD), never mean/standard deviation;
- flag when `abs(value - median) > max(4 × MAD, 50% of median, €20)`;
- if MAD is zero, the percentage and €20 floors still apply;
- compare charges with charges; do not let refunds/income set the baseline;
- do not evaluate a merchant whose normalized key is empty.

Return median, MAD, sample size, normal range, and observed amount in evidence. Use integer cents for
the detector. Statistics may calculate medians over integers.

#### Check D — recurring charge missing

Reuse recurring detection rather than implementing a second merchant/cadence system. First extract
`recurring._merchant_key` into a public shared helper such as `merchant_key(txn)` and use it from both
modules.

Recommended first-version rule for monthly charges:

- recurring detector has at least 4 historical months;
- the expected month is complete;
- statement coverage confirms the relevant account has data through that month;
- no matching merchant charge exists by the expected window plus a 7-day grace period;
- yearly and quarterly items may be added later unless their expected-month logic is tested well.

Do not report a missing charge merely because the current month is partial or its statement has not
been imported. The evidence should name last seen date, normal cadence, median amount, expected
window, and coverage used.

#### Check E — unexpected sign

Flag when a merchant has at least 5 prior occurrences all with one sign and a new occurrence has the
opposite sign. Refunds are often legitimate, so default to `medium` confidence and wording such as
`This merchant is normally an expense; this entry is positive.`

#### Check F — new account/currency combination

Recommended definition:

- an account has at least 20 prior transactions;
- all prior transactions used the declared account currency;
- a new transaction uses another currency for the first time.

This overlaps with the doctor’s account-currency check but adds a transaction-level review link.
Do not flag ordinary multi-currency cards repeatedly: after the first accepted/dismissed occurrence,
the combination is no longer new.

#### Check G — date outside declared statement period

Use the upload registry (`data/uploads.json`) and `transaction.source.upload`. Only run when the
upload has a machine-readable declared period. Do not infer a statement period from min/max rows and
then accuse those same rows of being outside it.

Extend period metadata to normalized `{start, end, source}` where extractors/formats provide it. A
year-only report is too broad for a monthly out-of-period check but can still reject another year.

### 4.4 Backend design

#### New: `pipeline/anomalies.py`

Suggested functions:

- `scan(year, scope="all", as_of=None, include_dismissed=False) -> dict`
- `exact_duplicates(ctx) -> list`
- `near_duplicates(ctx) -> list`
- `amount_spikes(ctx) -> list`
- `missing_recurring(ctx) -> list`
- `unexpected_signs(ctx) -> list`
- `new_account_currency(ctx) -> list`
- `outside_statement_period(ctx) -> list`
- `stable_id(check, ids, discriminator="") -> str`
- `load_dismissals()` / `dismiss(anomaly_id, fingerprint)`

Build one context containing raw rows, effective rows by id, accounts, uploads, coverage, and
recurring facts. Do not reread the store once per check.

#### New persisted evidence: `data/anomaly-dismissals.json`

Suggested shape:

```json
{
  "version": 1,
  "dismissed": {
    "anomaly-id": {
      "fingerprint": "hash-of-relevant-evidence",
      "dismissed_at": "UTC ISO timestamp"
    }
  }
}
```

A dismissal suppresses only the same evidence fingerprint. If amount/date/account/participants
change, the finding may appear again under a new fingerprint. Add a doctor info finding for orphaned
or malformed dismissal records only if it is genuinely actionable; otherwise prune them lazily.

#### Modify: `pipeline/recurring.py`

- make merchant normalization a reusable public helper;
- expose enough cadence evidence to judge a missing month without scraping display output;
- preserve all existing recurring behavior and overrides.

#### Modify: `app/server.py`

Add:

```text
GET  /api/anomalies?year=2026&scope=all&include_dismissed=false
POST /api/anomaly-dismiss {"id": "...", "fingerprint": "..."}
```

Reject dismissal of an id/fingerprint that the current scan does not produce. This prevents a typo
from creating meaningless permanent state.

Do not insert every heuristic suggestion into `doctor.run()`. Doctor findings are integrity facts;
anomalies are review suggestions. Doctor may return one informational summary such as `7 undismissed
anomaly suggestions`, but only after the separate endpoint is stable.

### 4.5 UI design

#### Modify: `app/static/app.js`

Extend `renderReview(...)` to fetch `/api/anomalies` alongside review transactions and transfers.
Render `anomalyReviewSection(data)` above ordinary categorization groups, after pending transfers.

Each card should include:

- clear suggestion label and confidence;
- evidence in plain language;
- affected amount and potential math impact;
- affected transactions, each linked by id;
- `Review transaction(s)` and `Dismiss suggestion` actions.

Use one shared card renderer for all anomaly types. Type-specific evidence belongs in small helper
formatters, not seven copied card templates.

Do not add automatic `Delete duplicate` in version one. Deletion currently happens by deleting a
tracked upload or editing source data; inventing per-transaction deletion is a separate financial
workflow with closed-period and audit implications.

### 4.6 Tests

#### New: `tests/test_anomalies.py`

Use named positive and negative scenarios for every detector. Important negative cases:

- two legitimate same-day equal purchases remain suggestions, never auto-deleted;
- same amount at different merchants is not a near duplicate;
- refund does not contaminate the expense median;
- fewer than the minimum history count produces no spike;
- partial current month produces no missing-recurring warning;
- a statement coverage gap suppresses missing-recurring;
- multi-currency card does not warn on every later transaction;
- no declared statement period means no outside-period accusation;
- out-of-scope/internal-transfer rows do not create misleading spending anomalies;
- split parents are not counted twice;
- dismissal survives restart and is invalidated by changed evidence;
- ids and ordering remain deterministic;
- scan is read-only.

Extend:

- `tests/test_recurring.py` for shared merchant keys and missing-payment evidence;
- `tests/test_http_contract.py` for malformed dismissal bodies and status codes;
- `tests/test_closed_period.py` to classify dismissal as non-financial metadata;
- UI/i18n/smoke tests for escaping merchant text and opening/dismissing a suggestion.

### 4.7 Acceptance criteria

- Every suggestion explains its evidence and links to actual rows.
- No check changes a transaction, decision, rule, transfer, close, or total.
- Missing-recurring never fires without coverage evidence.
- Dismissals are stable but reappear if relevant evidence changes.
- The same fixture and `as_of` produce byte-for-byte equivalent JSON ordering.
- False-positive controls and minimum samples are documented in code and tests.

## 5. FEAT-11 — “What changed?” comparison

### 5.1 What the feature is

A semantic comparison between the current effective financial view and a previously recorded
checkpoint. It explains changes in terms a user understands rather than only saying a digest moved.

Recommended first version: checkpoints for close, successful import, and successful backup. Do not
build a full chronological mutation journal yet.

The distinction matters:

- **Checkpoint comparison** answers: “What is different now versus that reviewed moment?”
- **Mutation journal** answers: “Which actions happened, in which order, and possibly by whom?”

The first is enough for the requested feature and fits the plain-file, local architecture. The
second requires instrumenting nearly every write endpoint, event versioning, transactionality,
retention, restore semantics, and identity/session attribution. It is a separate project.

### 5.2 Expected result

From the Dashboard, a user selects a baseline:

- latest close for a month;
- annual close;
- latest successful import affecting the year;
- latest successful backup containing the year.

The UI then reports:

- transactions added and removed;
- imports/sources added or removed;
- amount/date/account edits;
- category, sharing, income-owner, tax, year-cost, and split changes;
- whether a rule or manual decision controlled the old/new classification;
- internal-transfer pair/status changes;
- income, expenses, savings, category totals, shared expenses, paid/fair shares, balances, and final
  settlement before and after;
- changes whose totals net to zero but whose underlying lines changed.

The comparison is evidence only. Current calculations continue to use the current effective view.

### 5.3 Checkpoint data model

#### New: `data/<year>/audit-checkpoints.json`

Keep only the latest checkpoint for each rolling kind plus close checkpoints referenced by active
closes. Suggested shape:

```json
{
  "version": 1,
  "checkpoints": {
    "last-import": {
      "id": "import:s_123",
      "kind": "import",
      "created_at": "UTC ISO timestamp",
      "label": "statement.pdf",
      "period": null,
      "metadata": {"upload_id": "s_123", "source_stem": "account__s_123"},
      "snapshot_version": 1,
      "snapshot": {}
    },
    "last-backup": {},
    "close:2026-07": {},
    "close:annual": {}
  }
}
```

Do not store every full effective transaction object. Store the smallest semantic line record that
can explain a result:

```json
{
  "line_key": "transaction-id:part-index",
  "transaction_id": "...",
  "part_index": 0,
  "date": "2026-07-10",
  "account": "bank1-person1",
  "amount_cents": -1299,
  "category": "recreation/streaming",
  "sharing": "shared",
  "income_owner": null,
  "year_cost": false,
  "tax_bucket": null,
  "kind": "normal",
  "transfer_partner": null,
  "counterparty": "Example Merchant",
  "source_file": "statement.csv",
  "source_upload": "s_123",
  "matched_rule": "rule-id-or-null",
  "decision_fields": ["category", "sharing"]
}
```

`decision_fields` is field provenance, not the full decision. It lets the diff say `manual category
changed` versus `rule result changed`. Preserve merchant/source labels for removed rows, because the
current store can no longer supply them.

Also store the recognizable derived snapshot needed for before/after reporting:

- month/year summary;
- settlement fields from `closings.SETTLEMENT_FIELDS`;
- category totals in integer cents;
- semantic digest and snapshot version.

### 5.4 Backend design

#### New: `pipeline/audit.py`

Suggested functions:

- `semantic_snapshot(year, month=None) -> dict`
- `semantic_lines(year, month=None) -> dict[line_key, dict]`
- `checkpoint(year, kind, *, period=None, label=None, metadata=None) -> dict`
- `load_checkpoints(year) -> dict`
- `available_baselines(year) -> list`
- `compare(year, checkpoint_id) -> dict`
- `diff_lines(before, after) -> dict`
- `diff_figures(before, after) -> dict`
- `prune(year) -> None`

Build semantic lines with `settle.money_lines(...)` and `settle.part_view(...)`. This is mandatory:
reimplementing split inheritance would make “What changed?” disagree with the dashboard.

Refactor `closings._line_digest(...)` to hash the canonical semantic-line representation from
`pipeline.audit`, or move the shared representation to a smaller neutral module if importing audit
from closings would create a cycle. There must be one definition of the watched money line.

`diff_lines` should classify:

- `added`: key only in current;
- `removed`: key only in baseline;
- `amount_or_date_changed`;
- `account_or_owner_changed`;
- `classification_changed` (category/sharing/year-cost/tax/income owner/split);
- `transfer_changed`;
- `source_changed`;
- `presentation_only` (merchant text changed while financial fields did not).

One transaction may have multiple field changes but should appear as one item with a list of changed
fields. Split creation/removal needs a parent-level summary so two new parts are not presented as two
unrelated imported transactions.

`diff_figures` must use integer cents and explicitly report both old and new values plus delta. It
must include settlement, not only income/expenses.

#### Modify: `pipeline/closings.py`

- when `record(...)` succeeds, record/update `close:YYYY-MM` checkpoint;
- when `record_year(...)` succeeds, record/update `close:annual` checkpoint;
- when a close is dropped/reopened, remove its corresponding comparison checkpoint;
- when `rebaseline(...)`/closing acceptance adopts current figures, update the same checkpoint;
- bump checkpoint snapshot versions independently from `DIGEST_VERSION`;
- keep current closing behavior and drift detection backward compatible.

Checkpoint failure must fail the close rather than leaving `closed` with no explainable baseline.
Tests must assert atomic behavior across `months.json`, `closings.json`, and checkpoints.

#### Modify: `app/server.py` import flow

After a staged import, transfer detection, anchors, upload metadata, and staging metadata have all
successfully published, record `last-import` for every affected year. Include upload id, source stem,
file hash, account, and original filename.

The checkpoint must be part of the existing import transaction. Its file lives under the affected
year directory, so the scoped year rollback already covers it. If checkpoint publication fails, the
import must roll back and may truthfully say no data was imported.

CLI inbox ingestion also needs an import checkpoint. Since one CLI run may process several files,
prefer one checkpoint per successful file or one run checkpoint with a list of sources; choose one
and test it consistently. Recommended: one checkpoint after the complete successful CLI run, labelled
with all processed source names, because transfer detection finishes after the file loop.

#### Modify: `app/server.py` backup flow

After a backup ZIP is successfully written:

- calculate its SHA-256;
- record/update `last-backup` for every year included in the selected backup parts;
- include backup filename, hash, selected parts, and timestamp.

The backup contains the state immediately before its checkpoint metadata is written; that is fine,
but document it and store the ZIP hash so the comparison refers to an actual artifact. A failed ZIP
must not create a successful-backup checkpoint.

Because `/api/backup` is currently GET but writes a backup file, either:

- change checkpoint-producing backup to POST followed by a download token, or
- explicitly acquire the shared mutation lock inside the handler.

The first is semantically cleaner but affects the UI. The second is the lower-change option.

#### Modify: `app/server.py` API

Add:

```text
GET /api/changes/baselines?year=2026
GET /api/changes?year=2026&baseline=<checkpoint-id>
```

Do not accept arbitrary filesystem paths or client-supplied snapshot data. Baseline ids must resolve
through `audit-checkpoints.json` for the selected year.

Suggested comparison response:

```json
{
  "year": 2026,
  "baseline": {"id": "...", "kind": "close", "created_at": "...", "label": "July close"},
  "scope": {"period": "2026-07"},
  "summary": {
    "added": 2,
    "removed": 0,
    "changed": 3,
    "financial_delta_cents": -2500,
    "settlement_changed": true
  },
  "figure_changes": [],
  "source_changes": [],
  "line_changes": []
}
```

### 5.5 UI design

#### Modify: `app/static/app.js`

Add a Dashboard card or outlined action labelled `What changed?`. Do not add a top-level navigation
page initially.

Functions:

- `whatChangedCardHtml()` — small latest-baseline summary/entry point;
- `fillWhatChangedCard(year)` — fetch available baselines lazily;
- `openWhatChanged()` — shared modal;
- `whatChangedHtml(result)` — before/after tiles and grouped details;
- `changeRowHtml(change)` — escaped labels, old/new values, transaction link.

The baseline selector must show type, period, timestamp, and label. A monthly close automatically
limits comparison to that month; annual/import/backup compares the year snapshot stored at that
checkpoint.

Group results in this order:

1. effect on totals and settlement;
2. added/removed sources and transactions;
3. classification/ownership changes;
4. transfer changes;
5. presentation-only changes.

If nothing changed, say `No semantic or financial changes since …`; do not merely show empty cards.
If no baseline exists, explain how to create one (close a period, process an import, or create a
backup).

### 5.6 Tests

#### New: `tests/test_audit_changes.py`

Cover:

- no change after checkpoint;
- imported row added;
- upload deleted/source removed;
- raw amount/date/account edit;
- manual decision changes category, sharing, income owner, and year cost;
- rule changes classification with no decision;
- split created, modified, and removed;
- transfer pair created/released;
- merchant rename with unchanged totals;
- two compensating changes with unchanged totals but changed digest;
- settlement movement with unchanged income/expense totals;
- monthly close compares only its month;
- annual baseline compares the whole year;
- acceptance/rebaseline updates the checkpoint;
- reopening removes the close baseline;
- old checkpoint version reports reduced coverage, never false drift;
- checkpoint write failure makes close/import fail atomically;
- backup checkpoint exists only after a successful ZIP and carries its hash;
- retention keeps only the configured rolling checkpoints;
- current data and checkpoints survive backup/restore according to selected parts;
- deterministic ordering and escaped deleted-row merchant text.

Extend:

- `tests/test_pipeline.py` for closing integration;
- `tests/test_ingest_pdf_flow.py` for checkpoint rollback;
- `tests/test_restore.py` and backup tests for checkpoint semantics;
- `tests/test_closed_period.py` to account for the new endpoints;
- `tests/test_http_contract.py` for unknown/malformed baseline ids;
- UI smoke/i18n tests for the empty, unchanged, and changed states.

### 5.7 Acceptance criteria

- A closed-period change names the affected transactions and fields, not only a digest.
- Before/after totals rebuild from their corresponding semantic snapshots in integer cents.
- Rules, decisions, transfers, splits, imports, and deletions are distinguishable.
- A deleted row remains explainable from baseline evidence.
- Checkpoints never influence current calculations.
- Import/close checkpoint failure is atomic.
- Storage has explicit versioning and bounded retention.

## 6. Cross-feature implementation sequence

### Phase 0 — contracts and shared helpers

1. Write API response examples as test fixtures.
2. Extract shared merchant normalization from recurring.
3. Add shared audit UI status/link helpers.
4. Decide the open product questions in section 8.

### Phase 1 — FEAT-08

1. Refactor FX lookup provenance without changing conversion results.
2. Store fallback rate date on new imports.
3. Implement read-only audit module/API.
4. Add Transactions modal and tests.
5. Verify oracle totals remain byte-for-byte equivalent in cents.

### Phase 2 — FEAT-09

1. Implement context and stable anomaly schema.
2. Ship exact/near duplicate and unexpected-sign checks first.
3. Add amount spike after false-positive fixtures are agreed.
4. Add missing recurring only with statement-coverage gating.
5. Add currency/period checks.
6. Add dismissals and Review UI.

Checks should be introduced one by one. A single giant detector commit makes it difficult to know
which heuristic caused noise.

### Phase 3 — FEAT-11

1. Define/version semantic line snapshots.
2. Refactor closing digest to reuse that representation.
3. Implement checkpoint store and diff engine.
4. Integrate close/accept/reopen.
5. Integrate staged and CLI imports.
6. Integrate backup.
7. Add Dashboard comparison UI.

## 7. Definition of done for the implementing LLM

- [ ] No existing test assertion was weakened or removed to make the feature pass.
- [ ] Anti-shrink counters only increased.
- [ ] New backend code has named deterministic unit scenarios and negative cases.
- [ ] Every cent comparison uses `cents()` or integer cents.
- [ ] No audit/anomaly/checkpoint value is consumed by financial calculations.
- [ ] No heuristic performs a write except explicit dismissal metadata.
- [ ] New endpoints are included in the closed-period write matrix.
- [ ] New user strings exist in German i18n.
- [ ] User-controlled merchant/source/category text passes through `esc()`.
- [ ] UI reuses `openModal` and existing shared components.
- [ ] No duplicate control/renderer is introduced in `app/static/`.
- [ ] `./run-tests.sh` passes.
- [ ] All 12 pages load in a real browser and screenshots are reviewed.
- [ ] `.venv/bin/python -m pipeline.cli doctor` is unchanged except for known findings.
- [ ] Personal data and generated audit evidence are not committed.
- [ ] `CHANGELOG.md` documents the feature.

## 8. Decisions that still need stakeholder judgment

The features are good ideas, but several product meanings cannot be inferred safely from code.
Recommended defaults are included so implementation can proceed if the stakeholder accepts them.

### Decision 1 — anomaly sensitivity

Question: should Review show only high-confidence anomalies, or also broader exploratory signals?

Recommendation: show high confidence by default, with a `Show lower-confidence suggestions` switch.
Financial software loses trust quickly if ordinary purchases constantly look suspicious.

### Decision 2 — what “missing recurring” means

Question: when is a charge late enough to call missing?

Recommendation: only after a complete, statement-covered month and a 7-day grace period; require at
least 4 historical months. Do not alert during a partial/uncovered month.

### Decision 3 — dismissal lifetime

Question: should dismissing a suggestion hide it forever?

Recommendation: dismiss the exact evidence fingerprint. It stays hidden while the underlying rows
are unchanged and reappears if relevant evidence changes. Provide no global permanent merchant
suppression initially; recurring already has its own explicit override system.

### Decision 4 — checkpoint comparison or full mutation history

Question: does “What changed?” mean difference from a known checkpoint, or a chronological audit log
of every action?

Recommendation: checkpoint comparison for version one. It answers the financial question with much
less storage and instrumentation. If attribution/order is later required, design a mutation journal
as a separate feature rather than quietly expanding this implementation.

### Decision 5 — which baseline opens by default

Recommendation:

- on a drifted closed month, open its close checkpoint;
- otherwise open the latest checkpoint affecting the selected year;
- let the user switch among close/import/backup baselines.

### Decision 6 — checkpoint retention

Recommendation: retain every active close checkpoint, plus only the latest import and latest backup
checkpoint per year. This bounds storage while satisfying “since last …”. If historical comparisons
are desired, specify a number or duration before implementation.

### Decision 7 — FX policy

Question: should a discrepancy automatically revalue historical transactions?

Recommendation: no. Audit stored ECB conversions and report discrepancies. Any revaluation feature
would move historical totals and closed settlements and needs its own explicit workflow.

### Decision 8 — Review badge semantics

Question: should anomaly suggestions increase the existing Review badge?

Recommendation: yes only for undismissed high-confidence suggestions, and show a breakdown on the
Review page: `classification`, `transfers`, and `suggestions`. Lower-confidence signals should not
make the main badge permanently noisy.

## 9. Recommended stakeholder answers

If the stakeholder wants the lowest-risk implementation without another design round, use:

1. high-confidence anomalies by default;
2. 4 historical months + complete covered month + 7-day grace for missing recurring;
3. evidence-fingerprint dismissals;
4. checkpoint comparison, not a mutation journal;
5. drifted close first, otherwise latest checkpoint;
6. active closes + latest import + latest backup retained;
7. FX is read-only audit, never automatic revaluation;
8. only high-confidence undismissed suggestions affect the Review badge.

Those choices preserve the project’s core character: deterministic, explainable, local, and strict
about financial evidence without creating a large new accounting platform inside the app.
