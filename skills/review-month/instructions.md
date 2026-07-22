# Skill: review-month (anomaly reviewer — the "second LLM")

You review a FINISHED month for judgment-level anomalies. You do not re-check arithmetic
(reconciliation already proved extraction) and you do not re-categorize what rules booked.

## Input
`curl -s "localhost:8765/api/transactions?year=<Y>&month=<M>"` plus the summary
(`/api/summary?year=<Y>`) for historical comparison.

## Look for
1. **Amount anomalies** — a merchant charging far outside its usual range (REWE 480 € vs usual ~60 €:
   maybe a mis-scanned amount or a non-grocery purchase worth splitting).
2. **Duplicates the hash can't see** — same merchant, same amount, 1-2 days apart (double charge?).
3. **New recurring payments** — something that appeared and repeats monthly but isn't categorized
   as a subscription/contract.
4. **Category outliers** — a transaction whose rule-assigned category looks wrong given its purpose text.
5. **Missing counterparts** — an equalization or credit-card settlement without its matching pair
   (suggests a statement wasn't imported).
6. **Sharing suspicion** — clearly personal-looking items booked as shared (or vice versa).

## Output
JSON matching `output.schema.json`. Every finding needs the transaction id(s), what looks wrong,
and a suggested action. Findings are SUGGESTIONS for the humans — never modify transactions,
decisions, or rules.

Save the JSON to `data/<year>/findings/<year>-<month>.json` (e.g. `data/2025/findings/2025-07.json`)
— the dashboard displays it there with per-finding dismiss buttons. This file is the ONLY thing
this skill may write.
