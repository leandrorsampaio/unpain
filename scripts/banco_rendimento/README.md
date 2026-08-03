# Banco Rendimento statement extractor

Deterministically converts a Banco Rendimento account statement (*extrato*) PDF into the
`generic-extracted` CSV that the UnPAIN pipeline consumes. Amounts stay in **BRL**; the
pipeline converts them at the ECB rate of each transaction date on ingest.

Requirements: Python 3.9+ and Poppler's `pdftotext` command (`brew install poppler`).

```bash
.venv/bin/python scripts/banco_rendimento/extract.py \
  "inbox/rendimento-person1__2025.pdf"
```

Strict mode is the default: any questionable row stops the CSV output entirely. Rows whose
amount the running balance proves, but whose text is incomplete, can be sent to the app's
Review tab instead:

```bash
.venv/bin/python scripts/banco_rendimento/extract.py \
  --allow-review "inbox/rendimento-person1__2025.pdf"
```

## The reconciliation gate

The statement prints a running balance on every row, so the balance chain *is* the gate.
Walked oldest to newest, each printed balance must equal the previous one plus the printed
amount. The opening balance is derived from the oldest row (`balance − amount`), and the
closing balance is the newest row's. A single break fails the whole statement.

There is no printed opening balance to check against, and the trailing `Saldo Atual` block is
the balance on the *print date*, not the end of the period — on a real 2025 statement the
period ends at R$ 1.280,00 while `Saldo Atual` reads R$ 1.450,00. Neither is used as an anchor.

## Layout notes

```
Documento    Lançamento                          Valor (R$)      Saldo
29/12/2025                                     <- date section header
1234567      Rec Pgto Pix Cp :...MARIA MUSTERMANN      100,00     1.100,00
             Saldo Final                                     1.280,00
```

Three things this layout does that the parser has to handle:

- **Date sections run newest-first, rows inside a date run oldest-first.** Sorting by date
  ascending while keeping document order within a date reproduces the true sequence.
- **A date section can be split by a page break**, leaving rows at the top of the next page
  whose header is on the page before. The current date carries across pages.
- **Long descriptions wrap onto the lines above and below their own amount.** Rows are cut on
  the Saldo column rather than on text lines, and words are read line-by-line then
  left-to-right — sorting on `left` alone puts the continuation line first.

Both money columns are right-aligned, so a wide value starts further left: Valor spans
446–464 points and Saldo 495–512. The column split sits in that gap at 480.

Verified against a real 6-page statement covering 01/01/2025–31/12/2025: 48 transactions,
37 date sections, chain reconciles to the cent.
