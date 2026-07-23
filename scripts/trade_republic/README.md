# Trade Republic statement extractor

This extractor deterministically converts a Trade Republic account-statement PDF into the
`generic-extracted` CSV that the UnPain pipeline consumes.

Requirements: Python 3.9+ and Poppler's `pdftotext` command (`brew install poppler`).

```bash
.venv/bin/python scripts/trade_republic/extract.py \
  "inbox/trade-republic-person1__2025.pdf"
```

Strict mode is the default. Any questionable transaction stops the CSV output. Some rows have an
amount that the running balance proves, but incomplete text. To keep those rows, send them to the
app's Review tab with `--allow-review`:

```bash
.venv/bin/python scripts/trade_republic/extract.py \
  --allow-review "inbox/trade-republic-person1__2025.pdf"
```

The script writes the CSV beside the PDF. It always writes a JSON log beside the requested output,
also on failure. The script does not move the source PDF. Normal inbox ingestion moves the generated
CSV after it accepts it.

