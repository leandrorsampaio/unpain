# Skill: extract-statement

You are extracting transactions from a bank/broker statement PDF that has no CSV export
(e.g. Trade Republic, Deutsche Bank credit card). Follow this procedure exactly.

## Input
A PDF in `inbox/` named `<account-id>__<anything>.pdf`, where `<account-id>` exists in `data/accounts.json`.

## Procedure
1. Read the PDF. Identify: the statement period, the **opening balance**, the **closing balance**, and every transaction line.
2. Extract each transaction: booking date (ISO `YYYY-MM-DD`), signed amount (negative = money out; be careful with German number format `1.234,56`), currency, counterparty name, purpose/reference text, counterparty IBAN if printed.
3. **Reconcile (mandatory gate):** `opening_balance + sum(all amounts)` must equal `closing_balance` to the cent.
   - If it matches: proceed.
   - If it does not match: DO NOT write any output file. Report the discrepancy, list the pages/lines you are unsure about, and stop. A human decides.
   - **If the document prints no balances** (e.g. a credit card listing): there is nothing to
     reconcile against, so this skill cannot admit it. Do not write the CSV. Report what you found
     and stop. Extracting anyway and calling the result "unreconciled" put unproven numbers into the
     ledger with a label nobody downstream reads. A human can enter such a document by hand, or add
     a deterministic extractor for it under `scripts/`.
4. Write the result as CSV to `inbox/<account-id>__<original-name>.extracted.csv` with this exact
   header: `date,amount,currency,counterparty,purpose,counterparty_iban`
   (dot decimal, ISO dates, UTF-8). This is the `generic-extracted` format the pipeline ingests.
   **The `.extracted` suffix is required** — it is what tells the pipeline these numbers were read
   out of a document rather than exported by a bank, and therefore owe it a reconciliation.
5. Write the reconciliation you just did to
   `inbox/<account-id>__<original-name>.extracted.report.json` beside the CSV. **The pipeline
   refuses an `.extracted.csv` that arrives without it** and re-does the arithmetic itself over the
   CSV — your word for it is not the gate, this file is what it checks:

   ```json
   {
     "status": "ok",
     "opening_balance": 1234.56,
     "closing_balance": 987.65,
     "sum_of_transactions": -246.91,
     "transactions_extracted": 42,
     "period": "2026-03-01 - 2026-03-31"
   }
   ```

   The balances are the statement's own printed figures, in the statement's currency. If your
   numbers do not survive `opening + sum == closing` to the cent, the import is refused — which is
   the point. Never adjust a figure to make it balance.
6. Move the source PDF to `inbox/processed/`.
7. Print a short report: period, number of transactions, reconciliation status (`opening X + sum Y = closing Z ✓`).

## Rules
- Never guess an amount. If a line is ambiguous, exclude it, fail reconciliation, and report it.
- Never convert currencies — output the original amount and currency; the pipeline converts.
- Never categorize here. Extraction only.
- Trade Republic: interest payments and dividends are transactions; individual trades (buy/sell) are too, but card transactions and trades keep their own lines. Extract everything; the pipeline filters.
