# Changelog

This file documents all notable changes to UnPAIN.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). UnPAIN aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **"What changed?" — why today's figures differ from the ones you reviewed** (FEAT-11). The
  Dashboard gains a card that compares the current year against a recorded moment: a month or annual
  close, the last import, or the last backup. It answers in transactions and fields — *"UNKNOWN
  VENDOR XYZ, category — → living-costs/rent, classified by rule X"* — rather than reporting that a
  digest moved.

  The cases it exists for are the ones a totals-only report cannot see: a rule reclassifying a row, a
  merchant renamed out of the rule that categorised it, two edits that cancel out, or a settlement
  that moves while income and expenses sit still. Changes are grouped by what they mean (amount or
  date, account or owner, classification, transfer, source, presentation-only), a split is summarised
  at its parent rather than as three unrelated lines, and a **removed** row is still described from
  the baseline — the store can no longer describe it, and "one line was removed" without saying which
  is not an explanation.

  It is a **checkpoint comparison, not a mutation journal** (the plan's Decision 4): it answers "what
  is different since that reviewed moment", which is the financial question, without instrumenting
  every write path with event ordering and attribution. Checkpoints are evidence only — a test
  tampers with one and asserts no total moves. Closing records one, reopening drops it, and retention
  keeps every active close plus the latest import and backup per year.

  `closings` now builds its drift digest from the same `pipeline/audit.py` line representation, so
  the alarm and the explanation of the alarm cannot disagree about what a watched money line is. That
  changes the digest bytes, so `DIGEST_VERSION` went to 3 — existing closed periods report *reduced
  coverage* and prompt `close-baseline`, never false drift.

- **Review suggestions — transactions worth a second look** (FEAT-09). The Review page gains a
  **Suggestions** section fed by seven deterministic checks: possible duplicate charges (same day, or
  one to three days apart), an amount unlike a merchant's usual ones, an expected recurring charge
  that has not arrived, a merchant billing in the unusual direction, an account's first transaction
  in a new currency, and a row dated outside its own statement's period.

  It is a review assistant, not a fraud detector, and says so: *Possible duplicate*, never
  *Duplicate*. **Nothing it produces can change a transaction** — no categorize, merge, delete or
  transfer mark, and no total, settlement or tax figure is computed from any of it. The only write is
  dismissing a suggestion, which is tied to a fingerprint of the evidence: dismiss it and it stays
  gone, until one of the facts behind it changes and the question is genuinely new.

  Every threshold is a deliberate floor, because a list that flags ordinary purchases gets ignored
  and then hides the real signals: six prior charges before any amount is judged, median and
  median-absolute-deviation rather than mean and standard deviation, and three separate floors (4×MAD,
  50% of the median, and €20) that must all be cleared. A missing recurring charge is only raised
  after four months of history, a complete month, statement coverage proving the month was actually
  imported, and a seven-day grace period. Out-of-scope lines, internal transfers and the uncounted
  part of a split produce no suggestions about spending. Only high-confidence suggestions reach the
  Review badge; the rest are behind a switch.

  Scans take an explicit `as_of`, so the same store gives byte-identical results and a test never
  depends on the clock. `recurring._merchant_key` became the public `recurring.merchant_key` shared by
  both modules, so the two cannot disagree about what one merchant is.

- **FX audit — how a foreign amount became the euro amount the totals use** (FEAT-08). Every non-EUR
  row carried a euro figure that dashboards, settlement and tax all consumed and nothing could
  explain. The Transactions toolbar now offers **FX audit** (only when the year has foreign rows),
  showing per transaction: the original amount, the ECB rate, **the date that rate was actually
  published**, the exact unrounded quotient, the stored euros, and the rounding between them — in
  thousandths of a cent. Per currency and per year it reconciles to the same integer cents the
  dashboards use.

  It keeps three things apart that are easy to collapse into a confident wrong answer: a transaction
  date is not a publication date (a Saturday purchase is converted at Friday's rate, and the FX
  tooltip used to name the booking date — wrong about two days in seven); an ECB bookkeeping
  conversion is not what your card issuer charged; and a stale rate cache is a reason to run
  `fx-update` before importing, not evidence that past conversions are wrong.

  New imports now store `fx_rate_date` and `fx_rate_source` alongside the rate, through **one**
  conversion helper shared by all three ingestion paths and the manual cash form. Existing rows need
  no migration: the audit re-derives their rate date from the read-only cache and labels it
  `rate date derived` rather than pretending it was recorded at import. The endpoint never downloads,
  never writes, and never revalues anything — a discrepancy is reported for a human to act on.

### Fixed (review follow-up)

- **The cross-process write lock stopped the app starting on Windows.** It is built on `fcntl`,
  which is POSIX-only, and importing it unconditionally meant `app.server` raised
  `ModuleNotFoundError` on the platform the README documents installation for — the app failed to
  start rather than failing to lock, which is a much worse answer to a race nobody had reported.
  The file lock now degrades to a no-op where it cannot exist, so Windows keeps exactly the
  protection it had before (two browser tabs are still serialized; a simultaneous CLI run is not),
  and POSIX keeps the full guarantee. A test hides `fcntl` and asserts the app still imports.
- **Ingest preview could promise a normalized extraction that Process would refuse.** Preview now
  applies the same reconciliation gate, so an extracted CSV without its sidecar fails immediately.
- **PDF rollback copied the entire data tree and could overwrite a concurrent CLI import.** Web and
  CLI mutations now share a cross-process lock, while rollback snapshots only transaction years,
  adjacent transfer-matching years and anchor years that the statement can actually change.
- Banco Rendimento's derived-opening rejection now names the exact prior-day date and balance the
  user must record, including actionable guidance when an existing manual balance contradicts it.
- **Normalized extracted CSVs could bypass reconciliation by being renamed.** Admission now keys
  on the detected format, not a filename suffix, and the financial oracle uses a real Nubank export
  shape instead of relying on the normalized extractor format.
- **A failed PDF import could leave transactions behind while reporting that nothing was imported.**
  Statement processing now rolls back the canonical ledger, anchors, upload metadata and moved
  source file as one operation when any publication step fails.
- **Banco Rendimento could not prove the oldest statement boundary.** Its self-derived opening now
  requires a matching manual prior-day balance, and daily printed closes verify every later boundary.
- **The doctor tolerated malformed canonical rows without identifying them.** Missing or invalid
  ids, dates, accounts, monetary values, currencies, kinds and sources are now explicit findings;
  JSONL values that are not transaction objects are reported as unreadable store data.
- The real-data tripwire now hashes every byte of large files, and closed-period coverage now tests
  rule updates/deletes and account ownership changes as retroactive financial operations.

### Fixed (found by the new tests)

- **Settlement rounded per transaction, so every odd cent went to the same person.** Halving a
  couple-owned payment as it arrived rounded once per payment instead of once per year, and the bias
  accumulated: a year of joint groceries walked one person's "paid" figure away from the truth.
  Couple-owned money is now pooled and halved once.
- **The settlement ratio was derived from rounded figures.** Seven cents of joint salary produced a
  57/43 income ratio instead of 50/50. The ratio is a proportion, so it is now computed from
  unrounded half-cents; rounding happens in exactly one place, the fair-share allocation.
- **The doctor crashed on the data it exists to find.** A row missing its account or amount, an
  amount holding text, a JSONL line that is not JSON, or a rule with no pattern each took down the
  whole effective view — so the integrity check died alongside the corruption it was run to report,
  and a damaged store looked like a broken app. Each is now a finding. An unreadable file is reported
  as `unreadable-file`, and the audit says it ran on partial data.
- **`store.rewrite_year` published the canonical ledger through a shared `.tmp` name** and assumed
  the year directory existed — the same defect already fixed in `write_json`.

### Added (test coverage)

- **`tests/test_settlement_properties.py`** — 1200+ checks over 133 scenarios of deliberately awkward
  money (one-cent totals, odd cents on joint accounts, 1/99 and 1/3 ratios, refunds larger than the
  spend, negative payroll corrections). It asserts conservation exactly and *faithfulness* — every
  reported figure within one cent of the exact `Fraction` value — so conservation cannot be bought by
  handing one person everything. Both settlement bugs above were found by it.
- **`tests/test_format_matrix.py`** — a sanitized statement for all 10 declared formats (`volksbank`
  and `nubank-conta` had none at all), each read field by field, then a mutation table applied to
  every one: text/NaN/Infinity/formula where money belongs, an impossible year, the wrong decimal
  style. Every mutation must refuse the whole file. Also BOM, CP1252, quoted delimiters and embedded
  newlines, trailers, zero rows and empty statements. A new `pipeline/formats/*.json` without a
  fixture now fails the build.
- **`tests/test_closed_period.py`** — the closed-month write matrix: nine operations asserted refused
  with the stored bytes unchanged, and the one that is allowed by design (a merchant rule) asserted
  to surface its drift both on the dashboard and in the integrity check. A new write endpoint that is
  neither listed as irrelevant nor covered fails the suite.
- **`tests/test_http_contract.py`** — the app over a real socket rather than as a bag of functions:
  malformed bodies, the status codes the UI branches on, uploads, the lock middleware and its session
  cookie, path traversal, and 36 concurrent writes across two documents asserted to all survive.
- Extraction, doctor-robustness, extractor-truncation and FX cent-boundary cases added to
  `tests/test_integrity_gates.py` and `tests/test_fx.py`; the oracle now protects a **manifest of
  named financial scenarios** (couple income, split parts, reimbursements, year costs, foreign
  currency…) so removing a branch fails the suite instead of shrinking it quietly.
- The browser smoke now checks that **displayed figures equal computed ones** (settlement and
  dashboard tiles against their APIs) and renames a category to an XSS payload to prove the app shows
  it rather than runs it.
- The real-data tripwire hashes file **contents**, not just mtime and size — a same-length,
  timestamp-preserving edit used to pass it.

### Security

- **A category name or icon could put working script into every screen that showed it.** Names are
  free text and were interpolated straight into markup; icons were free text rendered between
  `<md-icon>` tags. Renaming a category was therefore enough to run code in the app — for anyone on
  the network in LAN mode, and through an imported config or a restored backup. Names are now escaped
  at every HTML sink (they stay unescaped in `catName()` itself, because chart labels are drawn to a
  canvas and would otherwise show `&amp;`), and **every icon passes through one validator**,
  `safeIcon()`, instead of being escaped at each of a dozen sinks — the dozenth is the one that gets
  forgotten. The server refuses a non-Material-Symbol icon on the way in as well. A browser test
  plants a payload and asserts it renders as text.

### Fixed

- **Settlement conserved cents.** Money was accumulated as floats and each person's figure rounded on
  its own, so a shared cent paid from the joint account became two cents of "paid" and two of "fair
  share". Everything is integer cents now, split by largest remainder, and `sum(paid)`,
  `sum(fair_share)` and `sum(balances)` are asserted against the shared total before the numbers are
  returned. **On real data this moved the 2025 annual settlement by one cent** — the old figures
  claimed a cent more was paid than the household spent. The doctor reports it as closed-month drift,
  which is exactly the mechanism for it; accept it from the Dashboard.
- **A monthly ratio override could rewrite a closed month.** The lock compared the override key
  (`"3"`) against month state keyed `"2026-03"`, so it never matched and every monthly override went
  through. An unknown key is now an error rather than something stored and read by nothing.
- **A statement row with an unreadable amount vanished.** A valid date with a broken amount was
  skipped and not counted, so a statement could import incomplete while still looking whole. It now
  refuses the file. An empty amount cell is still an informational row, as before.
- **`NaN` and `Infinity` parsed as money**, and could reach the store, where every total they touch
  becomes `NaN`. They are refused, `cents()` rejects non-finite input, and `write_json` will not
  publish one.
- **A date outside 1900–2999** was filed under `data/<year>` by its literal digits — `0001-01-01`
  wrote `data/1`, which nothing in the app ever reads. The import said it succeeded and the money was
  gone. Such a date now refuses the file, and the doctor reports any directory an older version
  already filed away.
- **Ratios that were not fractions.** Reference shares only had to sum to 100%, so `-20% / 120%` was
  accepted; and a negative annual salary total produced ratios like -11% / 111% and a transfer larger
  than the whole shared cost. Shares are now validated to `[0, 1]`, and a negative salary total falls
  back to the reference ratio and says so **on the Settlement page**, beside the figures.
- **Creating a rule validated nothing but its scope**, while updating one validated everything —
  and a rule applies to the whole history. An empty pattern (a substring of every string), an unknown
  category, an invented sharing value and an arbitrary action were all accepted. Both paths now share
  one `_validate_rule()`.
- **Two identical rows in one JSONL were invisible to the doctor**, which counted the filenames an id
  appeared in rather than the occurrences. The ledger counted the money twice under a clean bill of
  health.
- **Concurrent writes could discard each other.** Nearly every endpoint is read-modify-write over a
  whole document, and two tabs (or the second device LAN mode exists for) could both read the old one.
  Mutating requests are serialized; reads stay parallel. `write_json` also no longer shares one
  `.tmp` name between writers, and fsyncs before publishing.
- **`cash.csv` was written in place**, so a failure part-way through truncated the cash ledger, and a
  failure after it left the CSV and the derived JSONL describing different things. The write is atomic
  and the pair is now all-or-nothing.
- **A typo in a backup or restore selection meant "everything".** `parts=dta` silently widened the
  operation; an unrecognised part is now a 400.
- **PDF imports carrying balance anchors crashed after writing their data** and reported "No data was
  imported" — found by the new admission tests, not by the review.

### Changed

- **An extraction now has to prove itself, not assert itself.** The server admitted any PDF whose
  extractor returned `status: "ok"` and imported whatever CSV was beside it, which made the project's
  one safety invariant a convention each producer was trusted to keep. `pipeline/extraction.py` re-runs
  `opening + sum(rows) == closing` in integer cents over the file that is actually about to be
  imported, and holds the report to its own claims about that file. The `extract-statement` skill
  writes `<name>.extracted.csv` plus a reconciliation report, and the pipeline refuses the first
  without the second. Its instruction to "extract anyway and flag the output as unreconciled" when a
  document prints no balances is gone — there was nothing to reconcile against and nothing downstream
  read the flag.
- **The Banco Rendimento extractor no longer reconciles against itself.** Its opening balance is
  derived from the first row it read, so a statement truncated at either end balanced perfectly. The
  `Saldo Final` line closing each date section — written by the bank, read independently of the
  transaction rows — is now checked against the rows, and a document that prints none is refused.
- The doctor reports a decision that reassigns a transaction's account, because settlement and
  coverage follow it while balance reconciliation and net worth read the imported account.

### Added

- **An Overview page across every year on record, and the app now opens on it.** Four figures
  (income, expenses, savings, savings rate), liquid net worth over every month, income vs expenses
  by month, and where the money goes. Reachable any time from the home icon beside the year
  selector — it sits there rather than in the tab row because it is the one page the year selector
  does not apply to. The figures are the same `year_summary` the Dashboard shows, added up: widening
  the window must not change what a euro means. Year costs can be spread across the months of the
  year they belong to, never across another year's, using the same switch and the same caveat as the
  Dashboard. A second "where the money goes" breaks the same total down by **subcategory**: the 15
  biggest are named and everything smaller is collapsed into one Other slice, because thirty named
  slivers is a legend, not a chart. Subcategories inherit their category's colour, so each one steps
  along a light-to-dark ramp inside its family — the grouping still reads and the slices stay apart.

- **Settings › Balances**: a year grid of recorded account balances — months down, accounts across, with
  a per-month total. Each cell is either a balance recorded from a statement (carrying its reconciliation
  verdict) or the figure the ledger computes, shown so you can check it against your bank. Entry is a
  per-account-year dialog: type a balance, press Enter, move to the next month. Correcting a recorded
  figure now needs an explicit `replace` (`POST /api/anchor`), so a contradiction still conflicts by default.
- **Bulk actions ask before they write.** The confirmation lists the fields it is about to change,
  built from the same object that gets sent, and lives inside `bulkRun` so a bulk action added later
  cannot skip it.
- **Year costs can be spread across the months** in the dashboard's income-vs-expenses chart, with a
  switch that defaults on. Without it the twelve months summed to less than the year totals printed
  above them and nothing said so. The caption always names which of the two views is on screen, and the
  divisor is the months that have happened, so a running year is not amortized into its own future.

### Changed

- The dashboard net-worth chart shows the selected year only (January to December) instead of the whole
  history, and is renamed **Liquid net worth** — it charts recorded account balances, not total wealth.
  Outside the running year the heading reads "End of &lt;year&gt;" rather than "Now".
- **Writes confirm themselves with a toast.** `showMessage` was a modal with an OK button, so every
  saved edit cost a click; it is now a transient message in the corner. Confirmation moved into `api()`
  — every POST reports — so feedback no longer depends on which screen you are on. Errors stay up
  longer and keep a close button.
- Settings spans the full app width like every other page. Content keeps a readable measure; sections
  holding a data grid opt out of it.

### Fixed

- The app shell stamps its own asset versions from each file's mtime and size, instead of hand-written
  `?v=` strings in `index.html`. A forgotten bump shipped new JS with the previously cached stylesheet,
  which looks exactly like a broken page.

## [1.0.0] - 2026-07-22

### Added

- Initial public release. UnPAIN is a self-hosted, offline-first expense tracker for two-person
  households:
  - Bank CSV ingestion with format auto-detection (`pipeline/formats/*.json`), content-hash dedupe,
    and ECB foreign-currency conversion. Deterministic PDF extraction for PDF-only sources, gated by
    balance reconciliation.
  - Learned merchant-categorization rules (family- and per-person-scoped), applied on read. UnPAIN
    recomputes everything derived (dashboards, settlement, tax) and never stores it.
  - Income-proportional settlement between partners, with a monthly estimate and a binding annual
    true-up. Recurring-payment detection. A year-end German tax evidence pack.
  - FastAPI and vanilla-JS Material Design 3 web UI (English and German), fully offline. Optional
    LLM skills at the edges (categorization proposals, statement extraction) — no API keys, no
    vendor lock-in.
  - Try it with `./start.sh --demo` (synthetic data in an isolated `./demo/`).
