---
name: New bank format
about: Request support for a bank whose CSV export isn't recognized
title: "[format] <bank name> (<country>)"
labels: bank-format
---

**Bank & country**
e.g. "ING (Germany)", "Revolut (EU)".

**Export type**
CSV / Excel / PDF-only. (PDF-only sources need an extractor, not just a format file.)

**Headers the ingest error printed**
When you drop the file in `inbox/` and click Ingest, the error prints the exact column headers
it saw. Paste that line here (it's just column names, no personal data):

```
<paste the headers here>
```

**Format details (if you know them)**
- Delimiter: `,` / `;` / tab
- Decimal style: `1234.56` (dot) or `1234,56` (comma)
- Date format: e.g. `DD.MM.YYYY`, `YYYY-MM-DD`
- Which columns are: date / counterparty / purpose / amount / currency / IBAN

**A few synthetic sample rows (optional but very helpful)**
Make up fake values — **do not paste real transactions**.

> Tip: adding a bank is usually just a new JSON file in `pipeline/formats/`. See
> [CONTRIBUTING.md](../../CONTRIBUTING.md) — PRs welcome!
