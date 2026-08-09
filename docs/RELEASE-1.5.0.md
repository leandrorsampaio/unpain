# UnPAIN 1.5.0 — the integrity release

**1.0 could compute a household's finances. 1.5 can show its work, and be told when it is wrong.**

Seventy-five commits since 1.0.0. No migration, no config change, no new dependencies. If you are
running 1.0, pull and restart.

---

## Why this release exists

A ledger you built yourself has a specific problem: it is only as trustworthy as your confidence
that it is not quietly wrong. A total that is off by a cent looks exactly like a total that is
right. A spreadsheet you exported last March looks exactly like one you exported this morning. A
month you closed in January looks settled even if a rule you wrote in June silently changed what it
contains.

1.5 is about closing that gap. Almost everything here answers one of three questions:

- **Is this number right?** — and can something other than the code that produced it say so.
- **Where did this number come from?** — and can it be reproduced.
- **Has something changed since I last looked?** — and can I be told what, not just that.

---

## The headline changes

### One place where a number becomes money

`pipeline/money.py` is now the only place a value turns into an amount. The rounding policy is
named, versioned, and applied identically everywhere — exchange-rate conversion included, which
previously divided floats and rounded afterwards.

While consolidating this, one thing surfaced worth stating plainly: **the app had always used
banker's rounding while the docs claimed half-up.** That is now documented rather than silently
corrected, because changing a rounding policy moves historical figures and is a migration with a
before/after report, not a refactor. Verified against the real store: zero figures moved.

### An auditor that does not take the code's word for it

`doctor` recomputes totals the long way — from the effective rows, in integer cents, deliberately
*without* calling the functions it audits. An auditor that calls what it audits can only ever agree
with it.

It checks identities that hold for any correct ledger: the year equals the sum of its months;
income and expenses partition the counted lines; each person's balance equals what they paid minus
their fair share; the settlement transfer clears exactly the positive balance; and every fair share
matches the exact allocation from the salary weights. That last group matters because **conservation
alone is blind to a compensating error** — move a cent from one person's share to the other's and
every sum still balances while the settlement asks the wrong person for money.

### Exports that can prove where they came from

Every workbook now carries a **Metadata** sheet: the statements behind the rows with a hash of each
upload, the exchange-rate evidence, how much of the year was still in review and how much was
closed, the rounding policy, a digest of the canonical inputs, a digest of the workbook's own cells,
and the definitions that decide what a total *means* — with the exclusions spelled out.

Two promises are kept apart, because they are not the same promise. *Audit* reproducibility is
always provided. *Binary* reproducibility — same inputs, same bytes — needs the timestamp supplied
rather than read from the clock, so both endpoints take `as_of` and `SOURCE_DATE_EPOCH` is honoured.

New command:

```
python -m pipeline.cli export-verify data/2025/transactions-2025.xlsx
```

It answers two questions separately — **INTACT** (the cells still hash to what the file declares)
and **CURRENT** (the store still holds what it was built from). A file can be intact and stale
(re-export it) or current and edited (do not trust it). Those are very different situations and
nothing could tell them apart before.

### Restore that validates before it destroys

Restoring a backup used to write a safety copy, delete your live `data/`, `rules/` and
`config.json`, and *then* copy the archive in. Everything after that delete was unprotected.

Now a complete candidate tree is assembled beside the live one and validated against every schema
and cross-reference the app has, and only then swapped in — moving the live directories aside
rather than deleting them. An uploaded zip is treated as untrusted input: traversal in either
separator style, absolute and drive-letter paths, symlinks, device nodes, case-collisions, CRC
failures and decompression bombs are all refused before a single byte is extracted, and every
rejection is asserted to leave the live tree byte-for-byte identical.

### Multi-file operations that land whole, or not at all

Every individual write already published atomically. That was not enough: the operations that fail
span *several* files. A month marked closed whose baseline never landed is a month whose drift
detection silently does nothing, and no single file can reveal it.

`pipeline/bundle.py` gives closing a month, closing a year and importing a recoverable boundary —
name the paths, and the body either finishes or every path goes back. The journal outlives the
process, so a crash is rolled back on the next start rather than discovered months later.

Both recovery paths only ever roll *back*, to a state that certainly existed. Rolling *forward*
means guessing which half-written state to adopt, and a wrong guess discards good data — a worse
failure than the one it prevents.

### Evidence you can read

Three features that describe the ledger without ever being read back to compute it:

- **FX audit** — reproduce any stored euro amount from its foreign amount and the ECB rate that
  converted it, entirely offline.
- **Review suggestions** — duplicates, spikes and cadence breaks worth a second look. A heuristic
  never edits a transaction.
- **What changed?** — when a settled period no longer matches, the app explains *what* moved, not
  just that something did. Detecting drift and never mentioning it is most of the way to not
  detecting it.

### And in the app itself

- **Overview** — an all-years landing page: net worth over every month, income vs expenses, and
  where the money goes. Two windows meet here and are not the same, so each is labelled with its
  own span.
- **Balances** — a year grid of recorded balances, months down and accounts across. A recorded
  balance (an anchor from a statement) and a derived one (what the ledger computes) never blur.
  There is deliberately no button that adopts a derived figure as recorded, because that would turn
  every cell green while proving nothing.
- **Settings** — reorganised into a left rail of sub-pages, and every field autosaves.
- Bulk actions ask before they act, and every write confirms with a toast.

---

## Fixed

The three review rounds this release went through found real defects, including in the fixes for the
previous round. The full list is in [CHANGELOG.md](../CHANGELOG.md); the ones that could have cost
you money or data:

| What | Why it mattered |
|---|---|
| Restore's rollback could delete the data it was protecting | A failure part-way through left the tree half old, half new — and removed the displaced original |
| The settlement gave the odd cent to the wrong person | Float weights made largest-remainder break ties on rounding error. 164 of 36,504 cases, always by one cent |
| `export-verify` verified the store, not the workbook | An amount edited inside a spreadsheet still reported VERIFIED |
| Binary reproducibility was never actually implemented | openpyxl re-stamps `dcterms:modified` inside `save()`; the test built both files in the same second |
| Schema coverage was five files wide and called complete | `{"2026-13": "banana"}` was accepted as a month lock — and restore's safety argument rests on that gate |
| The admission gate could be skipped by asserting it had run | A boolean the caller declared, naming no file |
| `append_transactions` wrote in place | An interrupted append left a half-written line in the canonical ledger |

---

## Upgrading from 1.0

```
git pull
.venv/bin/pip install -r requirements.txt      # no new dependencies, but harmless
.venv/bin/uvicorn app.server:app --port 8765
```

Then, once:

```
.venv/bin/python -m pipeline.cli doctor            # should report 0 errors
.venv/bin/python -m pipeline.cli close-baseline    # upgrade pre-1.5 closed months
```

Two things to expect:

- **Workbooks exported before 1.5 will report `STALE`.** The provenance digest now covers more
  inputs than it did. Re-export and they verify.
- **`doctor` may report `closed-month-stale-baseline`** for months closed under 1.0. Those baselines
  predate the settlement fields, so they are only partly watched. `close-baseline` adopts the
  current figures — read what it reports before accepting.

---

## What this release does not claim

Honesty is cheaper than a support thread.

- It has been exercised in earnest against **one household's data**. The arithmetic is covered by 45
  test files, a five-year synthetic oracle and CI on every branch — but breadth of real-world bank
  formats is not the same as depth of testing, and only you have your bank's statements.
- The `barclays-de` format is marked **best-guess**: nobody has parsed a real Barclays statement
  with it. The app tells you this at import time. Check the first one by hand.
- It is a bookkeeping tool, not an accounting product. It has no opinion about your tax return
  beyond assembling the evidence you point it at.

---

*UnPAIN is self-hosted, offline, and free for noncommercial use under
[PolyForm Noncommercial 1.0.0](../LICENSE). No accounts, no telemetry, no cloud.*
