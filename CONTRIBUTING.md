# Contributing

Thanks for your interest. We built UnPAIN for one household and share it as-is. Contributions are
welcome, especially **new bank format files**. They are the single most useful thing you can add.
Please be respectful and follow our [Code of Conduct](CODE_OF_CONDUCT.md).

Read these files before you open a PR:

- [PROJECT.md](PROJECT.md) — what the project is and why.
- [CLAUDE.md](CLAUDE.md) — architecture and invariants.
- [AGENTS.md](AGENTS.md) — the "one concept, one component" rules (for any UI work).

## Development setup

You need **Python 3.12** and **Node 22+**. Node is only for the browser smoke test.

```bash
git clone <repo-url> && cd unpain
./start.sh                                   # creates the .venv and starts the app on :8765

# To run the full test suite (dev only):
npm install && npx playwright install chromium
git config core.hooksPath .githooks          # runs tests automatically before each commit
./run-tests.sh                               # full suite (add --fast to skip the browser smoke)
```

To try it with throwaway demo data, run this command. It never touches your real `data/`:

```bash
./start.sh --demo        # seeds an isolated ./demo/ with 5 years of Alex and Sam data, then serves it
```

## Add a bank format (most-wanted contribution)

CSV parsing is **configuration, not code**. Each bank is one JSON file in `pipeline/formats/`. It
maps the export's columns to canonical fields (delimiter, decimal style, date format,
header-to-field mapping). To add yours:

1. Drop your CSV in `inbox/` and click **Ingest**. If UnPAIN does not recognize the format, the
   error prints the exact headers it saw.
2. Copy the closest existing file in `pipeline/formats/` (e.g. `dkb.json`, `n26.json`). Adjust the
   header mapping, `delimiter`, `decimal` (`comma` or `dot`), and `date_format`.
3. Re-ingest until it parses, then **add your format to `tests/test_format_matrix.py`** — one
   sanitized two-row statement and the column indexes for its date and amount. The suite fails on a
   format with no fixture, and it will run the whole mutation table (broken amounts, impossible
   dates, the wrong decimal style) against yours for free. Use synthetic rows.
4. Open a PR that describes the bank and country. Use **synthetic** rows in fixtures. Never use real
   statement data or IBANs.

## Code style and invariants

- **Match the surrounding code.** Use no new frameworks and no build step. The frontend is vanilla
  JS and native ES modules. The backend is plain Python 3.12.
- **Linting.** [Ruff](https://docs.astral.sh/ruff/) checks Python style (config in `ruff.toml`; a
  deliberately light default rule set). `./run-tests.sh` runs it when it is installed
  (`.venv/bin/pip install 'ruff==0.15.22'`, the version CI pins) and tells you when it is not.
  You can also run it directly — for example `uvx ruff check` or `pipx run ruff check`. An `.editorconfig` keeps indentation consistent (4-space Python, 2-space
  everything else).
- **Money** uses integer cents (`pipeline/util.cents()` or the JS `cents()` mirror). Never compare
  money with float equality.
- **Escape user text** (`esc()`) anywhere it reaches `innerHTML`.
- **User-facing strings** go through `T(...)`. Every key must exist in `app/static/i18n/de.js`
  (`tests/test_i18n.js` enforces this). Machine values (slugs, enums) stay English.
- **Never weaken a test to make it pass.** A red test means the code is wrong, or the test found a
  real bug. The `MIN_CHECKS` counters and the real-data tripwire in `run-tests.sh` enforce this.
- **Never commit personal data.** `data/`, `rules/`, `config.json`, `inbox/`, `receipts/`, and
  `backups/` are gitignored. Keep it that way. Source files are not covered by that ignore list,
  so `.githooks/check-personal-data.sh` (run by the pre-commit hook) refuses staged changes that
  contain a real-looking IBAN or a household name from your local `config.json`. Layout examples
  copied out of a real statement into a parser, README or test are the usual way this slips
  through — an IBAN cannot be rotated once it is public.

## Pull requests

- Keep PRs focused. Conventional-Commit-style messages are appreciated (`feat:`, `fix:`, `docs:`).
- The full test suite must pass. It includes the `test_oracle.py` accounting-math checker. CI runs
  it on every push and PR.
- When you contribute, you agree to license your contributions under the project's
  [PolyForm Noncommercial 1.0.0](LICENSE) license.
