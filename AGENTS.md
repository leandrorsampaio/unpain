# AGENTS.md — rules every LLM must follow on this repo

This file exists because the same architectural mistake kept getting made: **UI logic gets
copy-pasted across pages, so a fix on one screen doesn't reach the others.** Read this before
touching `app/static/`. It complements [CLAUDE.md](CLAUDE.md) (technical guide) and
[PROJECT.md](PROJECT.md) (context).

---

## The prime directive

> **One concept → one component → used everywhere. Never a second copy.**

If you are about to write markup or a handler for something that already exists elsewhere
(a category picker, tax picker, note editor, owner selector, year-cost switch, modal,
transaction row, money formatting), **stop and reuse the existing component.** If it doesn't
exist as a single reusable unit yet, create it once in `app/static/components/` and use it from
all sites — do not inline a fresh copy "just for this page."

### The test that catches the mistake
Before finishing any UI change, ask:
> "If the user later wants this control changed, will it take an edit in **one** file?"

If the answer is "no, I'd have to edit it in two places," you have created the exact problem
this repo is trying to kill. Go back and extract a component.

---

## Hard rules for `app/static/`

0. **Material Design 3 everywhere.** This app is built on Google Material Design. Always use
   the vendored `@material/web` components (`md-*`) and the M3 design tokens
   (`var(--md-sys-color-*)` and the app aliases `--ink`, `--ink2`, `--line`, `--primary`,
   `--good`, `--bad`, …). Never hardcode hex colors, ad-hoc pixel spacing, or roll a custom
   control where an `md-*` component exists (buttons, switches, tabs, icons, selects). New UI
   must look and behave like M3 and adapt to light/dark via tokens.
1. **No framework, no build step.** Vanilla JS + native ES modules (`<script type="module">`).
   No React/Vue/Svelte/bundler/TypeScript. (PROJECT.md: offline-first, no vendor lock-in.)
2. **Reusable controls go through their component's interface.** Render via
   `Component.html(props)`; read the value via `Component.read(root)`. Never hand-build a
   control's HTML in a view, and never read a control by an ad-hoc id like `$('#e-cat-'+i)`.
3. **One modal.** Use `components/modal.js`. Do not hand-roll another
   `cat-backdrop`/`generic-modal` with its own close/stop-propagation logic.
4. **Escape user text.** Any bank/merchant/purpose/note/user string interpolated into
   `innerHTML` must pass through `esc()` from `core/`. Assume it contains `<`, `"`, `&`.
   **Category and subcategory names count** — they are free text a person types, and a name is
   plain text on the way out of `catName()` on purpose (chart labels are drawn to a canvas), so
   the escaping belongs at each HTML sink. **Icons are different**: every icon goes through the one
   `safeIcon()` validator rather than being escaped per sink, because a Material Symbol name is the
   only legal value and the twelfth sink is the one that gets forgotten.
   `tests/test_escaping_ui.js` fails the build if either slips.
5. **Money uses the shared `cents()`** (`Math.round(Number(x)*100)`), matching
   `pipeline/util.cents()`. Never compare money with float `===`; never sum floats then round.
6. **State by id, not by array index.** Prefer `state.txnById(id)` and event delegation over
   `window._txns[i]` + `onclick="fn(${i})"`. Index handles go stale and desync.
7. **Don't fan out fetches.** A tab should fetch only the data it needs.

### Typography roles

Application text uses the shared Roboto family and the six semantic roles in `app/static/app.css`:
`type-caption`, `type-label`, `type-body-small`, `type-body`, `type-title`, and
`type-headline`. Semantic component selectors such as headings and stat tiles map to the same
tokens. Choose by meaning and hierarchy, not by the pixel size you want. Weight/style changes
use only `font-regular`, `font-medium`, `font-bold`, and `font-italic`. Do not reintroduce size
utilities (`text-xs`, `text-sm`, etc.) or inline text sizes. Icon sizing is separate from typography.

## Hard rules for the pipeline / backend (see CLAUDE.md for the full list)

- LLMs never write to `data/` directly — extraction goes through `inbox/` + the reconciliation
  gate in `pipeline/extraction.py`, which re-does the arithmetic rather than trusting the
  extractor's `status`. Everything derived (settlement, dashboards, tax) is recomputed on read,
  never stored, and settlement is integer cents that must add back to the shared total.
- Run `./run-tests.sh --fast` after **any** change under `pipeline/` or `app/server.py`.
- Run the read-only integrity audit with `.venv/bin/python -m pipeline.cli doctor [year]`.
- Categories: never delete, set `archived: true`. Slugs are stable ids.
- Closed months reject decisions (HTTP 409). `out-of-scope` and internal transfers are
  invisible to all math.

---

## Components that already exist — reuse, don't recreate

All live in the `SHARED FIELD COMPONENTS` section near the top of `app/static/app.js`. Render
with the `xField(...)` function; read the value back with the matching `readX(root)` — never by
ad-hoc id.

| Need | Render | Read | 
|---|---|---|
| Modal (backdrop+dialog+ESC) | `openModal({title, body, actions, onMount})` | — |
| Category field/picker (editable) | `catField(attr, slug)` + `openCatPicker()` | `.cat-field input`.value |
| Category badge (read-only display: label+colour+icon) | `catBadge(slug)` | — |
| Sharing badge (Shared/person/out-of-scope: label+colour+icon) | `shareBadge(sharing)` (`shareInfo()` for parts) | — |
| Attachments (add/list/download/delete) | `attachButton(txn)` + `openAttachments(id)` | `attachList(txn)` |
| Segmented control (pick one) | `segControl(id, opts, sel, className)` | `readSeg(root, id)` |
| Sharing selector | `sharingOptions(id, sel, className)` | `readSharingCtx(root)` |
| Tax bucket picker | `taxField(slug)` + `openTaxPicker(btn)` | `readTax(root)` |
| Income "earned by" selector | `ownerField(id, current)` | `readOwner(root)` |
| Income out-of-scope toggle | `oosToggle(id, on)` | `readOOS(root)` / `readSharingCtx(root)` |
| Year-cost switch | `yearCostSwitch(id, on)` | `readYearCost(root)` |
| Split parts as sub-rows | `splitChildren(txn)` | — |
| Note editor | `openNote(id, cur)` (API) / `openLocalNote(btn)` (DOM `[data-note]`) | — |
| Note indicator/popover | `notePopover(id, note)` | — |
| Stat tile | `statTile(label, value, color)` | — |
| Chart card (title + optional control + caption + canvas) | `chartCard(title, canvasId, {tall, header, note})` | — |
| Period figures (income · expenses · savings · rate) | `moneyTiles(data)` | — |
| "Whose money" perspective options | `scopeSegmentOptions()` | `readSeg(root)` |
| Category doughnut ("Where the money goes") | `drawCategoryPie(canvasId, byCategory)` | — |
| Subcategory doughnut (top N + Other) | `drawSubcategoryPie(canvasId, byCategory, {top})` | — |
| Lighten/darken a colour | `shadeColor(color, amount)` | — |
| Income · expenses · surplus over months | `drawIncomeExpenseLine(canvasId, labels, rows)` | — |
| Liquid net worth card | `netWorthCardHtml(id)` + `fillNetWorthCard({id, year, isCurrent, labelFor})` | — |
| Statement coverage map | `coverageCard(data)` | `coverageGapsFor(data, month)` |
| Balance-anchor status | `anchorStatusChip(summary)` | — |
| Data doctor modal | `openDoctor()` + `doctorResultHtml(result)` | — |
| Chart (line/bar/pie/doughnut) | `mkChart(canvas, {type, data, options})` (Chart.js, vendored, theme-aware) | — |
| Escape user text | `esc(str)` | — |
| Money → integer cents | `cents(x)` (mirrors `pipeline/util.cents`) | — |

These were consolidated from earlier duplicates (tax picker was 3×, note editor 2×, owner 2×,
year-cost 3×, modal ~6×). **Do not reintroduce a second copy** — extend the one component.

The last six exist because the Dashboard (one year) and the Overview (all years) ask the same
questions over different windows. Each takes its window as an argument and knows nothing about
which page called it. A chart that appears on both pages goes here or it will drift.

---

## Working agreements with the user (from PROJECT.md §6)

- **Be critical** — push back on weak ideas; don't just agree.
- **Write short.** No walls of text.
- **Verify UI changes in a real browser** (screenshots). The user notices when things aren't there.
- Commit + push after a finished feature; Conventional-ish messages; Co-Authored-By Claude.
- Personal data (`data/`, `rules/`, `config.json`, `inbox/`, `receipts/`, `backups/`) never
  goes to git.

---

## Definition of done for any change

- [ ] The concept touched exists as **one** component, used by every page that needs it (UI).
- [ ] No new hand-built markup or id-based reads for a control that has a component (UI).
- [ ] User text escaped; money via `cents()` (UI/Backend).
- [ ] All 12 pages still load; the changed control looks/behaves identically everywhere (UI).
- [ ] Pipeline/backend changes **must** pass `./run-tests.sh`.
- [ ] UI changes **must** pass `./run-tests.sh` and `node tests/ui_smoke.js`, and be verified via screenshots in `tests/screenshots/`.
- [ ] **Never change or delete an existing test assertion to make it pass.** A red test means
      either your change is wrong or the test found a real bug — fix the code or report the
      conflict to the user. Weakening a test is the one unforgivable change on this repo.
      (The `MIN_CHECKS` counters in `tests/test_pipeline.py` / `tests/test_oracle.py`, the
      real-data tripwire in `run-tests.sh`, and GitHub Actions CI enforce this mechanically;
      `MIN_CHECKS` may only ever be raised.)
- [ ] A **new bank format** brings a fixture and a mutation row in `tests/test_format_matrix.py` —
      the suite fails on a `pipeline/formats/*.json` nobody has ever read a statement with.
- [ ] A **new write endpoint** is listed in `tests/test_closed_period.py`, as either covered by the
      closed-month matrix or explicitly irrelevant to a settled period. The suite fails until
      somebody decides which.
- [ ] A **new financial branch** in the fixture generator is named in the oracle's
      `REQUIRED_SCENARIOS`. A check count proves volume; the manifest proves meaning.
- [ ] Money arithmetic that rounds gets a property, not an example. `tests/test_settlement_properties.py`
      is the pattern: assert conservation exactly *and* faithfulness against an independent
      `Fraction` computation, so a wrong answer that happens to add up still fails.
