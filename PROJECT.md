# UnPAIN — Project Context (START HERE)

> UnPAIN stands for **Un**necessarily **P**recise **A**ccounting & **I**ncome **N**avigator.
>
> **For any LLM or agent on this project:** read this file first, top to bottom. Then read
> [CLAUDE.md](CLAUDE.md) (technical guide), [AGENTS.md](AGENTS.md) (UI rules), and
> [README.md](README.md) (usage). This is the neutral, public version of the context document.

## 1. Who this is for

UnPAIN is a self-hosted expense tracker for a **two-person household**. The two people are
`person1` and `person2`, or the display names that the users configure. One partner usually drives
setup from the terminal. The other uses only the **web UI** in a browser. Everything user-facing is
in **English**. The base currency is **EUR**. UnPAIN supports foreign-currency accounts through ECB
reference rates.

## 2. The problem this solves

Couples who try expense apps or manual spreadsheets fail the same way. There are too many accounts
and too much manual typing each month. They fall behind. Months pile up. They lose sight of where
the money goes and how much they save. Tax season becomes painful.

UnPAIN makes monthly accountability near-automatic. You drop bank exports in a folder. You review a
small queue in the browser. You are done. Nobody types the same merchant-to-category mapping twice.

## 3. Goals (in priority order)

1. Make monthly accountability near-automatic. You drop bank exports in `inbox/` and review a small
   queue. Rules learn the categorization, so manual work approaches zero over time.
2. Show a clear monthly and yearly picture: expenses by category, income, and savings rate.
3. **Fairness.** Split shared costs in proportion to income. Equalize with a transfer.
4. Build a year-end **German tax evidence pack** (refund-relevant amounts per bucket, with
   evidence). This is a bonus for German users. Other users ignore or reseed the tax layer.
5. Scale across years, keep history, and help find where to cut costs without a lower quality of
   life. Categories and budgets exist for this.

## 4. Core design philosophy (do not re-litigate)

- **Deterministic core, LLM only at the edges.** LLMs never write to the canonical data store. LLM
  output is JSON. Deterministic code validates the schema and admits it.
- **CSV first.** Deterministic parsers read bank CSV exports. LLM PDF extraction applies only to
  PDF-only sources. A **balance reconciliation** gates it (`opening + sum(txns) == closing` to the
  cent, or UnPAIN rejects the data).
- **Rules before LLM.** `rules/merchant-rules.json` categorizes for free. The LLM only *proposes
  rules* for unseen merchants. A human confirms once in the UI. The cost goes to zero over time.
- **Recompute everything derived; never store it** (settlement, dashboards, tax report). A changed
  rule or decision recalculates all history automatically.
- **LLM-agnostic.** A skill is a markdown instruction plus a JSON Schema in `skills/`. Any CLI agent
  runs it. There are no API keys and no vendor lock-in. The app is fully usable without any LLM.

## 5. Domain decisions and their reasons

| Decision | Detail | Why |
|---|---|---|
| **What this ledger measures** | **Consumption.** Money the household spent or received to live on. Deliberately outside it: investment activity (securities purchases, dividends, interest, Saveback), gifts received, and money that predates the tracked period. All are marked `out-of-scope` and are invisible to every total. | The question being answered is "what did we spend, and is the split fair" — not "how much richer did we get". Consequence to state plainly: **the savings figure is cash not consumed, not wealth growth.** Actual net worth grows by more. |
| Settlement ratio | Income-proportional, **salary only** (`ratio_income: true` subcategories). Monthly = estimate; **binding true-up after Dec 31** on actual annual income. | Fairness is a year-level property, not per-expense. |
| Income owner | `person1 \| person2 \| couple` (couple = 50/50, e.g. tax refunds). Capital gains/dividends/gifts **never** count toward the ratio. | An explicit definition of income. |
| Sharing | Per transaction: `shared` (default) \| `personal:<person>` \| `out-of-scope`. Personal is excluded from equalization but still a household expense; `out-of-scope` is invisible to ALL math. | |
| Year costs | `year_cost: true`: excluded from the monthly picture, included annually. | A large one-off (e.g. an e-bike) shouldn't distort the month. |
| Reimbursements | Booked to the expense category they offset (signed sums net out). | Keeps category totals honest. |
| Splits | One bank transaction can split into parts that must sum exactly to the original (validator-enforced). Multi-purpose merchants (Amazon/PayPal) always go to review. | |
| Internal transfers | Marker texts plus conservative opposite-amount pair matching; includes the equalization transfer and credit-card settlements. Every detection waits in the review queue for a human yes or no — it stays out of the totals meanwhile, so nothing swings on an unanswered question. | Otherwise transfers between own accounts double-count. But excluding money is the one thing the app does on its own that the totals depend on, so it has to be visible and answerable rather than silent. |
| Rules scoping | `family` (default, all accounts incl. joint) vs per-person (only their accounts, **beats** family rules). Scope is decided by which account the money moved through. | The same merchant can mean different things per person. |
| Categories | Seeded, stable slugs; add anytime; never delete → `archived: true`; some groups accept dynamic subcategories (one per trip/project). | Flexibility was an explicit requirement. |
| Currency | Store original + ECB rate of the transaction date + EUR value; convert in code, never by LLM. | Recomputable under any tax convention. |
| Months | Can be **closed** (locked against edits, reopenable). | |

## 6. Working agreements

- **Be critical.** Push back on weak ideas. Do not just agree.
- **Write short** ("TLDR without losing precision"). Do not write walls of text.
- **Commit and push after every finished feature** (Conventional-ish messages).
- **Personal data never goes to git.** `data/`, `rules/`, `config.json`, `inbox/`, `receipts/`, and
  `backups/` are gitignored. Sanitized templates live in `examples/`. Test fixtures are generic.
- Verify UI changes in a real browser (Playwright, screenshots). Users notice when things are not
  there.
- Token frugality is a stated nice-to-have. Prefer deterministic solutions.

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
