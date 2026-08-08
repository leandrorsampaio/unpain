# Changelog

This file documents all notable changes to UnPAIN.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). UnPAIN aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
