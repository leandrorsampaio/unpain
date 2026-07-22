# Contributing

Thanks for your interest! This project was built for one household and is shared as-is, but
contributions are welcome — especially **new bank format files**, which are the single most
useful thing you can add. Please be respectful and follow our
[Code of Conduct](CODE_OF_CONDUCT.md).

Please read [PROJECT.md](PROJECT.md) (what the project is and why), [CLAUDE.md](CLAUDE.md)
(architecture + invariants), and — for any UI work — [AGENTS.md](AGENTS.md) (the "one concept,
one component" rules) before opening a PR.

## Development setup

Requires **Python 3.12** and **Node 22+** (Node is only for the browser smoke test).

```bash
git clone <repo-url> && cd FamilyAccountability
./start.sh                                   # creates the .venv and starts the app on :8765

# For running the full test suite (dev only):
npm install && npx playwright install chromium
git config core.hooksPath .githooks          # run tests automatically before each commit
./run-tests.sh                               # full suite (add --fast to skip the browser smoke)
```

Try it with throwaway demo data (never touches your real `data/`):

```bash
./start.sh --demo        # seeds an isolated ./demo/ with 5 years of Alex & Sam data, then serves it
```

## Adding a bank format (most-wanted contribution)

CSV parsing is **configuration, not code**. Each bank is one JSON file in `pipeline/formats/`
that maps the export's columns to canonical fields (delimiter, decimal style, date format,
header→field mapping). To add yours:

1. Drop your CSV in `inbox/` and click **Ingest**. If the format isn't recognized, the error
   prints the exact headers it saw.
2. Copy the closest existing file in `pipeline/formats/` (e.g. `dkb.json`, `n26.json`) and adjust
   the header mapping, `delimiter`, `decimal` (`comma`/`dot`), and `date_format`.
3. Re-ingest until it parses. Add a small fixture under `tests/fixtures/` if you can.
4. Open a PR describing the bank and country. Please use **synthetic** rows in fixtures — never
   real statement data or IBANs.

## Code style & invariants

- **Match the surrounding code.** No new frameworks, no build step — vanilla JS + native ES
  modules on the frontend, plain Python 3.12 on the backend.
- **Linting.** Python style is checked with [Ruff](https://docs.astral.sh/ruff/) (config in
  `ruff.toml`; a deliberately light default rule set). Run `ruff check` before a PR — e.g.
  `uvx ruff check` or `pipx run ruff check` if you don't have it installed. An `.editorconfig`
  keeps indentation consistent (4-space Python, 2-space everything else).
- **Money** uses integer cents (`pipeline/util.cents()` / the JS `cents()` mirror). Never compare
  money with float equality.
- **Escape user text** (`esc()`) anywhere it reaches `innerHTML`.
- **User-facing strings** go through `T(...)`; every key must exist in `app/static/i18n/de.js`
  (`tests/test_i18n.js` enforces this). Machine values (slugs, enums) stay English.
- **Never weaken a test to make it pass.** A red test means the code is wrong or the test found a
  real bug. The `MIN_CHECKS` counters and the real-data tripwire in `run-tests.sh` enforce this.
- **Never commit personal data.** `data/`, `rules/`, `config.json`, `inbox/`, `receipts/`,
  `backups/` are gitignored — keep it that way.

## Pull requests

- Keep PRs focused; Conventional-Commit-style messages are appreciated (`feat:`, `fix:`, `docs:`…).
- The full test suite (including the `test_oracle.py` accounting-math checker) must pass; CI runs
  it on every push and PR.
- By contributing, you agree your contributions are licensed under the project's [MIT](LICENSE)
  license.
