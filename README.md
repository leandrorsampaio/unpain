# UnPain

**Un**necessarily **P**recise **A**ccounting & **I**ncome **N**avigator

[![Tests](https://github.com/leandrorsampaio/UnPain/actions/workflows/tests.yml/badge.svg)](https://github.com/leandrorsampaio/UnPain/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

UnPain is a self-hosted, offline-first expense tracker for **two-person households**. You drop
your bank CSV exports in a folder. You review a small queue in the browser. You get monthly and
yearly dashboards, an income-proportional settlement between partners, and a German tax evidence
pack. Your financial data stays on your machine.

![Dashboard](docs/screenshots/dashboard.png)

## Who this is for

- Couples who split shared costs **in proportion to income**. You get a monthly estimate and a
  binding yearly true-up.
- People in the euro area. EUR is the base currency. UnPain converts foreign-currency accounts
  (USD, BRL, CHF, and any ECB reference currency) at the ECB rate of the transaction date.
- People in Germany get a bonus. UnPain builds a year-end tax evidence pack (Werbungskosten,
  Sonderausgaben, §35a) for ELSTER or a Steuerberater. Other users ignore or reseed the tax layer.
- People who can run one local command. There is no cloud, no account, and no telemetry. There is
  also no login. Run UnPain on localhost or a network you trust.

## Quick start

```bash
git clone <repo-url> && cd UnPain
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

To look around first, run `./start.sh --demo`. It seeds an isolated `demo/` folder with five years
of synthetic data (Alex and Sam) and serves it. Your real data stays untouched.

## Monthly workflow

1. Download the CSV export from each bank. Name each file `<account-id>__anything.csv`. The account
   ids come from the Accounts tab. Drop the files in `inbox/`. PDF-only sources go through the
   deterministic extractors on the Ingest page.
2. Click **Ingest inbox**. Then work through the **Review** tab. "Apply + rule" teaches the system
   permanently. After a few months, almost everything categorizes itself.
3. Check the Dashboard. Then click **Close month**.

A new year needs no setup. Transactions go into `data/<year>/` by their date. Your rules and
categories carry over automatically.

## Add your bank

CSV parsing is configuration, not code. Each format is one JSON file in `pipeline/formats/`. It
maps the bank's headers to fields (delimiter, decimal style, date format). If UnPain does not
recognize your export, the ingest error prints the headers it saw. Copy an existing format file and
adjust it. PRs with new bank formats are the most welcome kind.

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

## How it works (the short version)

> For a visual walkthrough, open [`how-it-works.html`](how-it-works.html) in a browser.

- **Deterministic core.** Parsing, categorization rules, FX, settlement, and tax math are plain
  Python. UnPain recomputes them from source data and your decisions. It never stores derived
  values.
- **Optional LLM at the edges.** `skills/` contains instructions that any CLI agent (Claude Code,
  Gemini CLI) can run. An agent proposes categorization rules for unseen merchants or extracts
  PDF-only statements. A balance reconciliation gates every extraction (`opening + sum == closing`
  to the cent, or UnPain rejects the data). No API keys and no vendor lock-in. The app is fully
  usable without any LLM.
- **Fairness model.** Only salary counts toward the income ratio. UnPain computes the binding
  settlement after Dec 31 from actual annual income. Personal and out-of-scope expenses never enter
  the split.

## Data and privacy

Everything stays in this folder: `data/` (transactions, decisions), `rules/` (your learned
categorization), and `config.json`. Git ignores all of it. The Backup button zips it. Back it up
yourself, because git does not carry your data.

## Development

```bash
npm install && npx playwright install chromium   # browser smoke test (dev only)
git config core.hooksPath .githooks
./run-tests.sh
```

Stack: Python 3.12 and FastAPI, vanilla JS with vendored Material Design 3 components. There is no
build step, and it is fully offline. Agents and LLM contributors start at [PROJECT.md](PROJECT.md).

## Status and support

We built UnPain for our own household and share it as-is. Issues and PRs are welcome. There is no
support promise and no roadmap beyond what we need ourselves. License: MIT (see LICENSE).
</content>
</invoke>
