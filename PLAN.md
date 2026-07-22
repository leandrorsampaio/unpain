# FamilyAccountability — Design Plan

Family expense accountability for a two-person household. Germany-based, multi-currency (EUR/BRL/USD), tax-aware, income-proportional fairness settlement.

**Status: M1+M2 built (pipeline, review UI, dashboards, settlement, tax report). See README.md for usage.**

## Goals

1. Near-zero manual labour: drop bank exports in a folder, review a small queue, done.
2. Clear monthly + yearly picture of expenses, income, and savings rate.
3. Fair settlement between partners, proportional to annual income.
4. Year-end German tax export (amounts per tax bucket per person, with evidence).
5. Scales across years; historical comparison comes free.
6. LLM-agnostic and token-frugal: deterministic core, LLM only at the edges.

## Core principles

- **CSV first.** Deterministic parsers for every source that offers CSV/Excel export. LLM PDF extraction only for PDF-only sources (Trade Republic, DB credit card).
- **Reconciliation gate.** Any LLM-extracted statement must satisfy `opening balance + sum(transactions) == closing balance` before entering the store. No exceptions.
- **Rules before LLM.** `merchant-rules.json` categorizes deterministically. The LLM only proposes *new rules* for unseen merchants; a confirmed rule works forever. Token cost trends to zero over time.
- **LLM never writes to the canonical store.** LLM output is JSON validated against a schema, then admitted by deterministic code.
- **Everything derived is recomputed, never stored** (settlement, dashboards, ratios). Source data + decisions are the only state.
- **English everywhere** (UI, categories, docs).

## Accounts

| Owner | Source | Export | Ingestion |
|---|---|---|---|
| person1 | Deutsche Bank giro | CSV | parser |
| person1 | Deutsche Bank credit card | PDF (likely) | LLM skill |
| person1 | Trade Republic | PDF only | LLM skill |
| person1 | Nubank (BRL) | CSV/OFX | parser |
| person2 | DKB | CSV | parser |
| person2 | Volksbank | CSV | parser |
| person2 | Trade Republic | PDF only | LLM skill |
| person2 | Barclays | CSV/Excel | parser |
| couple | N26 ×3 (one each + joint) | CSV | parser |
| — | Cash | `inbox/cash.csv` + UI form | parser |

`accounts.json` registers every account and enables automatic `paid_by`. Internal transfers are
detected through configured marker text and conservative opposite-amount pair matching
(credit-card payments, checking→savings, the equalization transfer itself — none count as income or expense).

Trade Republic stays minimal: transfers in/out + interest; individual trades tracked but out of expense math (capital gains never count as income for the ratio).

## Folder layout

```
FamilyAccountability/
├── PLAN.md
├── inbox/                    # drop exports here; consumed by ingest
│   └── cash.csv
├── data/
│   ├── accounts.json
│   ├── 2024/ 2025/ 2026/
│   │   ├── transactions/     # normalized JSONL, one file per source statement
│   │   ├── decisions.json    # manual review outcomes (splits, overrides)
│   │   └── months.json       # month state: open | closed
├── rules/
│   ├── categories.json
│   ├── merchant-rules.json   # the learned brain; grows via confirmations
│   └── tax-buckets.json
├── skills/                   # LLM instructions: markdown + JSON Schema
│   ├── extract-statement/    # PDF → transactions JSON (+ balances for reconciliation)
│   ├── propose-rules/        # unseen merchants → suggested rules w/ confidence
│   └── review-month/         # anomaly reviewer (2nd LLM)
├── pipeline/                 # deterministic Python
├── app/                      # local web app: review UI + dashboards
└── receipts/2026/...         # optional tax evidence files, linked by transaction id
```

## Transaction schema

```json
{
  "id": "sha256(account,date,amount,counterparty,ref)",
  "account": "db-giro-person1",
  "date": "2026-03-14",
  "amount": {"original": -89.00, "currency": "EUR", "eur": -89.00, "rate": null, "rate_date": null},
  "counterparty": "AMAZON EU S.A R.L.",
  "purpose": "raw bank reference text",
  "kind": "expense | income | internal-transfer",
  "category": "core-living/groceries",
  "sharing": "shared | personal:person1 | personal:person2 | out-of-scope",
  "income_owner": "person1 | person2 | couple",
  "splits": [ {"amount": -40.00, "category": "...", "sharing": "..."} ],
  "tax": {"bucket": "sonderausgaben/donations", "receipt": "receipts/2026/x.pdf"},
  "status": "rule-matched | confirmed | needs_review",
  "source": {"file": "inbox/dkb-2026-03.csv", "line": 42}
}
```

- `paid_by` derives from `account` via `accounts.json` — never entered.
- `splits` must sum exactly to `amount` (validator-enforced). Used for e.g. Amazon multi-purpose orders.
- Non-EUR: converted with ECB reference rate of transaction date (previous business day on weekends); original always kept, so any other convention can be recomputed later.
- `income_owner: couple` (tax refunds, joint gifts) counts 50/50 to each side.
- `out-of-scope` transactions are kept and reconciled but excluded from all expense/settlement/savings math.

## Categories

Seeded from the existing Excel (spelling normalized). Stable slugs; display names editable; add anytime; remove = archive (history stays valid). Months can be closed/locked; category changes never blocked within an open year.

- **to-receive** (income): salary, salary-extras, reimbursement, freelance, sold-items, gifts
- **living-costs**: cold-rent, nebenkosten, energy, maintenance, home-insurance, radio-tax, internet
- **living-upgrades**: furniture, appliances, deco
- **core-living**: groceries, clothing, cellphone, insurances, service-fees, makeup, items-up-to-50, items-over-50, other-taxes
- **transport**: public-transport, fuel, car-insurance, car-maintenance, car-tax, bike
- **health**: psychotherapy, doctors, therapy, medicine
- **sports**: gym, equipment, other
- **studying**: courses, materials
- **recreation**: restaurants, hobbies, going-out, streaming
- **donations**: church, people, ngo
- **gifts**: family, friends, us
- **traveling**: *dynamic — one subcategory per trip (e.g. `japan-2026`)*
- **projects**: *dynamic — one subcategory per project*

Dropped from the old "Other" category (replaced by system features):
- *Not Accountable* → the `out-of-scope` sharing flag
- *50% Costs* → **open question** (see bottom)
- *Year Costs* → **open question** (see bottom)
- *Items up to/over 50* → auto-assigned by amount threshold, not by human/LLM

## Categorization flow

1. **Rules pass** (deterministic, free): `merchant-rules.json` patterns → category + default sharing + tax bucket. Handles the 20 REWE buys, the gym ×12/year, etc. with zero typing.
2. **LLM pass** (only unmatched merchants): proposes a *rule* with confidence. High confidence → pre-applied, flagged for one-click confirm. Low → review queue with suggestion.
3. **Review UI**: confirm/override/split. Every confirmation persists the rule → automatic forever.
4. **Anomaly reviewer** (2nd LLM, monthly, optional): reads the finished month, flags judgment-level issues — unusual amounts ("REWE €480 ≈ 6× normal"), duplicate charges, uncategorized recurring payments. Never re-does arithmetic (reconciliation already proved it).

Merchants like Amazon get a rule: "always → review queue" (multi-purpose orders need human split).

## Settlement (fairness)

- **Ratio** = income-proportional. Income for the ratio = **salary + salary-extras (bonus) only**. No capital gains, dividends, gifts. `couple` income splits 50/50 to each side. Year = calendar year (Jan 1 – Dec 31).
- **Two-tier**: monthly settlement is an *estimate* using a configured reference ratio; the **binding true-up runs after Dec 31** on actual annual income.
- Math (deterministic, recomputed on demand):
  `fair_share(person) = ratio(person) × total shared expenses`
  `transfer = fair_share − actually_paid`
- Personal and out-of-scope expenses are excluded. The equalization transfer itself is an internal transfer.

## Tax (Germany)

Not a tax declaration — a **year-end evidence pack**: every refund-relevant amount summed per bucket per person, transaction list + linked receipts behind each number. Ready for ELSTER typing or a Steuerberater.

Bucket seed (in `tax-buckets.json`, category-mapped where possible, per-transaction override):
- **Werbungskosten** — work equipment, commute, training (studying/courses when job-related)
- **Sonderausgaben** — donations (church/NGO), insurance/Vorsorge
- **Außergewöhnliche Belastungen** — medical (health/*)
- **§35a haushaltsnahe Dienstleistungen / Handwerkerleistungen** — cleaning, repairs, maintenance (bank transfer *is* the required evidence)

## Review UI + dashboards

Small local web server (Python FastAPI, single process), browser UI on the home network. English.

- **Review queue**: bulk confirm ("214 transactions match new rule REWE→groceries — confirm all"), per-item category/sharing/tax pick, transaction splitting, cash entry form.
- **Dashboards**: monthly & yearly by category, income vs expenses, savings rate, settlement estimate, year-over-year comparison, tax bucket running totals.
- **Month close**: lock a month after review; closed months immutable unless reopened.

## LLM integration (agnostic)

A "skill" = `instructions.md` + `output.schema.json` + validator. Any CLI agent (Claude Code, Gemini CLI) runs it: read instructions, produce JSON, validated deterministically, retried on schema failure. No API keys required — runs inside existing agent subscriptions. Swapping LLMs = running the same skill in a different CLI.

## Milestones

**M1 — Backlog engine (core value)**
Accounts registry, CSV parsers (DB giro, DKB, Volksbank, N26, Barclays, Nubank, cash), normalized store + dedupe, internal-transfer detection, rules engine, currency conversion (ECB), batch processing of the Jan 2024→now backlog.

**M2 — Review UI**
FastAPI app: review queue, bulk confirm, splits, cash form, month close. LLM skill: propose-rules. Process the full backlog through review.

**M3 — Dashboards + settlement**
Monthly/yearly dashboards, savings rate, settlement estimate + annual true-up. Retroactive 2024/2025 settlement.

**M4 — PDF extraction + tax**
extract-statement skill (Trade Republic, DB credit card) with reconciliation gate; tax buckets + year-end evidence pack; anomaly-reviewer skill.

2024 = the validation year: full real data through the whole pipeline before trusting outputs.

## Resolved questions

1. **"Year Costs"** → per-transaction `year_cost` flag: excluded from the monthly picture (an
   e-bike shouldn't distort June), included in the annual totals. **"50% Costs"** → dropped.
2. Settlement ratio income = **salary only** (`ratio_income: true` in categories.json — editable).
3. **Reimbursements** offset the original expense category (signed sums handle it naturally).
4. Tech stack: **Python + FastAPI + vanilla JS**, Tailwind with a Material-Design-light look
   (validated chart palette: #1a73e8 / #e37400, status #188038 / #b3261e).
