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

## Install the app (no terminal)

Download the installer for your computer from the
[website](https://leandrorsampaio.github.io/unpain/#download) or the
[latest release](https://github.com/leandrorsampaio/unpain/releases/latest). Python is inside the
app. You do not need to install anything else.

| Computer | File |
|---|---|
| Mac with Apple silicon (M1 and newer) | `UnPAIN-macos-arm64.dmg` |
| Mac with Intel | `UnPAIN-macos-x86_64.dmg` |
| Windows 10 or 11 (64-bit) | `UnPAIN-windows-x64-setup.exe` |
| Linux: Debian, Ubuntu | `UnPAIN-linux-amd64.deb` |
| Linux: other | `UnPAIN-linux-x86_64.tar.gz` |

**The installers are not signed.** Signing costs money every year, and UnPAIN is free. Thus macOS
and Windows show a warning the first time. You do this step one time only.

**macOS**

1. Open the `.dmg` file. Drag **UnPAIN** to **Applications**.
2. Open UnPAIN from Applications. macOS says that it "could not verify" UnPAIN. Click **Done**.
3. Open **System Settings → Privacy & Security**. Scroll down to "UnPAIN was blocked". Click
   **Open Anyway**, then enter your password.

After this, UnPAIN opens like any other app.

**Windows**

1. Open the setup file. Windows shows "Windows protected your PC".
2. Click **More info**, then click **Run anyway**.
3. Follow the installer. It does not need an administrator account.

UnPAIN is now in the Start menu and on the desktop.

**Linux.** Open the `.deb` file with your software installer, or run
`sudo apt install ./UnPAIN-linux-amd64.deb`. Then start UnPAIN from the app menu. On other
distributions, extract the `.tar.gz` file and run `UnPAIN/UnPAIN`. Only the automated build tests
the Linux packages.

**How it works.** When you open UnPAIN, a small window appears, and your browser opens the app. The
window shows where your data is. To stop UnPAIN, close the window or click **Quit**. If you open
UnPAIN again while it runs, it only opens the browser again.

**Your data** is in the `UnPAIN` folder in your home folder: `~/UnPAIN` on macOS and Linux,
`C:\Users\<you>\UnPAIN` on Windows. It is not in Documents, because macOS and Windows can sync
Documents to iCloud or OneDrive. An update or an uninstall never touches this folder. To update,
install the new version over the old one.

**Move from a source install.** In the old app, go to **Settings → Data** and download a backup. In
the new app, complete the setup wizard, then restore the backup in **Settings → Data**.

**Limits of the installed app:**

- It answers only on this computer (`127.0.0.1`). Other devices cannot reach it. For access from
  your home network, run from source with `./start.sh --lan`.
- PDF statement import needs the `pdftotext` program, and the app does not include it. On Linux,
  install `poppler-utils`. On macOS and Windows, use the CSV export of your bank.

To build the installers yourself, see [Desktop app](#desktop-app) under Development.

## Run from source (terminal)

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
categorization), and `config.json`. The installed app keeps the same files in `~/UnPAIN`. Git ignores
all of it. The in-app Backup button zips it. Back it up yourself, because git does not carry your
data, and there is no server-side copy to recover from. See [SECURITY.md](SECURITY.md) for the
threat model (no auth by design).

## Development

```bash
npm install && npx playwright install chromium   # browser smoke test (dev only)
git config core.hooksPath .githooks
./run-tests.sh
```

Stack: Python 3.12 and FastAPI, vanilla JS with vendored Material Design 3 components. There is no
build step, and it is fully offline. Agents and contributors start at [PROJECT.md](PROJECT.md) and
[CONTRIBUTING.md](CONTRIBUTING.md).

### Desktop app

`desktop/` packages the app for people who do not use a terminal. `desktop/launcher.py` starts the
same server on `127.0.0.1`, keeps the data in `~/UnPAIN`, opens the browser, and shows one small
window with **Quit**. PyInstaller bundles it with Python. Each OS then gets its own installer: a
`.dmg` on macOS, an Inno Setup `.exe` on Windows, and a `.deb` plus a `.tar.gz` on Linux.

```bash
pip install --require-hashes -r desktop/requirements-build.txt   # in a Python that has tkinter
python desktop/build.py                                          # app + self-test + installer → dist/
```

The build runs the built app with `--self-test` before it packages anything. The self-test starts
the server on an empty folder and checks the page, the setup wizard, the bank formats, the window
toolkit, and the local-only guard. The [Desktop workflow](.github/workflows/desktop.yml) builds and
self-tests all four targets (macOS arm64 and Intel, Windows, Linux) when `desktop/` changes. A `v*`
tag attaches the installers to a **draft** release. You publish it yourself.

## Author, status, and licence

Built by Leandro, a software engineer in Germany, for one household and shared as-is. Issues and PRs
are welcome. There is no support promise, and no roadmap beyond what we need ourselves.

If UnPAIN saved you time, 🍺 [buy me a beer](https://buymeacoffee.com/lsampaio).

**License: [PolyForm Noncommercial 1.0.0](LICENSE).** Free for any noncommercial use — personal,
hobby, students, research, non-profits, and public institutions. Commercial or for-profit use
requires a separate paid license; open an issue or reach out to arrange one. This is a
source-available license, not an OSI open-source license.
