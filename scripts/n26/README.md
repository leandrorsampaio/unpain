# N26 statement extractor

Deterministically converts an N26 account statement (*Kontoauszug*, German) PDF into the
`generic-extracted` CSV that the UnPAIN pipeline consumes.

Requirements: Python 3.9+ and Poppler's `pdftotext` command (`brew install poppler`).

```bash
.venv/bin/python scripts/n26/extract.py "inbox/n26-person1__01.2025.pdf"
```

Strict mode is the default: any questionable row stops the CSV output entirely. To send
questionable rows to the app's Review tab instead:

```bash
.venv/bin/python scripts/n26/extract.py --allow-review "inbox/n26-person1__01.2025.pdf"
```

## Two templates, one bank

Personal and joint (*Gemeinschaftskonto*) statements are the same bank with different
templates, and the differences are all load-bearing:

| | Personal | Joint ("N26 Family") |
|---|---|---|
| Summary page title | `Zusammenfassung` | `Übersicht` |
| Extra summary line | — | `Davon Gebühren` |
| `Verbuchungsdatum` column | x=342 | x=359 |
| Description reaches | x=297 | x=348 |
| Footer starts at | y=762 | y=702 |

Because of that, columns are **calibrated per page from its own header** rather than
hardcoded, and a block ends at its own `Wertstellung` line rather than at a fixed footer
offset. A split tuned to the personal layout truncates joint descriptions; a footer offset
tuned to the personal layout pulls the address block and the account's own IBAN into the
last transaction on every joint page.

## The reconciliation gate

N26 ships a summary page, and it is the gate. Two things must both hold:

- the summary is internally consistent —
  `alter Kontostand + Einkommende − Ausgehende == neuer Kontostand`
- the extracted rows explain the move — `sum(transactions) == neuer − alter`

A missed or misread row breaks the second, so it fails the statement rather than landing
silently in the books. There is no running balance per row, unlike Trade Republic or Banco
Rendimento, so this pair of totals is the only check available — which is why both are enforced.

## Layout notes

```
Beschreibung                        Verbuchungsdatum      Betrag
Max Mustermann                     08.01.2025     -50,00€
Belastungen                                              <- category
IBAN: DE11... • BIC: BANKDEFFXXX                         <- counterparty IBAN
Sent from N26                                            <- reference text
Wertstellung 08.01.2025                                  <- closes the block
```

- A transaction is a **variable-height block**: the amount line, then any combination of
  category, counterparty IBAN and free text. Real statements contain blocks with all three,
  with free text only, and with nothing but the amount line.
- The counterparty name sits about **2pt above** its own date and amount (different baseline),
  so a row is matched with a small vertical band rather than an exact `top`.
- `Wertstellung` is the value date and is dropped; the booking date (*Verbuchungsdatum*) is
  authoritative, matching the convention used for Deutsche Bank giro.
- Amounts carry an explicit sign and a euro suffix (`-4.475,00€`), and are right-aligned, so a
  wide amount starts further left (observed 490–519 points against dates at 390–396).

## Months with no activity are normal

N26 issues a statement for a month even when nothing happened in it: the summary reads zero
across the board and there are no rows at all. These are common, and extract cleanly as zero
transactions and a header-only CSV, which the ingest gate accepts. Do not treat "no rows" as a
failure for this bank.

Verified against twenty-four real 2025 statements — twelve personal and twelve joint: 39
transactions, every statement reconciling to the cent, and the closing balance of each month
matching the opening balance of the next in both series.
