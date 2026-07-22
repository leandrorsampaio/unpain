# Family Accountability — Project Context (START HERE)

> **For any LLM/agent working on this project:** read this file first, top to bottom, then
> [CLAUDE.md](CLAUDE.md) (technical guide), [AGENTS.md](AGENTS.md) (UI rules), and
> [README.md](README.md) (usage). This is the neutral, public version of the project's
> context document.

## 1. Who this is for

A self-hosted expense tracker for a **two-person household** (referred to as `person1` and
`person2`, or by whatever display names the users configure). One partner typically drives
setup from the terminal; the other uses only the **web UI** in a browser. Everything
user-facing is in **English**. The base currency is **EUR**; foreign-currency accounts are
supported via ECB reference rates.

## 2. The problem being solved

Couples who try expense apps or manual spreadsheets tend to fail the same way: too many
accounts, too much manual typing per month, they fall behind, months pile up, and they lose
sight of where the money goes and how much they save — and tax season becomes painful. The
goal is to make monthly accountability near-automatic: drop bank exports in a folder, review
a small queue in the browser, done. Nobody ever types the same merchant→category mapping twice.

## 3. Goals (in priority order)

1. Make monthly accountability near-automatic — drop bank exports in `inbox/`, review a small
   queue, done. Rules learn categorization so it approaches zero manual work over time.
2. Clear monthly and yearly picture: expenses by category, income, savings rate.
3. **Fairness**: split shared costs proportionally to income, equalize with a transfer.
4. Year-end **German tax evidence pack** (refund-relevant amounts per bucket, with evidence) —
   a bonus for German users; everyone else can ignore or reseed the tax layer.
5. Scales across years, keeps history, and helps identify where to cut costs without reducing
   quality of life (categories + budgets exist for this).

## 4. Core design philosophy (don't re-litigate)

- **Deterministic core, LLM only at the edges.** LLMs never write to the canonical data store.
  LLM output is JSON, schema-validated, admitted by deterministic code.
- **CSV first**: deterministic parsers for bank CSV exports; LLM PDF extraction only for
  PDF-only sources, gated by **balance reconciliation** (`opening + sum(txns) == closing` to
  the cent, or the data is rejected).
- **Rules before LLM**: `rules/merchant-rules.json` categorizes for free; the LLM only
  *proposes rules* for unseen merchants; humans confirm once in the UI; cost → zero over time.
- **Everything derived is recomputed, never stored** (settlement, dashboards, tax report).
  Changing a rule or decision recalculates all history automatically.
- **LLM-agnostic**: skills = markdown instructions + JSON Schema in `skills/`, runnable by any
  CLI agent. No API keys, no vendor lock-in. The app is fully usable without any LLM.

## 5. Domain decisions and their reasons

| Decision | Detail | Why |
|---|---|---|
| Settlement ratio | Income-proportional, **salary only** (`ratio_income: true` subcategories). Monthly = estimate; **binding true-up after Dec 31** on actual annual income. | Fairness is a year-level property, not per-expense. |
| Income owner | `person1 \| person2 \| couple` (couple = 50/50, e.g. tax refunds). Capital gains/dividends/gifts **never** count toward the ratio. | An explicit definition of income. |
| Sharing | Per transaction: `shared` (default) \| `personal:<person>` \| `out-of-scope`. Personal is excluded from equalization but still a household expense; `out-of-scope` is invisible to ALL math. | |
| Year costs | `year_cost: true`: excluded from the monthly picture, included annually. | A large one-off (e.g. an e-bike) shouldn't distort the month. |
| Reimbursements | Booked to the expense category they offset (signed sums net out). | Keeps category totals honest. |
| Splits | One bank transaction can split into parts that must sum exactly to the original (validator-enforced). Multi-purpose merchants (Amazon/PayPal) always go to review. | |
| Internal transfers | Marker texts plus conservative opposite-amount pair matching; includes the equalization transfer and credit-card settlements. | Otherwise transfers between own accounts double-count. |
| Rules scoping | `family` (default, all accounts incl. joint) vs per-person (only their accounts, **beats** family rules). Scope is decided by which account the money moved through. | The same merchant can mean different things per person. |
| Categories | Seeded, stable slugs; add anytime; never delete → `archived: true`; some groups accept dynamic subcategories (one per trip/project). | Flexibility was an explicit requirement. |
| Currency | Store original + ECB rate of the transaction date + EUR value; convert in code, never by LLM. | Recomputable under any tax convention. |
| Months | Can be **closed** (locked against edits, reopenable). | |

## 6. Working agreements

- **Be critical** — push back on weak ideas, don't just agree.
- **Write short** ("TLDR without losing precision"). No walls of text.
- **Commit + push after every finished feature** (Conventional-ish messages).
- **Personal data never goes to git**: `data/`, `rules/`, `config.json`, `inbox/`,
  `receipts/`, `backups/` are gitignored. Sanitized templates live in `examples/`; test
  fixtures are generic.
- Verify UI changes in a real browser (Playwright, screenshots) — users notice when things
  aren't there.
- Token frugality is a stated nice-to-have: prefer deterministic solutions.

## 7. File map

| Path | What |
|---|---|
| `PROJECT.md` | This file — the neutral project entry point. |
| `CLAUDE.md` | Technical agent guide: architecture, commands, invariants. |
| `AGENTS.md` | UI rules: one concept → one component, escape/`cents()` discipline. |
| `PLAN.md` | The original design document (model still accurate). |
| `README.md` | Human usage: setup, monthly workflow, CLI. |
| `CONTRIBUTING.md` | How to set up a dev environment, run tests, and add a bank format. |
| `SECURITY.md` | Threat model (no auth by design) and how to report issues. |
| `pipeline/` | Deterministic Python (3.12). Formats in `pipeline/formats/*.json`. |
| `app/` | FastAPI server + vanilla-JS UI + vendored Material assets. |
| `skills/` | LLM skill instructions + output schemas. |
| `examples/` | Sanitized config/data templates (generic person1/person2). |
| `tests/` | Regression tests + generic fixtures. |
| `start.sh` | Cross-platform launcher (creates the venv on first run). |
