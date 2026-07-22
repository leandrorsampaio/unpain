# Deterministic statement extractors

Each PDF extractor lives in its own folder and is discovered automatically by
the Ingest page when that folder contains `extractor.json`.

Required manifest fields:

```json
{
  "id": "stable-extractor-id",
  "label": "Name shown in the UI",
  "description": "Statement layout handled by this extractor",
  "module": "scripts.folder_name.extract",
  "callable": "extract_pdf",
  "account_bank_contains": "optional bank-name safety check"
}
```

The callable receives `(pdf_path, output_csv_path, allow_review=False)` and
returns the reconciliation report used by the Ingest workflow. Folders without
an `extractor.json` are ignored.

