# Skill: propose-rules

You are proposing categorization rules for merchants the rules engine does not know yet.
You do NOT categorize individual transactions — you propose durable RULES that a human confirms.

## Input
Run `.venv/bin/python -m pipeline.cli status` to find years with items needing review, then inspect
the queue: `curl -s "localhost:8765/api/review?year=<Y>"` (or read `data/<Y>/` directly).
Categories live in `rules/categories.json`; existing rules in `rules/merchant-rules.json`.

## Procedure
1. Group the needs_review transactions by merchant (counterparty).
2. For each merchant you can identify with high confidence (a supermarket chain, a known
   subscription, a Krankenkasse...), propose one rule: a stable substring pattern + category +
   default sharing.
3. Output JSON matching `output.schema.json`. Confidence:
   - `high` — unmistakable (REWE is a supermarket). These get pre-applied pending one-click confirm.
   - `low` — plausible guess. These are shown as suggestions only.
   - Skip merchants you cannot identify; a human will handle them.
4. Do NOT write to `rules/merchant-rules.json` yourself — the review UI persists confirmed rules.

## Rules
- Patterns must be specific enough not to over-match ("RE" would match everything; "REWE" is fine).
- Multi-purpose merchants (Amazon, PayPal, big department stores) get `action: "review"`, never a category.
- Sharing default is "shared" unless the merchant is obviously personal (e.g. a men's barber -> personal).
- The valid person slugs are exactly the entries of the `people` array in the project's `config.json` — read it before proposing; never invent person names.
- Income: salary payers get category `to-receive/salary` and the account owner as income_owner.
