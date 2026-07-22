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
   - If the document has no balances (e.g. a credit card listing): say so explicitly, extract anyway, and flag the output as unreconciled.
4. Write the result as CSV to `inbox/<account-id>__<original-name>.csv` with this exact header:
   `date,amount,currency,counterparty,purpose,counterparty_iban`
   (dot decimal, ISO dates, UTF-8). This is the `generic-extracted` format the pipeline ingests.
5. Move the source PDF to `inbox/processed/`.
6. Print a short report: period, number of transactions, reconciliation status (`opening X + sum Y = closing Z ✓`).

## Rules
- Never guess an amount. If a line is ambiguous, exclude it, fail reconciliation, and report it.
- Never convert currencies — output the original amount and currency; the pipeline converts.
- Never categorize here. Extraction only.
- Trade Republic: interest payments and dividends are transactions; individual trades (buy/sell) are too, but card transactions and trades keep their own lines. Extract everything; the pipeline filters.
