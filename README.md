# UnPAIN

**Un**necessarily **P**recise **A**ccounting & **I**ncome **N**avigator

[![Tests](https://github.com/leandrorsampaio/unpain/actions/workflows/tests.yml/badge.svg)](https://github.com/leandrorsampaio/unpain/actions/workflows/tests.yml)
[![License: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/License-PolyForm%20Noncommercial%201.0.0-blue.svg)](LICENSE)

UnPAIN is a self-hosted, offline-first expense tracker for **two-person households**. You drop your
bank CSV exports in a folder, review a short queue in the browser, and get monthly and yearly
dashboards, an income-proportional settlement between partners, and a German tax evidence pack. Your
financial data never leaves your machine.

> **Why it exists, who it's for, and screenshots → [the website](https://leandrorsampaio.github.io/unpain/).**
> This README is the technical reference.

## What it does

- **Deterministic core.** Parsing, categorization, FX, settlement, and tax are plain Python. UnPAIN
  recomputes them from your source data and decisions. It never stores derived values.
- **Learned categorization.** Teach a merchant once. Rules apply on read, scoped to the family or to
  one person. Manual work approaches zero over time.
- **Multi-currency.** EUR is the base. UnPAIN converts foreign-currency accounts at the ECB rate of
  the transaction date and keeps the original amount.
- **Fair settlement.** Shared costs split in proportion to income — a monthly estimate, and a binding
  true-up after Dec 31.
- **Optional LLM at the edges.** CLI-agent skills propose rules for unseen merchants and extract
  PDF-only statements, gated by balance reconciliation. No API keys, no vendor lock-in.
- **Private by design.** No cloud, no telemetry, no login. Fully offline.
- **German tax pack (optional).** Refund-relevant spending, bucketed for ELSTER or a Steuerberater.

## Quick start

```bash
git clone <repo-url> && cd unpain
./start.sh            # creates a Python 3.12 venv on first run, then starts the app
```

Open http://localhost:8765. A **setup wizard** asks for your two names and creates the first
configuration.

To reach the app from another device on your home network, run `./start.sh --lan`. This binds all
interfaces. Do this only on a network you trust, because the app has no login.

On Windows, run these commands:

```
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\uvicorn app.server:app --port 8765
```

One difference on Windows: UnPAIN serializes writes between the web app and the `pipeline.cli`
commands with a POSIX file lock, which Windows does not have. Browser tabs are still serialized
against each other, but running a CLI command that writes (`ingest`, `fx-update`, `close-baseline`)
while the server is running can interleave with a write from the browser. Stop the server first, or
use the web app for everything.

To look around first, run `./start.sh --demo`. It seeds an isolated `demo/` folder with five years of
synthetic data and serves it. Your real data stays untouched.

## Monthly workflow

1. Download the CSV export from each bank. Name each file `<account-id>__anything.csv`. The account
   ids come from the Accounts tab. Drop the files in `inbox/`. PDF-only sources go through the
   deterministic extractors on the Ingest page.
2. Click **Ingest inbox**. Then work through the **Review** tab. "Apply + rule" teaches the system
   permanently.
3. Check the Dashboard. Then click **Close month**.

A new year needs no setup. Transactions go into `data/<year>/` by their date. Your rules and
categories carry over automatically.

## Add your bank

CSV parsing is configuration, not code. Each format is one JSON file in `pipeline/formats/`. It maps
the bank's headers to fields (delimiter, decimal style, date format). If UnPAIN does not recognize
your export, the ingest error prints the headers it saw. Copy an existing format file and adjust it.
PRs with new bank formats are the most welcome kind — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Command line (optional)

The web app is the main interface. The same pipeline is also scriptable:

```bash
.venv/bin/python -m pipeline.cli ingest        # process inbox/
.venv/bin/python -m pipeline.cli status        # review counts per year
.venv/bin/python -m pipeline.cli settle 2026   # annual settlement (add --month 6 for an estimate)
.venv/bin/python -m pipeline.cli tax 2026      # tax evidence pack JSON
.venv/bin/python -m pipeline.cli fx-update     # refresh ECB rates (needed once for non-EUR)
.venv/bin/python -m pipeline.cli doctor        # read-only data integrity audit
```

## Reconciliation gate

Any LLM-extracted statement must satisfy `opening balance + sum(transactions) == closing balance` to
the cent before it enters the store. Otherwise UnPAIN rejects the data. LLMs never write to `data/`
directly — extraction goes through `inbox/` CSVs. For a visual walkthrough, open
[`how-it-works.html`](how-it-works.html) in a browser.

## Data, privacy, and backups

Everything stays in this folder: `data/` (transactions, decisions), `rules/` (your learned
categorization), and `config.json`. Git ignores all of it. The in-app Backup button zips it. Back it
up yourself, because git does not carry your data, and there is no server-side copy to recover from.
See [SECURITY.md](SECURITY.md) for the threat model (no auth by design).

## Development

```bash
npm install && npx playwright install chromium   # browser smoke test (dev only)
git config core.hooksPath .githooks
./run-tests.sh
```

Stack: Python 3.12 and FastAPI, vanilla JS with vendored Material Design 3 components. There is no
build step, and it is fully offline. Agents and contributors start at [PROJECT.md](PROJECT.md) and
[CONTRIBUTING.md](CONTRIBUTING.md).

## Author, status, and licence

Built by Leandro, a software engineer in Germany, for one household and shared as-is. Issues and PRs
are welcome. There is no support promise, and no roadmap beyond what we need ourselves.

If UnPAIN saved you time, 🍺 [buy me a beer](https://buymeacoffee.com/lsampaio).

**License: [PolyForm Noncommercial 1.0.0](LICENSE).** Free for any noncommercial use — personal,
hobby, students, research, non-profits, and public institutions. Commercial or for-profit use
requires a separate paid license; open an issue or reach out to arrange one. This is a
source-available license, not an OSI open-source license.
