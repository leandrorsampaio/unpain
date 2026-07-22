# Trade Republic statement extractor

Deterministically converts a Trade Republic account-statement PDF into the
`generic-extracted` CSV consumed by the Family Accountability pipeline.

Requirements: Python 3.9+ and Poppler's `pdftotext` command (`brew install poppler`).

```bash
.venv/bin/python scripts/trade_republic/extract.py \
  "inbox/trade-republic-person1__2025.pdf"
```

Strict mode is the default: any questionable transaction prevents CSV output.
To keep rows whose amount is proven by the running balance but whose text is
incomplete, send those rows explicitly to the app's Review tab:

```bash
.venv/bin/python scripts/trade_republic/extract.py \
  --allow-review "inbox/trade-republic-person1__2025.pdf"
```

The CSV is written beside the PDF. A JSON log is always written beside the
requested output, including on failure. The source PDF is deliberately not
moved; normal inbox ingestion moves the generated CSV after it is accepted.

