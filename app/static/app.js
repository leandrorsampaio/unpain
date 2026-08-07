/* Family Accountability UI — vanilla JS, talks to the FastAPI endpoints. */
'use strict';

const state = { meta: null, year: null, tab: 'dashboard', renderId: 0, yearCache: new Map(), lastRendered: null, reviewBatches: 1,
  spreadYearCosts: (() => { try { return localStorage.getItem('fa-spread-year-costs') !== '0'; } catch (_) { return true; } })() };
const $ = sel => document.querySelector(sel);
const fmt = v => (v == null ? '–' : (v === 0 ? 0 : v).toLocaleString('en-DE', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' €'); // v===0 normalizes -0 -> 0
/* Format an amount in its ORIGINAL (foreign) currency; falls back to "<number> <CODE>"
   when Intl doesn't recognize the currency code. */
function fmtCur(v, currency) {
  if (v == null) return '–';
  const n = (v === 0 ? 0 : v);
  try { return n.toLocaleString('en-DE', { style: 'currency', currency }); }
  catch (_) { return n.toLocaleString('en-DE', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' ' + currency; }
}
/* German date format, used everywhere: dd.mm.yy (withYear) or dd.mm */
function fmtDate(iso, withYear = false) {
  if (!iso) return '';
  const [y, m, d] = iso.split('-');
  return withYear ? `${d}.${m}.${y.slice(2)}` : `${d}.${m}`;
}
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const MONTH_NAMES = ['January','February','March','April','May','June','July','August','September','October','November','December'];
const YEAR_SELECTION_KEY = 'fa-year-selection';
/* Whether the by-month chart amortizes year costs. Defaults ON so that chart agrees with the
   year totals printed above it; a client-only preference, like the theme. */
const SPREAD_KEY = 'fa-spread-year-costs';
const YEAR_SELECTION_TTL_MS = 7 * 24 * 60 * 60 * 1000;

/* Escape user-supplied text before it goes into innerHTML. Every merchant name,
   purpose, note, or free-text string MUST pass through this. */
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* Money in integer cents — mirrors pipeline/util.cents() exactly. Never sum
   floats then round; round each value, then sum the integers. */
const cents = x => Math.round(Number(x) * 100);

/* Currency dropdown options from config (via /api/meta). EUR is the base and always present. */
const currencyOptions = () => (state.meta.currencies || ['EUR']).map(c => [c, c]);

/* Writes confirm themselves. Feedback lived at the call sites, so whether an edit said
   anything depended on which screen you happened to be on — most of them said nothing. It
   belongs here instead: every POST is a write, so every POST reports, and a page added later
   cannot forget. Endpoints that already show their own result opt out through API_QUIET, and
   a caller can pass {silent:true} when it wants to word the confirmation itself. */
const API_QUIET = new Set([
  '/api/settings',            // autosaves into the #save-status flag
  '/api/anchor', '/api/anchor-delete',   // the balances dialog counts what it wrote
  '/api/decisions-bulk', '/api/decisions-clear-bulk',   // bulk reports how many it touched
  '/api/doctor', '/api/category-usage', '/api/ingest/staging', '/api/ingest/uploads',
  '/api/rule-apply',          // shows a preview, not a write
  '/api/unlock', '/api/lock', '/api/setup',   // the screen itself changes
  '/api/restore', '/api/delete-year', '/api/feedback',  // bespoke messages, some with a reload
  '/api/security/set', '/api/security/change', '/api/security/remove', '/api/security/settings',
]);
const API_MESSAGES = {
  '/api/decision': 'Transaction updated.',
  '/api/decision-clear': 'Back to its rule.',
  '/api/transaction-edit': 'Entry corrected.',
  '/api/transfer-confirm': 'Transfer answered.',
  '/api/close': 'Month updated.',
  '/api/close-year': 'Year updated.',
  '/api/closing-accept': 'New figures adopted.',
  '/api/rule-update': 'Rule saved.',
  '/api/rule': 'Rule saved.',
  '/api/rule-delete': 'Rule deleted.',
  '/api/category-add': 'Category added.',
  '/api/category-rename': 'Category renamed.',
  '/api/category-delete': 'Category archived.',
  '/api/account-add': 'Account added.',
  '/api/account-update': 'Account saved.',
  '/api/account-delete': 'Account deleted.',
  '/api/ratio-override': 'Ratio updated.',
};

async function api(path, body, { silent = false } = {}) {
  const res = await fetch(path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : undefined);
  if (!res.ok) {
    const t = await res.text();
    if (res.status === 401 && t.includes('locked')) { showLockScreen(); throw new Error('locked'); }  // session expired / server restarted
    showError(T('Request failed: ') + t); throw new Error(t);
  }
  const result = await res.json();
  if (body) {
    invalidateYearCache();
    const route = path.split('?')[0];
    if (!silent && !API_QUIET.has(route)) showMessage(T(API_MESSAGES[route] || 'Saved.'));
  }
  return result;
}

function invalidateYearCache() { state.yearCache.clear(); }
function cachedYearData(kind, path) {
  const key = `${kind}:${path}`;
  if (!state.yearCache.has(key)) {
    const request = api(path).catch(err => { state.yearCache.delete(key); throw err; });
    state.yearCache.set(key, request);
  }
  return state.yearCache.get(key);
}
function renderIsCurrent(id, tab, year) { return id === state.renderId && tab === state.tab && year === state.year; }
function setReviewBadge(count) {
  const el = $('#review-badge');
  el.textContent = count || '';
  el.style.display = count ? '' : 'none'; // hide the chip entirely when nothing to review
}
async function refreshReviewBadge(id, tab, year) {
  // Detected transfers wait for confirmation too, and they are the queue nobody
  // would think to look for: an excluded transaction shows up nowhere else.
  const [review, transfers] = await Promise.all([
    cachedYearData('review-count', `/api/review-count?year=${year}`),
    cachedYearData('transfers-count', `/api/transfers-pending-count?year=${year}`),
  ]);
  if (renderIsCurrent(id, tab, year)) setReviewBadge(review.count + transfers.count);
}

/* ---------- boot ---------- */
function savedYear(years, now = Date.now()) {
  try {
    const saved = JSON.parse(localStorage.getItem(YEAR_SELECTION_KEY) || 'null');
    const age = saved ? now - saved.savedAt : NaN;
    if (saved && years.includes(Number(saved.year)) &&
        Number.isFinite(saved.savedAt) && age >= 0 && age <= YEAR_SELECTION_TTL_MS) {
      return Number(saved.year);
    }
  } catch (_) { /* Invalid/old browser state falls back to the newest year. */ }
  localStorage.removeItem(YEAR_SELECTION_KEY);
  return years[years.length - 1];
}

function rememberYear(year) {
  localStorage.setItem(YEAR_SELECTION_KEY, JSON.stringify({ year, savedAt: Date.now() }));
}

/* First-run wizard: shown when /api/meta reports setup_required (no config yet).
   Hides the year select and the section tabs, which are meaningless pre-setup. */
function renderSetup() {
  const yearSelect = $('#year-select');
  if (yearSelect) yearSelect.style.display = 'none';
  const tabsRow = $('#nav-tabs')?.closest('.appbar-row');
  if (tabsRow) tabsRow.style.display = 'none';
  $('#main').innerHTML = `
  <div class="card p-8" style="max-width:560px;margin:48px auto">
    <h1 class="type-headline mb-2">${T('Welcome')}</h1>
    <p class="type-body mb-6">${T('Set up your household once. Everything can be changed later in Settings, except the two names, which become permanent internal identifiers.')}</p>
    <div class="flex gap-4 mb-4">
      ${textField({ id: 'su-p1', label: T('First person (first name)'), className: 'flex-1' })}
      ${textField({ id: 'su-p2', label: T('Second person (first name)'), className: 'flex-1' })}
    </div>
    ${textField({ id: 'su-ratio', label: T('Reference split for the first person (%)'), type: 'number', value: 50, attrs: 'min="1" max="99"' })}
    <p class="type-body-small mb-4" style="color:var(--ink2)">${T('Only a monthly estimate — the binding yearly settlement is computed from actual salaries.')}</p>
    ${textField({ id: 'su-cur', label: T('Foreign currencies (optional, comma-separated ECB codes)'), placeholder: T('e.g. USD, CHF') })}
    <div class="flex justify-end mt-6">
      <md-filled-button id="su-go">${T('Create household')}</md-filled-button>
    </div>
  </div>`;
  $('#su-go').onclick = async () => {
    const cur = $('#su-cur').value.split(',').map(s => s.trim().toUpperCase()).filter(Boolean);
    await api('/api/setup', {
      person1: $('#su-p1').value, person2: $('#su-p2').value,
      ratio_person1: +$('#su-ratio').value || 50, currencies: ['EUR', ...cur],
    });
    location.reload();
  };
}

/* ---------- optional app lock ---------- */
let _inactivityTimer = null;
function clearInactivityTimer() { if (_inactivityTimer) { clearTimeout(_inactivityTimer); _inactivityTimer = null; } }
/* Re-armed on every user action; disarms itself when auto-lock is off. */
function _resetInactivity() {
  const m = state.meta || {};
  clearInactivityTimer();
  if (!(m.lock_enabled && m.auto_lock)) return;
  _inactivityTimer = setTimeout(lockNow, Math.max(1, m.lock_timeout || 5) * 60 * 1000);
}
function setupInactivityTimer() {
  if (!window._lockActivityWired) {
    ['mousemove', 'keydown', 'click', 'scroll', 'touchstart'].forEach(ev => window.addEventListener(ev, _resetInactivity, { passive: true }));
    window._lockActivityWired = true;
  }
  _resetInactivity();
}
async function lockNow() {
  clearInactivityTimer();
  try { await fetch('/api/lock', { method: 'POST' }); } catch (_) { /* offline is fine */ }
  showLockScreen();
}
/* Full-screen, non-dismissible gate. Shown at boot when meta.locked, on any 401, or on idle. */
function showLockScreen() {
  clearInactivityTimer();
  if (document.getElementById('lock-screen')) return;
  const el = document.createElement('div');
  el.id = 'lock-screen';
  el.className = 'lock-screen';
  el.innerHTML = `<div class="lock-card card p-8">
    <md-icon class="lock-icon">lock</md-icon>
    <h1 class="type-headline mt-2 mb-1">${T('Locked')}</h1>
    <p class="type-body-small mb-6" style="color:var(--ink2)">${T('Enter your password to unlock.')}</p>
    ${textField({ id: 'lock-pw', label: T('Password'), type: 'password', className: 'w-full' })}
    <div id="lock-error" class="type-body-small mt-2" style="color:var(--bad); min-height:20px"></div>
    <md-filled-button id="lock-go" class="w-full mt-1">${T('Unlock')}</md-filled-button>
  </div>`;
  document.body.appendChild(el);
  const field = document.getElementById('lock-pw');
  const err = document.getElementById('lock-error');
  const submit = async () => {
    const res = await fetch('/api/unlock', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: field.value }) });
    if (!res.ok) { err.textContent = T('Wrong password.'); field.value = ''; field.focus(); return; }
    location.reload();   // fresh boot, now with a valid session cookie
  };
  document.getElementById('lock-go').onclick = submit;
  field.addEventListener('keydown', e => { if (e.key === 'Enter') submit(); });
  setTimeout(() => field.focus(), 60);
}

/* The app-bar (header) icon + its colour are user-choosable in Settings › Household.
   One place applies them; a blank icon falls back to the piggy-bank default. */
const DEFAULT_BRAND_ICON = 'savings';
function applyBrand(style) {
  style = style || {};
  const iconEl = $('#appbar-icon');
  if (!iconEl) return;
  iconEl.textContent = style.icon || DEFAULT_BRAND_ICON;
  iconEl.style.color = style.color || '';   // blank = CSS default
}

/* The household name is shown in the app bar (header), the footer, and the tab title.
   One place updates all three; a blank name falls back to the product default. */
function applyHouseholdName(name) {
  const label = (name || '').trim() || 'Family Accountability';
  const title = $('#appbar-title-text');
  if (title) title.textContent = label;   // textContent = XSS-safe
  const footer = $('#footer-household');
  if (footer) footer.textContent = label;
  document.title = label;
}

async function boot() {
  state.meta = await api('/api/meta');
  if (state.meta.locked) { applyLanguage(state.meta.language || 'en'); showLockScreen(); return; }  // password set + no valid session
  if (state.meta.setup_required) { renderSetup(); return; }
  applyLanguage(state.meta.language || 'en');  // config.json is the source of truth; retranslates the static chrome
  setupInactivityTimer();
  applyHouseholdName(state.meta.household_name);
  applyBrand(state.meta.brand_style);
  const years = state.meta.years.length ? state.meta.years : [new Date().getFullYear()];
  state.year = savedYear(years);
  await customElements.whenDefined('md-outlined-select');
  const yearSelect = $('#year-select');
  yearSelect.innerHTML = years.map(y => `<md-select-option value="${y}" ${y === state.year ? 'selected' : ''}><div slot="headline">${y}</div></md-select-option>`).join('');
  await yearSelect.updateComplete;
  yearSelect.value = String(state.year);
  yearSelect.onchange = e => { state.year = +e.target.value; rememberYear(state.year); render(); };
  await customElements.whenDefined('md-tabs');
  const tabs = $('#nav-tabs');
  tabs.addEventListener('change', () => {
    const tab = tabs.activeTab && tabs.activeTab.dataset.tab;
    if (tab && tab !== state.tab) location.hash = tab;
  });
  // Direct click fallback: after a non-tab page (Notes/#feedback) md-tabs keeps its old
  // selected index, so clicking the previously-active tab fires no 'change'. Navigate on the
  // raw click too (idempotent — the hash guard makes a duplicate 'change' a no-op).
  tabs.addEventListener('click', event => {
    const tab = event.target.closest && event.target.closest('md-primary-tab');
    if (tab && tab.dataset.tab && tab.dataset.tab !== state.tab) location.hash = tab.dataset.tab;
  });
  const syncFromHash = () => {
    const hash = location.hash.slice(1);
    if (hash === 'accounts') { state.settingsArea = 'accounts'; state.tab = 'settings'; }  // legacy deep-link → Settings › Accounts
    else if (['dashboard', 'transactions', 'ingest', 'review', 'rules', 'categories', 'settlement', 'tax', 'add', 'feedback', 'settings'].includes(hash)) state.tab = hash;
    const idx = [...tabs.tabs].findIndex(t => t.dataset.tab === state.tab);
    if (idx >= 0) tabs.activeTabIndex = idx;
    else [...tabs.tabs].forEach(tab => { tab.active = false; });  // non-tab page: no tab active
  };
  window.addEventListener('hashchange', () => { syncFromHash(); render(); });
  syncFromHash();
  initTheme();
  initDoctor();
  initStickyOffset();
  render();
}

/* Keep the sticky control bars pinned right below the (variable-height) app bar
   by publishing its height as --appbar-h. */
function initStickyOffset() {
  const bar = document.querySelector('header.appbar');
  const sync = () => document.documentElement.style.setProperty('--appbar-h', bar.offsetHeight + 'px');
  sync();
  new ResizeObserver(sync).observe(bar);
  window.addEventListener('resize', sync);
}

/* Theme is a client-only preference (localStorage 'fa-theme'): 'light' | 'dark' | absent=system.
   The header toggle and the Settings › Preferences control both drive setThemeMode(). */
function resolvedTheme() {
  return document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
}
function themeMode() { try { return localStorage.getItem('fa-theme') || 'system'; } catch (_) { return 'system'; } }
function updateThemeIcon() {
  const ic = $('#theme-toggle')?.querySelector('md-icon');
  if (ic) ic.textContent = resolvedTheme() === 'dark' ? 'light_mode' : 'dark_mode';
}
function setThemeMode(mode) {
  if (mode === 'system') { delete document.documentElement.dataset.theme; try { localStorage.removeItem('fa-theme'); } catch (_) { /* private mode */ } }
  else { document.documentElement.dataset.theme = mode; try { localStorage.setItem('fa-theme', mode); } catch (_) { /* private mode */ } }
  updateThemeIcon();
  render();  // charts read theme colours at creation; also refreshes the Preferences control
}
function initTheme() {
  const saved = localStorage.getItem('fa-theme');
  if (saved) document.documentElement.dataset.theme = saved;
  updateThemeIcon();
  const btn = $('#theme-toggle');
  if (btn) btn.onclick = () => setThemeMode(resolvedTheme() === 'dark' ? 'light' : 'dark');
}
/* Light | Dark | System segmented control (Settings › Preferences). */
function themeSeg() {
  const mode = themeMode();
  const opt = (val, label, icon) => `<md-outlined-button type="button" class="seg-option${mode === val ? ' selected' : ''}" aria-pressed="${mode === val}" onclick="setThemeMode('${val}')"><span class="seg-option-content"><md-icon class="seg-icon">${icon}</md-icon>${T(label)}</span></md-outlined-button>`;
  return `<div class="seg-ctrl" role="group" aria-label="${T('Appearance')}">${opt('light', 'Light', 'light_mode')}${opt('dark', 'Dark', 'dark_mode')}${opt('system', 'System', 'brightness_auto')}</div>`;
}

function initDoctor() { const b = $('#doctor-button'); if (b) b.onclick = openDoctor; }  // doctor now lives in Settings › Data; header button is optional

/* Plain-language "what it means / what to do" per check slug, plus the row
   action (if any) offered for each affected transaction. Keeps the raw check
   slug out of the user's face while staying keyed to it. */
const DOCTOR_CHECKS = {
  'orphan-decision': {
    label: 'Leftover categorization',
    what: 'A saved categorization points at a transaction that no longer exists — it was removed, or re-imported under a new id. It changes no totals; it is just cruft left in the decisions file.',
    fix: 'Remove the stale decision. There is no transaction to review.', action: 'remove' },
  'unpaired-marker': {
    label: 'Unmatched internal transfer',
    what: 'This is flagged as an internal transfer, but no opposite movement of a similar amount turned up on another account within the matching window — so it is currently hidden from every total. Recognised credit-card settlements are excluded automatically.',
    fix: 'If it is not really an internal transfer, send it back to the review queue to categorize it properly.', action: 'review' },
  'unknown-account': { label: 'Unknown account', what: 'Points at an account that is not configured.', fix: 'Add the account, or fix the reference.' },
  'unknown-category': { label: 'Unknown category', what: 'References a category slug that does not exist.', fix: 'Re-categorize, or restore the category.' },
  'split-sum': { label: 'Split does not balance', what: 'The stored split parts do not add up to the transaction amount.', fix: 'Re-open the split and fix the amounts.' },
  'duplicate-id': { label: 'Duplicate transaction id', what: 'The same transaction id appears in more than one stored file.', fix: 'Remove the duplicate source file.' },
  'unknown-sharing': { label: 'Invalid sharing', what: 'A decision uses a sharing value that is not allowed.', fix: 'Re-pick shared / personal / out-of-scope.' },
  'unknown-owner': { label: 'Invalid owner', what: 'A decision names an income or tax owner that is not a known person.', fix: 'Re-assign the owner.' },
  'anchor-conflict': { label: 'Conflicting balance anchors', what: 'Two different balances were recorded for the same account on the same day.', fix: 'Remove the wrong anchor.' },
  'anchor-mismatch': { label: 'Balance does not reconcile', what: 'The transactions between two balance anchors do not add up to the recorded balance change.', fix: 'A statement is likely missing or double-counted in that span.' },
  'cash-desync': { label: 'Cash out of sync', what: 'The stored cash transactions no longer match cash.csv.', fix: 'Re-ingest cash.csv.' },
  'review-in-closed-month': { label: 'Unreviewed item in a closed month', what: 'A transaction still needs review inside a month you already closed.', fix: 'Re-open the month to review it.' },
  'orphan-budget': { label: 'Budget for missing category', what: 'A budget is set for a category that no longer exists.', fix: 'Remove or re-point the budget.' },
  'stale-upload-ref': { label: 'Stale upload reference', what: 'An upload record points at a transaction source that is gone.', fix: 'Safe to ignore; clears on next ingest.' },
};

function doctorRowHtml(finding, detail) {
  const meta = DOCTOR_CHECKS[finding.check] || {};
  const hasTxn = !!detail.date;
  const head = hasTxn
    ? `${fmtDate(detail.date, true)} · <span class="font-medium">${fmt(detail.amount_eur)}</span> · ${esc(detail.counterparty || '—')}`
    : `${T('Stale decision')}${detail.category ? ` · ${esc(detail.category)}` : ''}`;
  const sub = [hasTxn ? esc(detail.account || '') : '', `id ${esc(detail.id)}`].filter(Boolean).join(' · ');
  const button = meta.action
    ? `<md-text-button class="doctor-action shrink-0" data-action="${meta.action}" data-year="${esc(finding.year)}" data-id="${esc(detail.id)}">
        <md-icon slot="icon">${meta.action === 'review' ? 'rate_review' : 'delete'}</md-icon>${meta.action === 'review' ? T('Send to review') : T('Remove')}</md-text-button>`
    : '';
  return `<div class="doctor-row flex items-center justify-between gap-3 mt-2">
    <div style="min-width:0"><div class="type-body-small truncate">${head}</div>
      <div class="type-caption" style="color:var(--ink2)">${sub}</div></div>${button}</div>`;
}

function doctorResultHtml(result) {
  const counts = { error: 0, warning: 0, info: 0 };
  result.findings.forEach(item => { counts[item.severity] += 1; });
  const checked = result.checked;
  const summary = T('{errors} errors, {warnings} warnings, {info} info — {years} years, {txns} transactions checked', { errors: counts.error, warnings: counts.warning, info: counts.info, years: checked.years.length, txns: checked.transactions.toLocaleString('en-DE') });
  if (!result.findings.length) {
    return `<div class="doctor-summary type-body-small" style="color:var(--ink2)">${summary}</div>
      <div class="doctor-clear mt-4"><md-icon>check_circle</md-icon><span class="type-title font-medium">${T('All clear ✓')}</span></div>`;
  }
  const group = (severity, label) => {
    const items = result.findings.filter(item => item.severity === severity);
    if (!items.length) return '';
    return `<section class="doctor-group doctor-${severity} mt-4">
      <div class="flex items-center gap-2 mb-2"><span class="chip ${severity === 'error' ? 'chip-bad' : severity === 'warning' ? 'chip-warn' : 'chip-neutral'}">${esc(label)} · ${items.length}</span></div>
      ${items.map(item => {
        const meta = DOCTOR_CHECKS[item.check] || {};
        return `<div class="doctor-finding type-body-small">
        <div class="font-medium">${esc(meta.label ? T(meta.label) : item.check)}${item.year ? ` · ${esc(item.year)}` : ''}</div>
        ${meta.what ? `<div class="type-caption mt-1" style="color:var(--ink2)">${esc(T(meta.what))}</div>` : ''}
        ${meta.fix ? `<div class="type-caption mt-1" style="color:var(--ink2)"><span class="font-medium">${T('Fix:')}</span> ${esc(T(meta.fix))}</div>` : `<div class="type-caption mt-1" style="color:var(--ink2)">${esc(item.message)}</div>`}
        ${(item.details || []).map(detail => doctorRowHtml(item, detail)).join('')}
      </div>`; }).join('')}
    </section>`;
  };
  return `<div class="doctor-summary type-body-small font-medium">${summary}</div>
    ${group('error', T('Errors'))}${group('warning', T('Warnings'))}${group('info', T('Info'))}`;
}

async function runDoctor(body) {
  body.innerHTML = '<div class="flex justify-center p-8"><md-circular-progress indeterminate></md-circular-progress></div>';
  body.innerHTML = doctorResultHtml(await api('/api/doctor'));
}

async function doctorAction(button, body) {
  const { action, year, id } = button.dataset;
  button.disabled = true;
  if (action === 'review') {
    await api('/api/decision', { year: +year, id, fields: { kind: 'normal',
      note: 'Returned to review by the data health check: this internal-transfer marker had no matching opposite movement.' } });
  } else if (action === 'remove') {
    await api('/api/decision-clear-orphan', { year: +year, id });
  }
  await runDoctor(body);
}

function openDoctor() {
  openModal({
    title: T('Data health check'),
    width: '720px',
    body: `<div class="doctor-body">
      <p class="type-body-small mb-4" style="color:var(--ink2)">${T('Scans all years for data problems. Read-only until you act on a finding.')}</p>
      <md-filled-button class="doctor-run"><md-icon slot="icon">monitor_heart</md-icon>${T('Run check')}</md-filled-button>
    </div>`,
    actions: `<md-text-button class="doctor-close">${T('Close')}</md-text-button>`,
    onMount: root => {
      const body = root.querySelector('.doctor-body');
      root.querySelector('.doctor-close').onclick = () => root._close();
      root.querySelector('.doctor-run').onclick = () => runDoctor(body);
      body.addEventListener('click', event => {
        const button = event.target.closest('.doctor-action');
        if (button) doctorAction(button, body);
      });
    },
  });
}

/* Every mutating action re-renders the whole tab, which rebuilds #main from
   scratch and would drop the reader back at the top of the page. When we stay on
   the same tab+year (an in-place refresh, not navigation) we put the scroll
   position back afterwards; navigation still starts at the top. */
async function render() {
  const id = ++state.renderId;
  const tab = state.tab;
  const year = state.year;
  const inPlace = state.lastRendered && state.lastRendered.tab === tab && state.lastRendered.year === year;
  const scrollY = inPlace ? window.scrollY : 0;
  if (!inPlace) state.reviewBatches = 1;   // lazy-loaded review batches belong to one tab+year visit
  state.lastRendered = { tab, year };
  const views = { dashboard: renderDashboard, transactions: renderTransactions, ingest: renderIngest, review: renderReview, rules: renderRules, categories: renderCategories, settlement: renderSettlement, tax: renderTax, add: renderAdd, feedback: renderFeedback, settings: renderSettings };
  if (tab === 'review') $('#main').innerHTML = '<div class="card p-8 flex items-center justify-center"><md-circular-progress indeterminate></md-circular-progress></div>';
  const badge = ['dashboard', 'review'].includes(tab) ? null : refreshReviewBadge(id, tab, year);
  await views[tab](id);
  if (renderIsCurrent(id, tab, year)) {
    attachTooltips();
    if (scrollY) restoreScroll(scrollY, id, tab, year);
  }
  if (badge) await badge;
}

/* Re-apply a scroll offset once layout has settled. md-* components upgrade
   asynchronously, so the page can still be growing on the frame right after
   innerHTML lands; two rAFs put us past that. The browser clamps on its own when
   the rebuilt page is shorter (e.g. a review group just left the queue). */
function restoreScroll(y, id, tab, year) {
  requestAnimationFrame(() => requestAnimationFrame(() => {
    if (renderIsCurrent(id, tab, year)) window.scrollTo(0, y);
  }));
}

/* ---------- helpers ---------- */
function catName(slug) {
  if (!slug) return 'Uncategorized';
  const [g, s] = slug.split('/');
  const grp = state.meta.categories.find(c => c.slug === g);
  const sub = grp && grp.subs.find(x => x.slug === s);
  return grp ? `${grp.name} · ${sub ? sub.name : s}` : slug;
}
function groupName(slug) { const g = state.meta.categories.find(c => c.slug === slug); return g ? g.name : slug; }
function subName(slug) {
  const [g, s] = slug.split('/');
  const grp = state.meta.categories.find(c => c.slug === g);
  const sub = grp && grp.subs.find(x => x.slug === s);
  return sub ? sub.name : (s || slug);
}

/* Material Design palette (accent = ~500 tone). Two tints are derived per box
   with color-mix so they adapt to light/dark automatically. */
const MATERIAL_PALETTE = [
  ['Red', '#e53935'], ['Pink', '#d81b60'], ['Purple', '#8e24aa'], ['Deep Purple', '#5e35b1'],
  ['Indigo', '#3949ab'], ['Blue', '#1e88e5'], ['Light Blue', '#039be5'], ['Cyan', '#00acc1'],
  ['Teal', '#00897b'], ['Green', '#43a047'], ['Light Green', '#7cb342'], ['Lime', '#c0ca33'],
  ['Amber', '#ffb300'], ['Orange', '#fb8c00'], ['Deep Orange', '#f4511e'], ['Brown', '#6d4c41'],
  ['Blue Grey', '#546e7a'], ['Grey', '#757575'],
];
const CATEGORY_ICONS = [
  // money & income
  'payments', 'savings', 'account_balance', 'request_quote', 'euro', 'currency_exchange', 'credit_card',
  'account_balance_wallet', 'paid', 'price_change', 'receipt_long',
  // housing
  'home', 'apartment', 'house', 'cottage', 'roofing', 'bed', 'bathtub', 'kitchen',
  // utilities
  'bolt', 'water_drop', 'wifi', 'electric_bolt', 'thermostat', 'ac_unit', 'lightbulb', 'local_fire_department',
  // food & drink
  'restaurant', 'local_cafe', 'local_grocery_store', 'fastfood', 'lunch_dining', 'dinner_dining',
  'breakfast_dining', 'ramen_dining', 'bakery_dining', 'local_pizza', 'local_bar', 'wine_bar', 'coffee',
  'liquor', 'cake',
  // shopping & clothing
  'shopping_cart', 'checkroom', 'storefront', 'shopping_bag', 'local_mall', 'apparel',
  'local_laundry_service', 'dry_cleaning',
  // beauty & personal
  'spa', 'content_cut', 'brush',
  // tech & electronics
  'smartphone', 'devices', 'computer', 'laptop', 'tv', 'headphones', 'watch', 'print',
  // home goods & furniture
  'chair', 'weekend', 'cleaning_services', 'blender',
  // transport
  'directions_car', 'directions_bus', 'local_gas_station', 'two_wheeler', 'electric_car', 'electric_scooter',
  'local_taxi', 'local_shipping', 'car_repair',
  // travel
  'flight', 'train', 'luggage', 'hotel', 'travel_explore', 'camping', 'festival',
  // sports & fitness
  'fitness_center', 'sports_soccer', 'sports_tennis', 'sports_basketball', 'sports_football',
  'sports_gymnastics', 'pool', 'downhill_skiing', 'directions_run',
  // education
  'school', 'menu_book', 'science', 'calculate', 'auto_stories', 'translate',
  // health
  'medical_services', 'health_and_safety', 'medication', 'vaccines', 'healing', 'monitor_heart',
  'emergency', 'local_hospital',
  // entertainment & hobbies
  'movie', 'sports_esports', 'music_note', 'theaters', 'casino', 'piano', 'mic', 'photo_camera',
  'palette', 'museum',
  // nature & outdoors
  'park', 'beach_access', 'forest', 'agriculture', 'eco', 'local_florist',
  // tools & maintenance
  'build', 'handyman', 'construction', 'plumbing', 'carpenter', 'electrical_services',
  // gifts & giving
  'celebration', 'redeem', 'card_giftcard', 'volunteer_activism',
  // work & business
  'work', 'business_center', 'groups',
  // legal & other
  'gavel', 'balance', 'pets', 'child_care', 'category',
];
const ICON_RULES = [
  [/receive|income|salary|salar|wage/, 'payments'], [/living cost|rent|housing|apart/, 'home'],
  [/upgrade|furnitur|deco/, 'chair'], [/core living|grocer|supermarket/, 'shopping_cart'],
  [/transport|car|vehicle|commute|fuel/, 'directions_car'], [/health|medic|doctor|therap/, 'medical_services'],
  [/sport|gym|fitness/, 'fitness_center'], [/stud|school|educat|course|learn/, 'school'],
  [/recreation|leisure|entertain|hobby|fun/, 'sports_esports'], [/donation|charit/, 'volunteer_activism'],
  [/gift/, 'card_giftcard'], [/travel|trip|vacation|holiday/, 'flight'], [/project/, 'build'],
];
function defaultIcon(cat) {
  if (cat.icon) return cat.icon;
  const n = (cat.name || '').toLowerCase();
  for (const [re, ic] of ICON_RULES) if (re.test(n)) return ic;
  return cat.type === 'income' ? 'payments' : 'category';
}
function catColor(cat) {
  if (cat.color) return cat.color;
  let h = 0;
  for (const ch of cat.slug) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return MATERIAL_PALETTE[h % MATERIAL_PALETTE.length][1];
}

function catLabel(slug) {
  if (!slug) return '';
  if (slug === 'auto:items') return 'Items (auto by amount)';
  return catName(slug);
}
function catIconFor(slug) {
  if (slug === 'auto:items') return 'tune';
  const cat = state.meta.categories.find(c => c.slug === slug.split('/')[0]);
  return cat ? defaultIcon(cat) : '';
}
function catTriggerHtml(slug) {
  if (!slug) return T('Choose category');
  const ic = catIconFor(slug);
  return (ic ? `<md-icon slot="icon">${ic}</md-icon>` : '') + catLabel(slug);
}
function catColorFor(slug) {
  if (!slug) return 'var(--ink2)';
  const [gslug, sslug] = slug.split('/');
  const g = state.meta.categories.find(c => c.slug === gslug);
  if (!g) return 'var(--primary)';
  // Subcategories inherit their category's colour (the same hex the category shows and that
  // updates when its colour changes) — an explicit sub colour would win if one is ever set.
  const sub = sslug && (g.subs || []).find(s => s.slug === sslug);
  return (sub && sub.color) || catColor(g);
}

/* ---- charts ---- : the ONE way to render a chart anywhere. Wraps Chart.js
   (vendored, offline) with our theme defaults so every chart looks like the app
   and re-themes on light/dark. Callers pass a <canvas> and a Chart.js config;
   colors default to our tokens but can be overridden per-dataset (e.g. category
   colours). Destroys any prior chart on the same canvas so re-renders are safe. */
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
const CHART_COLORS = () => ({
  ink: cssVar('--ink'), ink2: cssVar('--ink2'), line: cssVar('--line'),
  c1: cssVar('--chart-1'), c2: cssVar('--chart-2'), good: cssVar('--good'), bad: cssVar('--bad'),
});
function mkChart(canvas, config) {
  if (!canvas || typeof Chart === 'undefined') return null;
  if (canvas._chart) canvas._chart.destroy();
  const t = CHART_COLORS();
  const money = v => fmt(v);
  const horiz = (config.options && config.options.indexAxis) === 'y';
  const isPie = config.type === 'doughnut' || config.type === 'pie' || config.type === 'sankey';
  const moneyAxis = { ticks: { color: t.ink2, font: { family: 'Roboto', size: 11 }, callback: v => money(v) }, grid: { color: t.line, drawTicks: false }, border: { color: t.line } };
  const catAxis = { ticks: { color: t.ink2, font: { family: 'Roboto', size: 11 } }, grid: { display: false }, border: { color: t.line } };
  const base = {
    responsive: true, maintainAspectRatio: false,
    plugins: {
      legend: { labels: { color: t.ink2, font: { family: 'Roboto', size: 12 }, boxWidth: 12, boxHeight: 12, usePointStyle: true } },
      tooltip: {
        callbacks: {
          label: ctx => {
            const p = ctx.parsed;
            const v = (p && typeof p === 'object') ? (p.y != null && !horiz ? p.y : p.x != null ? p.x : p.y) : p;
            const name = ctx.dataset.label || ctx.label || '';
            return `${name ? name + ': ' : ''}${money(v)}`;
          },
        },
      },
    },
    scales: isPie ? undefined : { x: horiz ? moneyAxis : catAxis, y: horiz ? catAxis : moneyAxis },
  };
  config.options = deepMerge(base, config.options || {});
  Chart.defaults.font.family = 'Roboto';
  canvas._chart = new Chart(canvas, config);
  return canvas._chart;
}
/* Resolve a CSS var() color string to its computed value (Chart.js needs real
   colors, not var(...)). Category colours are usually hex already. */
function resolveColor(c) {
  if (typeof c === 'string' && c.startsWith('var(')) return cssVar(c.slice(4, -1).trim()) || c;
  return c;
}
/* shallow-ish recursive merge used only by mkChart to overlay caller options */
function deepMerge(a, b) {
  if (Array.isArray(b) || typeof b !== 'object' || b === null) return b;
  const out = Array.isArray(a) ? [...a] : { ...(a || {}) };
  for (const k of Object.keys(b)) out[k] = (typeof b[k] === 'object' && b[k] !== null && !Array.isArray(b[k])) ? deepMerge(out[k], b[k]) : b[k];
  return out;
}

/* ---- category badge ---- : the ONE way to DISPLAY a category anywhere (label +
   colour + icon). Use for any read-only category display; use catField() only for
   the editable picker. */
function catBadge(slug, extraClass = '') {
  if (!slug) return `<span class="cat-badge ${extraClass}"><md-icon>help</md-icon>${T('Uncategorized')}</span>`;
  return `<span class="cat-badge ${extraClass}" style="--cat:${catColorFor(slug)}"><md-icon>${catIconFor(slug) || 'category'}</md-icon>${esc(catLabel(slug))}</span>`;
}

/* ---- sharing badge ---- : the ONE way to show/label a sharing value anywhere
   (Shared / <person> / Out of scope) — icon + colour + short label. Person
   colours by index: blue, pink, then fallbacks. Used in lists AND selectors. */
const PERSON_COLORS = ['#1e88e5', '#d81b60', '#8e24aa', '#00897b'];
/* Person-appropriate Material Symbols offered in Settings for each partner. */
const PERSON_ICONS = ['person', 'face', 'face_2', 'face_3', 'face_4', 'face_5', 'face_6',
  'person_2', 'person_3', 'person_4', 'account_circle', 'sentiment_satisfied', 'sentiment_very_satisfied',
  'emoji_emotions', 'mood', 'self_improvement', 'diversity_1', 'emoji_people', 'sports_martial_arts',
  'psychology', 'volunteer_activism', 'pets', 'elderly', 'elderly_woman', 'boy', 'girl', 'man', 'woman',
  'hiking', 'rocket_launch', 'star', 'favorite'];
/* A partner's colour/icon: a user override from config (Settings) else a stable default. */
function personColor(p) {
  return ((state.meta.person_styles || {})[p] || {}).color
    || PERSON_COLORS[state.meta.people.indexOf(p)] || PERSON_COLORS[2];
}
function personIcon(p) {
  return ((state.meta.person_styles || {})[p] || {}).icon || 'person';
}
/* Icons offered in Settings for the shared / together option. */
const SHARED_ICONS = ['group', 'groups', 'groups_2', 'groups_3', 'diversity_1', 'diversity_2', 'diversity_3',
  'diversity_4', 'people', 'people_alt', 'supervisor_account', 'handshake', 'favorite', 'interests',
  'home', 'family_restroom', 'escalator_warning', 'volunteer_activism', 'join_full', 'join_inner',
  'partner_exchange', 'connect_without_contact', 'hub', 'forum'];
const sharedColor = () => (state.meta.shared_style || {}).color || '#fb8c00';
const sharedIcon = (def) => (state.meta.shared_style || {}).icon || def;
/* Display name for a person slug. Slugs are permanent ids; labels are cosmetic.
   personLabelRaw is UNescaped — use it where the sink escapes (selectField/segControl
   headlines, txnFilterToggle, textContent, or a string a consumer later esc()s).
   personLabel is escaped — use it for direct innerHTML interpolation. Never both. */
const personLabelRaw = p => (state.meta.person_labels || {})[p] || (p ? p[0].toUpperCase() + p.slice(1) : '');
const personLabel = p => esc(personLabelRaw(p));
/* ONE source of truth for the icon + colour + label of a person/sharing option, keyed by
   `kind`: 'shared' | 'both' | 'together' | 'out-of-scope' | 'person:<slug>'. Used by the
   segmented selector (personSegment) AND the sharing badges (shareInfo), so every place the
   partners/shared/out-of-scope appear shares the same colours, icons and labels. */
function segInfo(kind) {
  if (kind === 'out-of-scope') return { icon: 'block', color: '#757575', label: T('Out of scope') };
  if (kind === 'both') return { icon: sharedIcon('group'), color: sharedColor(), label: T('Both') };
  if (kind === 'together') return { icon: sharedIcon('groups'), color: sharedColor(), label: T('Together') };
  if (kind && kind.startsWith('person:')) {
    const p = kind.slice('person:'.length);
    return { icon: personIcon(p), color: personColor(p), label: personLabelRaw(p) };
  }
  return { icon: sharedIcon('group'), color: sharedColor(), label: T('Shared') };
}
function shareInfo(sharing) {
  const kind = sharing === 'out-of-scope' ? 'out-of-scope'
    : (sharing && sharing.startsWith('personal:')) ? 'person:' + sharing.slice('personal:'.length)
      : 'shared';
  return segInfo(kind);
}
function shareBadge(sharing, extraClass = '') {
  const s = shareInfo(sharing);
  return `<span class="cat-badge ${extraClass}" style="--cat:${s.color}"><md-icon>${s.icon}</md-icon>${esc(s.label)}</span>`;
}
/* Foreign-currency conversion marker, shown left of the EUR value on the Transactions page.
   The EUR figure is the ECB-converted one; this flags that and the tooltip carries the
   original amount, the rate used, and its date. Offline + MD3: a Material Symbol, not a
   country flag (currency ≠ country, and flag emoji don't render on Windows). */
function fxBadge(t) {
  if (!t || !t.currency || t.currency === 'EUR' || t.fx_rate == null) return '';
  const tip = T('Converted from {orig} · ECB rate 1 € = {rate} {cur} · {date}', {
    orig: fmtCur(t.amount_original, t.currency),
    rate: t.fx_rate.toLocaleString('en-DE', { maximumFractionDigits: 4 }),
    cur: t.currency,
    date: fmtDate(t.date, true),
  });
  return `<md-icon class="fx-badge" ${tooltip(tip)}>currency_exchange</md-icon>`;
}
/* The ONE "possible internal transfer" hint chip (transactions list + review queue). */
function transferHintChip(t) {
  if (!t.possible_transfer) return '';
  return `<span class="chip chip-warn shrink-0" ${tooltip(t.possible_transfer_reason || T('Possible internal transfer'))}>${T('Possible transfer')}</span>`;
}

/* Category picker: a hidden input (keeps the field's id/class so save handlers
   read `.value` unchanged) plus a trigger button that opens the modal grid. */
function catField(attr, selected) {
  return `<span class="cat-field">
    <input type="hidden" ${attr} value="${selected || ''}">
    <md-outlined-button type="button" class="cat-trigger" onclick="openCatPicker(this)">
      ${catTriggerHtml(selected)}
    </md-outlined-button>
  </span>`;
}

function updateCatTrigger(btn, slug) {
  btn.innerHTML = catTriggerHtml(slug);
}

/* Single-select: openCatPicker(btn). Multi-select (e.g. filters): pass opts
   { multi:true, selected:[slugs], onDone:(slugs)=>{} }. Same modal/visuals. */
function openCatPicker(btn, opts = null) {
  const multi = !!(opts && opts.multi);
  state.catTarget = multi ? null : btn;
  state.catPickerOpts = opts;
  state.catPickerSel = multi ? new Set(opts.selected || []) : null;
  const current = multi ? '' : btn.closest('.cat-field').querySelector('input').value;
  const isSel = slug => multi ? state.catPickerSel.has(slug) : slug === current;
  const onClick = slug => multi ? `onclick="toggleCatPick('${slug}', this)"` : `onclick="pickCat('${slug}')"`;
  const box = c => {
    const subs = c.subs.filter(s => !s.archived);
    if (!subs.length) return '';
    return `<div class="cat-box ${c.type === 'income' ? 'income' : ''}" style="--acc:${catColor(c)}" data-name="${c.name.toLowerCase()}">
      <h4><md-icon>${defaultIcon(c)}</md-icon>${c.name}</h4>
      ${subs.map(s => {
        const slug = `${c.slug}/${s.slug}`;
        return `<button type="button" class="cat-sub ${isSel(slug) ? 'selected' : ''}" data-slug="${slug}" data-name="${s.name.toLowerCase()}" ${onClick(slug)}>${s.name}</button>`;
      }).join('')}
    </div>`;
  };
  const boxes = state.meta.categories.filter(c => !c.archived).map(box).join('');
  const special = multi ? '' : `<div class="cat-box" style="--acc:#757575" data-name="items auto special">
      <h4><md-icon>tune</md-icon>${T('Special')}</h4>
      <button type="button" class="cat-sub ${current === 'auto:items' ? 'selected' : ''}" data-name="items auto amount" onclick="pickCat('auto:items')">${T('Items (auto by amount)')}</button>
    </div>`;
  const actions = multi
    ? `<md-text-button class="cat-cancel">${T('Cancel')}</md-text-button><md-filled-button onclick="catPickerDone()">${T('Done')}</md-filled-button>`
    : `${current ? `<md-text-button onclick="pickCat('')">${T('Clear')}</md-text-button>` : ''}<md-text-button class="cat-cancel">${T('Close')}</md-text-button>`;
  const dialog = openModal({
    title: multi ? T('Choose categories') : T('Choose a category'), width: '1300px',
    body: `${textField({ label: T('Filter categories'), className: 'cat-search', attrs: 'oninput="filterCatPicker(this.value)"' })}<div class="cat-grid mt-3">${boxes}${special}</div>`,
    actions,
    onMount: root => { root.querySelector('.cat-cancel').onclick = () => root._close(); },
    onClose: resetCatPicker,
  });
  state.catBackdrop = dialog;
  setTimeout(() => dialog.querySelector('.cat-search').focus(), 30);
}

function toggleCatPick(slug, btnEl) {
  if (state.catPickerSel.has(slug)) state.catPickerSel.delete(slug);
  else state.catPickerSel.add(slug);
  btnEl.classList.toggle('selected');
}
function catPickerDone() {
  const opts = state.catPickerOpts, sel = state.catPickerSel;
  closeCatPicker();
  if (opts && opts.onDone) opts.onDone([...sel]);
}

function filterCatPicker(q) {
  q = q.trim().toLowerCase();
  state.catBackdrop.querySelectorAll('.cat-box').forEach(box => {
    let any = false;
    box.querySelectorAll('.cat-sub').forEach(sub => {
      const match = !q || sub.dataset.name.includes(q) || box.dataset.name.includes(q);
      sub.style.display = match ? '' : 'none';
      if (match) any = true;
    });
    box.style.display = any ? '' : 'none';
  });
}

function pickCat(slug) {
  if (state.catTarget) {
    const input = state.catTarget.closest('.cat-field').querySelector('input');
    input.value = slug;
    updateCatTrigger(state.catTarget, slug);
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }
  closeCatPicker();
}

function closeCatPicker() {
  if (state.catBackdrop) state.catBackdrop._close();
}
function resetCatPicker() {
  state.catBackdrop = null;
  state.catTarget = null;
  state.catPickerOpts = null;
  state.catPickerSel = null;
}

function scopeChoices() { return [['family', T('Rule for: family')], ...state.meta.people.map(p => [p, T('Rule for: {name} only', { name: personLabelRaw(p) })])]; }

/* Segmented control. One implementation for every "pick exactly one" choice —
   sharing, rule match-in, etc. Selection is communicated by the filled segment,
   not by a second radio glyph inside the button. */
function segControl(id, opts, selected, className = '', rawLabels = false) {
  return `<div class="seg-ctrl ${className}" id="${id}" role="group" aria-label="${T('Choose one option')}" onkeydown="segmentKeydown(event)">
    ${opts.map(([v, l]) => {
      const active = v === selected;
      return `<md-outlined-button type="button" class="seg-option${active ? ' selected' : ''}" data-value="${esc(v)}" aria-pressed="${active}" tabindex="${active ? '0' : '-1'}" onclick="selectSegment(this)"><span class="seg-option-content">${rawLabels ? l : esc(l)}</span></md-outlined-button>`;
    }).join('')}
  </div>`;
}
function selectSegment(option) {
  const group = option.closest('.seg-ctrl');
  group.querySelectorAll('.seg-option').forEach(item => {
    const active = item === option;
    item.classList.toggle('selected', active);
    item.setAttribute('aria-pressed', String(active));
    item.tabIndex = active ? 0 : -1;
  });
  group.dispatchEvent(new Event('change', { bubbles: true }));
}

/* Shared income-split slider: Partner 1 (people[0]) on the left, Partner 2 gets the remainder.
   Used by the settlement ratio-override modal and the Settings default. Render with
   ratioSlider(id, pct); read `#${id}`.value (0-100); call wireRatioSlider(id) after inserting
   into the DOM so the % labels track the drag. */
function ratioSlider(id, pct, { min = 1, max = 99 } = {}) {
  const [p1, p2] = state.meta.people;
  return `<div class="flex items-center justify-between mb-1 font-medium">
      <span id="${esc(id)}-l" style="color:var(--chart-1)">${personLabel(p1)} ${pct}%</span>
      <span id="${esc(id)}-r" style="color:var(--chart-2)">${personLabel(p2)} ${100 - pct}%</span>
    </div>
    <input type="range" min="${min}" max="${max}" step="1" value="${pct}" class="ratio-range" id="${esc(id)}" style="width:100%">`;
}
function wireRatioSlider(id, onChange) {
  const [p1, p2] = state.meta.people;
  const range = document.getElementById(id);
  if (!range) return;
  range.oninput = () => {
    const v = +range.value;
    document.getElementById(id + '-l').textContent = `${personLabelRaw(p1)} ${v}%`;
    document.getElementById(id + '-r').textContent = `${personLabelRaw(p2)} ${100 - v}%`;
    if (onChange) onChange(v);
  };
}
function segmentKeydown(event) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  const options = [...event.currentTarget.querySelectorAll('.seg-option')];
  const current = Math.max(0, options.indexOf(document.activeElement));
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1
    : (current + (event.key === 'ArrowRight' ? 1 : -1) + options.length) % options.length;
  event.preventDefault();
  options[next].focus();
  selectSegment(options[next]);
}
function readSeg(root, id) {
  const el = (id && root.querySelector('#' + id)) || root;
  return el?.querySelector('.seg-option.selected')?.dataset.value || null;
}
/* SINGLE source of truth for the partners / shared / out-of-scope segmented selector used
   everywhere (transaction editor, review queue, rule builder, split editor, dashboard
   perspective, tax claimant). Flexible by data: pass exactly the options a context needs —
   each { value, kind, label? }, where `kind` (see segInfo) drives the icon, colour and default
   label so partner 1/partner 2 always look the same. Read the choice with
   readSeg(root.querySelector('.' + className)) or readSeg(root, id). */
function personSegment(id, options, selected, className = 'person-seg') {
  const opts = options.map(o => {
    const info = segInfo(o.kind);
    return [o.value, `<md-icon class="seg-icon" style="color:${info.color}">${info.icon}</md-icon>${esc(o.label || info.label)}`];
  });
  return segControl(id, opts, selected, className, true);
}

function sharingOptions(id, selected, className = '') {
  const options = [
    { value: 'shared', kind: 'shared' },
    ...state.meta.people.map(p => ({ value: `personal:${p}`, kind: `person:${p}` })),
    { value: 'out-of-scope', kind: 'out-of-scope' },
  ];
  return personSegment(id, options, selected || 'shared', ['sharing-field', className].filter(Boolean).join(' '));
}

function tooltip(text) { return `data-tip="${text.replace(/"/g, '&quot;')}"`; }

/* Material field primitives. Views render through these helpers so labels,
   validation, dark mode and future field changes stay consistent everywhere. */
function textField({ id = '', label = '', value = '', type = 'text', className = '', placeholder = '', supportingText = '', attrs = '' }) {
  return `<md-outlined-text-field ${id ? `id="${esc(id)}"` : ''} class="${esc(className)}" label="${esc(label)}" type="${esc(type)}" value="${esc(value)}" ${placeholder ? `placeholder="${esc(placeholder)}"` : ''} ${supportingText ? `supporting-text="${esc(supportingText)}"` : ''} ${attrs}></md-outlined-text-field>`;
}

function localDateISO(d = new Date()) {
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function selectField({ id = '', label = '', value = '', options = [], className = '', attrs = '' }) {
  return `<md-outlined-select ${id ? `id="${esc(id)}"` : ''} class="${esc(className)}" label="${esc(label)}" ${attrs}>
    ${options.map(opt => {
      const [v, headline, supporting = ''] = opt;
      return `<md-select-option value="${esc(v)}" ${String(v) === String(value) ? 'selected' : ''}>
        <div slot="headline">${esc(headline)}</div>${supporting ? `<div slot="supporting-text">${esc(supporting)}</div>` : ''}
      </md-select-option>`;
    }).join('')}
  </md-outlined-select>`;
}

function checkboxField({ id = '', label, checked = false, className = '', attrs = '' }) {
  return `<label class="md-check ${esc(className)}"><md-checkbox ${id ? `id="${esc(id)}"` : ''} touch-target="wrapper" ${checked ? 'checked' : ''} ${attrs}></md-checkbox><span>${esc(label)}</span></label>`;
}

function fileField({ id = '', label = T('Choose file'), className = '', accept = '', multiple = false }) {
  return `<span class="md-file-field ${esc(className)}">
    <input type="file" ${id ? `id="${esc(id)}"` : ''} class="md-file-input" ${accept ? `accept="${esc(accept)}"` : ''} ${multiple ? 'multiple' : ''} hidden>
    <md-outlined-button type="button" class="md-file-pick"><md-icon slot="icon">attach_file</md-icon>${esc(label)}</md-outlined-button>
    <span class="md-file-name type-caption" style="color:var(--ink2)">${T('No file chosen')}</span>
  </span>`;
}

function wireFileField(root, onChange) {
  root.querySelectorAll('.md-file-field').forEach(host => {
    const input = host.querySelector('.md-file-input');
    host.querySelector('.md-file-pick').onclick = () => input.click();
    input.onchange = () => {
      host.querySelector('.md-file-name').textContent = input.files.length === 1 ? input.files[0].name : `${input.files.length} files selected`;
      onChange && onChange(input);
    };
  });
}

function attachTooltips() {
  const tip = $('#tooltip');
  document.querySelectorAll('[data-tip]').forEach(el => {
    el.onmousemove = e => { tip.style.display = 'block'; tip.textContent = el.dataset.tip; tip.style.left = (e.clientX + 14) + 'px'; tip.style.top = (e.clientY + 14) + 'px'; };
    el.onmouseleave = () => tip.style.display = 'none';
  });
  wireNotePopovers();
}

/* Note popovers live inside the month accordions, whose `overflow:hidden` (needed for the
   height animation) clips anything that pops above the top row. So position the card as
   `fixed` on hover — it then escapes every clipping context and always floats on top.
   Kept open while the pointer is over the trigger OR the card, so its buttons stay usable. */
function wireNotePopovers() {
  document.querySelectorAll('.note-popover-container').forEach(c => {
    if (c._popWired) return; c._popWired = true;
    const pop = c.querySelector('.note-popover');
    if (!pop) return;
    let hideTimer;
    const show = () => {
      clearTimeout(hideTimer);
      pop.style.visibility = 'visible'; pop.style.opacity = '1';
      const r = c.getBoundingClientRect(), pr = pop.getBoundingClientRect();
      const left = Math.max(8, Math.min(r.left + r.width / 2 - pr.width / 2, window.innerWidth - pr.width - 8));
      const above = r.top - pr.height - 8;
      pop.style.left = left + 'px';
      pop.style.top = (above < 8 ? r.bottom + 8 : above) + 'px';   // flip below if no room above
    };
    const hide = () => { hideTimer = setTimeout(() => { pop.style.visibility = 'hidden'; pop.style.opacity = '0'; }, 140); };
    c.addEventListener('mouseenter', show);
    c.addEventListener('mouseleave', hide);
    pop.addEventListener('mouseenter', () => clearTimeout(hideTimer));
    pop.addEventListener('mouseleave', hide);
  });
}

/* =========================================================================
   SHARED FIELD COMPONENTS
   One implementation per concept, used by every page. If you need a tax
   picker / note editor / owner selector / year-cost switch / modal anywhere,
   call these — never hand-roll a second copy. See AGENTS.md.
   ========================================================================= */

/* ---- modal shell ---- : the ONE Material dialog. Returns the dialog element.
   `body` and `actions` are HTML strings; `onMount(root)` wires listeners. */
/* Dialogs size to their content and are capped at 90vh — a short modal should not
   be padded out to a fixed height, and a long one should scroll its body rather
   than run off the screen. There is deliberately no height option: one rule for
   every dialog on the site. */
const DIALOG_MAX_HEIGHT = '90vh';

function openModal({ title, body, actions = '', width = '', onMount, onClose }) {
  const dialog = document.createElement('md-dialog');
  dialog.className = 'app-dialog';
  // A modal opening while another is already up needs its own shadow, or the two
  // stacked surfaces read as one flat sheet.
  const stacked = !!document.querySelector('.app-dialog');
  if (stacked) dialog.classList.add('app-dialog-stacked');
  if (width) dialog.style.setProperty('--app-dialog-width', width);
  dialog.innerHTML = `<div slot="headline">${esc(title)}</div>
    <div slot="content" class="generic-modal-body">${body}</div>
    ${actions ? `<div slot="actions" class="generic-modal-footer">${actions}</div>` : ''}`;
  const close = () => dialog.close();
  dialog._close = close;
  dialog.addEventListener('closed', () => { dialog.remove(); onClose && onClose(); }, { once: true });
  document.body.appendChild(dialog);
  dialog.show();
  // md-dialog caps its inner <dialog> at 560px; widen it (still viewport-bound) so
  // wide pickers size to content and stay centered instead of overflowing top-left.
  requestAnimationFrame(() => {   // center every dialog (md-dialog can land top-left otherwise)
    const inner = dialog.shadowRoot && dialog.shadowRoot.querySelector('dialog');
    if (!inner) return;
    if (width) inner.style.inlineSize = inner.style.maxInlineSize = `min(${width}, calc(100vw - 48px))`;
    // Cap only: block-size stays auto so the dialog is as tall as its content.
    inner.style.maxBlockSize = `min(${DIALOG_MAX_HEIGHT}, calc(100vh - 40px))`;
    inner.style.margin = 'auto';
    if (stacked) {
      const container = dialog.shadowRoot.querySelector('.container') || inner;
      container.style.boxShadow = 'var(--dialog-stack-shadow)';
    }
  });
  if (onMount) onMount(dialog);
  return dialog;
}

/* ---- toast ---- : the ONE transient confirmation. Feedback used to be a modal with an OK
   button, which made every saved edit cost a click and taught people to dismiss without
   reading. A toast says the same thing in the corner and leaves. It lives on <body>, not in
   #main, so it survives the re-render that usually follows the action it is reporting.
   Errors get longer on screen and a close button: a message you may need to act on must not
   be able to vanish before you have read it. */
const TOAST_MS = { good: 4000, info: 5000, bad: 9000 };

function toastHost() {
  let host = $('#toast-host');
  if (!host) {
    host = document.createElement('div');
    host.id = 'toast-host';
    // polite: announced after whatever the user is doing, never interrupting it
    host.setAttribute('aria-live', 'polite');
    host.setAttribute('aria-atomic', 'false');
    document.body.appendChild(host);
  }
  return host;
}

function toast(message, { kind = 'good', icon = '', timeout = 0 } = {}) {
  const marks = { good: 'check_circle', bad: 'error', info: 'info' };
  const el = document.createElement('div');
  el.className = `toast toast-${kind}`;
  el.setAttribute('role', kind === 'bad' ? 'alert' : 'status');
  el.innerHTML = `<md-icon class="toast-icon">${esc(icon || marks[kind] || marks.good)}</md-icon>
    <span class="toast-text type-body-small">${esc(message)}</span>
    <md-icon-button class="toast-close" aria-label="${T('Dismiss')}"><md-icon>close</md-icon></md-icon-button>`;
  const close = () => {
    if (el.dataset.closing) return;
    el.dataset.closing = '1';
    el.classList.add('toast-out');
    setTimeout(() => el.remove(), 180);
  };
  el.querySelector('.toast-close').onclick = close;
  toastHost().appendChild(el);
  // Oldest first: a burst of saves must not push the newest message off the screen.
  const host = toastHost();
  while (host.children.length > 4) host.firstElementChild.remove();
  const ms = timeout || TOAST_MS[kind] || TOAST_MS.good;
  const timer = setTimeout(close, ms);
  el.addEventListener('mouseenter', () => clearTimeout(timer));   // reading time is not a race
  el.addEventListener('mouseleave', () => setTimeout(close, 1500));
  return { close };
}

function showMessage(message, { kind = 'good', icon = '' } = {}) { return toast(message, { kind, icon }); }
function showError(message) { return toast(message, { kind: 'bad' }); }

function confirmAction({ title, body, confirmLabel = T('Confirm'), danger = false, onConfirm }) {
  return openModal({
    title, body: `<div class="type-body-small" style="color:var(--ink2)">${body}</div>`,
    actions: `<md-text-button class="confirm-cancel">${T('Cancel')}</md-text-button><md-filled-button class="confirm-go" ${danger ? 'style="--md-filled-button-container-color:var(--bad)"' : ''}>${esc(confirmLabel)}</md-filled-button>`,
    onMount: root => {
      root.querySelector('.confirm-cancel').onclick = () => root._close();
      root.querySelector('.confirm-go').onclick = async () => { await onConfirm(); root._close(); };
    },
  });
}

function promptText({ title, label, value = '', confirmLabel = T('Save'), onConfirm }) {
  return openModal({
    title, body: textField({ label, value, className: 'prompt-value w-full' }),
    actions: `<md-text-button class="prompt-cancel">${T('Cancel')}</md-text-button><md-filled-button class="prompt-save">${esc(confirmLabel)}</md-filled-button>`,
    onMount: root => {
      const field = root.querySelector('.prompt-value');
      root.querySelector('.prompt-cancel').onclick = () => root._close();
      root.querySelector('.prompt-save').onclick = async () => {
        const next = field.value.trim();
        if (!next) { field.error = true; field.errorText = T('Required'); return; }
        await onConfirm(next); root._close();
      };
      setTimeout(() => field.focus(), 30);
    },
  });
}

/* ---- choice picker ---- : the ONE Material list modal for selecting a value.
   Tax buckets, staged accounts and PDF extractors all use this layout. */
function openChoicePicker({ title, current = '', options, onPick }) {
  const optionHtml = options.map((opt, i) => `<md-list-item type="button" data-choice-index="${i}" class="${opt.value === current ? 'selected' : ''}">
    ${opt.icon ? `<md-icon slot="start">${esc(opt.icon)}</md-icon>` : ''}
    <div slot="headline">${esc(opt.label)}</div>
    ${opt.note ? `<div slot="supporting-text">${esc(opt.note)}</div>` : ''}
    ${opt.value === current ? '<md-icon slot="end">check</md-icon>' : ''}
  </md-list-item>`).join('');
  return openModal({
    title,
    body: `<md-list class="choice-list">${optionHtml}</md-list>`,
    actions: `<md-text-button class="choice-close">${T('Close')}</md-text-button>`,
    onMount: root => {
      root.querySelector('.choice-close').onclick = () => root._close();
      root.querySelectorAll('[data-choice-index]').forEach(btn => {
        btn.onclick = () => {
          const option = options[Number(btn.dataset.choiceIndex)];
          root._close();
          onPick(option.value, option);
        };
      });
    },
  });
}

/* ---- accordion ---- : the ONE expansion panel (Transactions months, Review
   groups, anywhere collapsible). Real height animation via grid-template-rows;
   @material/web ships no expansion-panel, so this is the house component.
   `onToggle` is a JS snippet run after toggling, with `this` = the header el. */
function accordion({ headerHtml, bodyHtml, open = true, cls = '', attrs = '', onToggle = '' }) {
  return `<div class="acc ${cls}${open ? ' open' : ''}" ${attrs}>
    <div class="acc-head" onclick="toggleAccordion(this.closest('.acc'));${onToggle}">
      <md-icon class="acc-chevron">expand_more</md-icon>
      <div class="acc-head-main flex items-center gap-3 flex-1 min-w-0">${headerHtml}</div>
    </div>
    <div class="acc-body-wrap"><div class="acc-body">${bodyHtml}</div></div>
  </div>`;
}
function toggleAccordion(acc) { acc.classList.toggle('open'); }
function setAccordion(acc, open) { acc.classList.toggle('open', open); }

/* ---- tax bucket field ---- : the ONE tax picker (Review, Transactions edit,
   Split rows all use this). Render with taxField(slug); read with readTax(). */
function taxLabel(slug) {
  if (!slug) return 'Tax relevant';
  const b = state.meta.tax_buckets.find(x => x.slug === slug);
  return T('Tax:') + ' ' + (b ? b.name : slug);
}
function taxBucketSummary(bucket) {
  return bucket.note || bucket.rule || T('Review this item for possible tax relevance.');
}
function taxField(slug) {
  slug = slug || '';
  return `<md-outlined-button type="button" class="tax-field${slug ? ' filled' : ''}" data-tax="${esc(slug)}" onclick="openTaxPicker(this)">${esc(taxLabel(slug))}</md-outlined-button>`;
}
function readTax(root) { const b = root.querySelector('.tax-field'); return b ? (b.dataset.tax || null) : null; }
function setTaxField(btn, slug) {
  btn.dataset.tax = slug || '';
  btn.classList.toggle('filled', !!slug);
  btn.textContent = taxLabel(slug);
  const review = btn.closest('.tax-review-fields');
  if (review) {
    const confirmed = review.querySelector('.tax-confirmed-field');
    confirmed.disabled = !slug;
    confirmed.selected = !!slug;
  }
}
function openTaxPicker(btn) {
  const current = btn.dataset.tax || '';
  const buckets = state.meta.tax_buckets;
  return openChoicePicker({
    title: T('Select tax category'),
    current,
    options: [
      { value: '', label: T('Not tax relevant'), note: T('(Default) Removes any tax bucket assignment'), icon: 'block' },
      ...buckets.map(b => ({ value: b.slug, label: b.name, note: taxBucketSummary(b), icon: 'receipt_long' })),
    ],
    onPick: slug => setTaxField(btn, slug || null),
  });
}

/* ---- tax review fields ---- : bucket + explicit confirmation + claimant.
   Category mappings are candidates until confirmed here. All edit surfaces
   use this component and readTaxReview(); never recreate these controls. */
function taxOwnerField(current) {
  const options = [
    { value: 'couple', kind: 'both' },
    ...state.meta.people.map(p => ({ value: p, kind: `person:${p}` })),
  ];
  return `<div class="scroll-x">${personSegment('tax-owner', options, current || '', 'tax-owner-field')}</div>`;
}
function taxReviewFields(t) {
  const hasSplitTax = !!(t.splits || []).some(part => part.tax_bucket);
  const hasBucket = !!t.tax_bucket || hasSplitTax;
  const candidate = hasBucket && !t.tax_confirmed;
  return `<div class="tax-review-fields card p-4" data-split-tax="${hasSplitTax ? '1' : '0'}">
    <div class="flex items-center gap-3 flex-wrap">
      ${hasSplitTax && !t.tax_bucket ? `<span class="chip chip-primary"><md-icon>call_split</md-icon>${T('Tax category set on split parts')}</span>` : taxField(t.tax_bucket)}
      <label class="type-label flex items-center gap-2" style="color:var(--ink2)">
        <md-switch class="tax-confirmed-field" ${t.tax_confirmed ? 'selected' : ''} ${hasBucket ? '' : 'disabled'}></md-switch>
        ${T('Confirm for tax export')}
      </label>
      ${candidate ? `<span class="chip chip-warn">${T('candidate')}</span>` : ''}
    </div>
    <div class="type-label mt-3 mb-1" style="color:var(--ink2)">${T('Claimed by — choose explicitly')}</div>
    ${taxOwnerField(t.tax_owner)}
    ${t.tax_bucket_source === 'category-map' ? `<div class="type-caption mt-2" style="color:var(--ink2)">${T('Suggested automatically from the expense category. Check eligibility before confirming.')}</div>` : ''}
  </div>`;
}
function readTaxReview(root) {
  const bucket = readTax(root);
  const review = root.querySelector('.tax-review-fields');
  const hasTax = !!bucket || review?.dataset.splitTax === '1';
  const confirmed = root.querySelector('.tax-confirmed-field');
  return {
    tax_bucket: bucket,
    tax_confirmed: !!(hasTax && confirmed && confirmed.selected),
    tax_owner: hasTax ? readSeg(root.querySelector('.tax-owner-field')) : null,
  };
}

/* ---- owner field ---- : the ONE income owner selector. Its initial selection
   is the bank account owner; there is no ambiguous "From account" UI state.
   Out of scope lives in this same mutually-exclusive segmented control. */
function accountOwner(accountId) {
  return state.meta.accounts.find(a => a.id === accountId)?.owner || 'couple';
}
function ownerField(id, current, accountId, sharing = 'shared') {
  const options = [
    { value: 'couple', kind: 'both' },
    ...state.meta.people.map(p => ({ value: p, kind: `person:${p}` })),
    { value: 'out-of-scope', kind: 'out-of-scope' },
  ];
  const selected = sharing === 'out-of-scope' ? 'out-of-scope' : (current || accountOwner(accountId));
  return personSegment(id, options, selected, 'owner-field');
}
function readOwner(root) {
  const value = readSeg(root.querySelector('.owner-field'));
  return value && value !== 'out-of-scope' ? value : null;
}

/* Read the sharing decision from any edit context: income includes out of scope
   in its owner segments; expenses use the sharing segmented control. */
function readSharingCtx(root) {
  const owner = root.querySelector('.owner-field');
  if (owner) return readSeg(owner) === 'out-of-scope' ? 'out-of-scope' : 'shared';
  return readSeg(root.querySelector('.sharing-field')) || 'shared';
}

/* ---- switch field ---- : the ONE labeled Material switch renderer/reader. */
function switchField({ id = '', label, on = false, className = '' }) {
  return `<label class="type-label flex items-center gap-2" style="color:var(--ink2)"><md-switch ${id ? `id="${esc(id)}"` : ''} class="switch-field ${esc(className)}" ${on ? 'selected' : ''}></md-switch>${esc(label)}</label>`;
}
function readSwitch(root, className) {
  const field = root.querySelector(`.${className}`);
  return !!(field && field.selected);
}

/* ---- year-cost switch ---- : the ONE year-cost toggle. */
function yearCostSwitch(id, on) {
  return switchField({ id, label: T('year cost'), on, className: 'yc-field' });
}
function readYearCost(root) { return readSwitch(root, 'yc-field'); }

function lowActivitySwitch(on) {
  return switchField({ label: T("Low activity (don't warn about empty months)"), on, className: 'low-activity-field' });
}
function readLowActivity(root) { return readSwitch(root, 'low-activity-field'); }

/* ---- statement coverage ---- : one renderer and one gap calculation shared
   by the Dashboard card and close-period confirmation gates. */
function coverageActiveMonth(data, month) {
  const [first, last] = data.active_range || [];
  return Number.isInteger(first) && Number.isInteger(last) && month >= first && month <= last;
}
function coverageGapsFor(data, month) {
  if (!coverageActiveMonth(data, month)) return [];
  return (data.accounts || []).filter(account => !account.low_activity && !(account.months[month - 1] > 0))
    .map(account => ({ id: account.id, label: accountLabel(account.id) }));
}
function anchorStatusChip(summary) {
  const value = summary || { status: 'none', detail: T('No consecutive balance anchors') };
  if (value.status === 'ok') {
    return `<span class="chip chip-good coverage-anchor" ${tooltip(esc(value.detail))}><md-icon>check</md-icon>${T('reconciled')}</span>`;
  }
  if (value.status === 'mismatch') {
    return `<span class="chip chip-bad coverage-anchor" ${tooltip(esc(value.detail))}><md-icon>close</md-icon>${T('balance mismatch')}</span>`;
  }
  return `<span class="chip chip-neutral coverage-anchor" ${tooltip(esc(value.detail))}>${T('no anchor')}</span>`;
}
function coverageCard(data) {
  const rows = (data.accounts || []).map(account => {
    const label = accountLabel(account.id);
    const reported = account.reported || [];
    const cells = account.months.map((count, index) => {
      const month = index + 1;
      // A statement was imported for this month and it carried no activity — that
      // is answered, not missing, so it must not read as an alert.
      const emptyReported = count === 0 && reported[index];
      const stateClass = count > 0 ? 'present'
        : emptyReported ? 'reported'
          : (!account.low_activity && coverageActiveMonth(data, month) ? 'missing' : 'muted');
      const detail = count > 0 ? T('{n} transactions', { n: count })
        : emptyReported ? T('statement imported — no activity')
          : T('no transactions');
      return `<span class="coverage-cell ${stateClass}" data-account="${esc(account.id)}" data-month="${month}" ${tooltip(esc(`${label} · ${T(MONTH_NAMES[index])}: ${detail}`))}>${count}</span>`;
    }).join('');
    return `<div class="coverage-account type-body-small truncate">${esc(label)}</div>${cells}${anchorStatusChip((data.anchors || {})[account.id])}`;
  }).join('');
  return `<div class="card p-5 mb-6" id="coverage-card">
    <h2 class="font-medium mb-1">${T('Statement coverage')}</h2>
    <div class="type-caption mb-3" style="color:var(--ink2)">${T('Transaction counts per account and month. Red cells may indicate a missing statement.')}</div>
    <div class="scroll-x"><div class="coverage-grid">
      <div class="coverage-account type-label" style="color:var(--ink2)">${T('Account')}</div>
      ${MONTHS.map(month => `<div class="coverage-month type-label">${T(month)}</div>`).join('')}
      <div class="coverage-month type-label">${T('Balance')}</div>
      ${rows}
    </div></div>
  </div>`;
}

/* ---- note editor ---- : the ONE note modal. Two thin entry points share it:
   openNote() persists to the API; openLocalNote() writes to a [data-note] host
   in the DOM (split rows, rule modal). Same modal, same escaping. */
function noteModal(current, onSave) {
  return openModal({
    title: current ? T('Edit note') : T('Add note'),
    body: `<md-outlined-text-field class="n-text w-full" type="textarea" rows="4" label="${T('Note')}" value="${esc(current || '')}" placeholder="${T('Type your note here...')}"></md-outlined-text-field>`,
    actions: `<md-text-button class="n-cancel">${T('Cancel')}</md-text-button><md-filled-button class="n-save">${T('Save')}</md-filled-button>`,
    onMount: root => {
      root.querySelector('.n-cancel').onclick = () => root._close();
      root.querySelector('.n-save').onclick = () => { onSave(root.querySelector('.n-text').value.trim()); root._close(); };
    },
  });
}
async function openNote(id, current) {
  noteModal(current, async text => { await api('/api/decision', { year: state.year, id, fields: { note: text || null } }); render(); });
}
/* Edit a note held in the DOM (saved later with the surrounding form) rather than
   posted immediately. The trigger is either a bare md-icon (rules form) or a
   button carrying an icon plus a label (transaction editor), and the host may
   show the note text, so refresh whichever of those is present. */
function openLocalNote(btn) {
  const host = btn.closest('[data-note]');
  if (!host) return;
  noteModal(host.dataset.note || '', text => {
    host.dataset.note = text;
    const icon = btn.tagName === 'MD-ICON' ? btn : btn.querySelector('md-icon');
    if (icon) icon.textContent = text ? 'edit_note' : 'note_add';
    const label = btn.querySelector('[data-note-label]');
    if (label) label.textContent = text ? T('Edit note') : T('Add note');
    const display = host.querySelector('[data-note-text]');
    if (display) {
      display.textContent = text;
      display.hidden = !text;
    }
  });
}

/* ---------- dashboard ---------- */
function statTile(label, value, color) {
  return `<div class="card p-5 flex-1" style="min-width:150px">
    <div class="stat-label">${label}</div>
    <div class="stat-value" style="color:${color || 'var(--ink)'}">${value}</div></div>`;
}

/* View sentinels for the dashboard tabs: 0 = whole year, 'yc' = year costs,
   1..12 = a month. state.dashView holds the current one. */
function dashViewToIndex(v) { return v === 0 ? 0 : v === 'yc' ? 1 : v + 1; }
function dashIndexToView(i) { return i === 0 ? 0 : i === 1 ? 'yc' : i - 1; }

async function renderDashboard(renderId = state.renderId) {
  const tab = 'dashboard';
  const year = state.year;
  const scope = state.dashScope || 'all';
  const s = await cachedYearData('summary', `/api/summary?year=${year}&scope=${scope}`);
  if (!renderIsCurrent(renderId, tab, year)) return;
  setReviewBadge(s.needs_review);

  const monthClosed = m => s.months_state[`${year}-${String(m).padStart(2, '0')}`] === 'closed';
  const allMonthsClosed = () => [1,2,3,4,5,6,7,8,9,10,11,12].every(monthClosed);
  // A period is settled only while its figures still match what was settled. Drift is
  // the app disagreeing with its own record, so it replaces the lock rather than
  // sitting beside it: a period that has moved should not read as closed.
  const drift = s.drift || {};
  const monthKey = m => `${year}-${String(m).padStart(2, '0')}`;
  const monthDrifted = m => Array.isArray(drift[monthKey(m)]);
  const yearDrifted = () => Array.isArray(drift.annual)
    || [1,2,3,4,5,6,7,8,9,10,11,12].some(monthDrifted);
  const tabLabel = (text, closed, drifted) => `<span class="tab-label">${text}${
    drifted ? `<md-icon class="dash-lock" style="color:var(--bad)" title="${T('No longer matches what was settled')}">error</md-icon>`
    : closed ? `<md-icon class="dash-lock" title="${T('closed')}">lock</md-icon>` : ''}</span>`;

  function driftBannerHtml(view) {
    const isYear = (view === 0 || view === 'yc');
    const keys = isYear
      ? Object.keys(drift)
      : (monthDrifted(view) ? [monthKey(view)] : []);
    if (!keys.length) return '';
    const rows = keys.sort().map(key => `<li class="mb-1"><b>${esc(key === 'annual' ? T('Annual settlement') : key)}</b> — ${esc((drift[key] || []).join('; '))}</li>`).join('');
    return `<div class="card p-4 mb-6" style="border-left:4px solid var(--bad)">
        <h2 class="type-title-small mb-1" style="color:var(--bad)">${T('This period no longer matches what was settled')}</h2>
        <p class="type-body-small mb-2" style="color:var(--ink2)">${T('Closing a period records its figures. Something has changed them since — often a correction, sometimes not. Check it is what you intended, then accept it or put it back.')}</p>
        <ul class="type-body-small mb-3" style="margin-left:1.1rem;list-style:disc">${rows}</ul>
        <div class="flex gap-2 flex-wrap">
          ${keys.map(key => `<md-filled-button onclick="acceptClosing('${esc(key)}')">
            <md-icon slot="icon">check</md-icon>${T('Accept {period}', { period: key === 'annual' ? T('the year') : key })}</md-filled-button>`).join('')}
        </div>
      </div>`;
  }

  // perspective selector: Together | Shared | <each person> — a clean partition
  const cap = w => w[0].toUpperCase() + w.slice(1);
  const scopeOpts = [{ value: 'all', kind: 'together' }, { value: 'shared', kind: 'shared' },
    ...state.meta.people.map(p => ({ value: p, kind: `person:${p}` }))];

  $('#main').innerHTML = `
    <div class="page-sticky">
      <md-tabs id="dash-tabs" class="mb-2">
        <md-primary-tab data-v="0">${tabLabel(T('Year {y}', { y: year }), allMonthsClosed(), yearDrifted())}</md-primary-tab>
        <md-primary-tab data-v="yc">${tabLabel(T('Year costs'), allMonthsClosed(), yearDrifted())}</md-primary-tab>
        ${MONTHS.map((mn, i) => `<md-primary-tab data-v="${i + 1}">${tabLabel(`${String(i + 1).padStart(2, '0')} ${T(mn)}`, monthClosed(i + 1), monthDrifted(i + 1))}</md-primary-tab>`).join('')}
      </md-tabs>
      <div class="flex items-center gap-3 flex-wrap">
        <span class="type-label" style="color:var(--ink2)">${T('Whose money')}</span>
        ${personSegment('dash-scope', scopeOpts, scope, 'seg-scope')}
        <span id="close-toggle" class="ml-auto"></span>
      </div>
    </div>
    <div id="dash-body" class="mt-8"></div>`;
  $('#dash-scope').addEventListener('change', () => {
    state.dashScope = readSeg($('#dash-scope')) || 'all';
    render();
  });

  let drawSeq = 0;               // guards async chart fills against tab switches
  let yoyData = null;            // cached /api/yoy response for the chart/table toggle

  /* ---- small builders reused across views ---- */
  const chartCard = (title, canvasId, { tall = false, header = '', note = '' } = {}) =>
    `<div class="card p-5"><div class="flex items-center justify-between gap-3 flex-wrap mb-3"><h2 class="font-medium">${title}</h2>${header}</div>
      ${note ? `<div class="type-caption mb-3" style="color:var(--ink2)">${note}</div>` : ''}
      <div class="chart-box${tall ? ' tall' : ''}"><canvas id="${canvasId}"></canvas></div></div>`;

  const tilesHtml = (view, data) => {
    if (view === 'yc') {
      return statTile(T('Year cost total'), fmt(-data.expenses), 'var(--bad)') +
        (data.income ? statTile(T('Offsets'), fmt(data.income), 'var(--good)') : '') +
        statTile(T('Net'), fmt(data.savings), data.savings >= 0 ? 'var(--good)' : 'var(--bad)') +
        statTile(T('Transactions'), String(data.transactions || 0));
    }
    const rate = data.income > 0 ? Math.round(100 * data.savings / data.income) + ' %' : '–';
    return statTile(T('Income'), fmt(data.income), 'var(--good)') +
      statTile(T('Expenses'), fmt(-data.expenses), 'var(--bad)') +
      statTile(T('Savings'), fmt(data.savings), data.savings >= 0 ? 'var(--good)' : 'var(--bad)') +
      statTile(T('Savings rate'), rate, data.savings >= 0 ? 'var(--good)' : 'var(--bad)') +
      (typeof view === 'number' && view !== 0 ? statTile(T('Year costs (excluded)'), fmt(-data.year_costs_excluded)) : '') +
      statTile(T('Needs review'), String(data.needs_review || 0), data.needs_review ? 'var(--bad)' : 'var(--good)');
  };

  // labeled toggle: outlined "Close …" when open; blue filled "Reopen …" when locked
  const closeBtnHtml = view => {
    const isYear = (view === 0 || view === 'yc');
    const closed = isYear ? allMonthsClosed() : monthClosed(view);
    const action = isYear ? `toggleYear('${closed ? 'open' : 'closed'}')` : `toggleMonth(${view}, '${closed ? 'open' : 'closed'}')`;
    // Available beside close/reopen so the check can be run on its own, without
    // having to open and reclose a period to see what it would say.
    const check = `<md-text-button class="close-btn" onclick="openPeriodCheck()">
      <md-icon slot="icon">health_and_safety</md-icon>${T('Run check')}</md-text-button>`;
    return check + (closed
      ? `<md-filled-button class="close-btn" onclick="${action}"><md-icon slot="icon">lock</md-icon>${T(isYear ? 'Reopen year' : 'Reopen month')}</md-filled-button>`
      : `<md-outlined-button class="close-btn" onclick="${action}"><md-icon slot="icon">lock_open</md-icon>${T(isYear ? 'Close year' : 'Close month')}</md-outlined-button>`);
  };

  // The 15 biggest single costs, travel and rent set aside so the everyday
  // spend is legible. Full width, category-coloured horizontal bars.
  const EXCLUDED_COST_SLUGS = new Set(['living-costs/cold-rent', 'living-costs/nebenkosten']);
  const isExcludedCost = slug => slug.split('/')[0] === 'traveling' || EXCLUDED_COST_SLUGS.has(slug);
  const topCostsCardHtml = view => `
    <div class="card p-5 mb-6">
      <h2 class="font-medium mb-3">${T('Top 15 costs — {period}', { period: view === 0 ? year : T(MONTHS[view - 1]) })}
        <span class="type-caption" style="color:var(--ink2)">${T('· excl. travel &amp; rent')}</span></h2>
      <div class="chart-box tall"><canvas id="cat-canvas"></canvas></div></div>`;
  const allSubcatsCardHtml = view => `
    <div class="card p-5 mb-6">
      <h2 class="font-medium mb-3">${T('All subcategories — {period}', { period: view === 0 ? year : T(MONTHS[view - 1]) })}</h2>
      <div class="chart-box tall"><canvas id="allsub-canvas"></canvas></div></div>`;

  /* ---- chart drawers (read canvases already in #dash-body) ---- */
  const drawFlow = data => {
    const c = $('#flow-canvas'); if (!c) return;
    const t = CHART_COLORS(), surplus = data.savings;
    mkChart(c, {
      type: 'bar',
      data: { labels: [T('Money in'), T('Costs'), T('Surplus')],
        datasets: [{ data: [data.income, -data.expenses, surplus],
          backgroundColor: [t.good, t.bad, surplus >= 0 ? t.good : t.bad], borderRadius: 6, maxBarThickness: 96 }] },
      options: { plugins: { legend: { display: false } } },
    });
  };
  // "Where the money goes" — one slice per MAIN category (subs aggregated up),
  // labelled and tooltipped as a share of total expenses, category-coloured.
  const drawPie = data => {
    const c = $('#pie-canvas'); if (!c) return;
    const mainTotals = {};
    Object.entries(data.by_category).forEach(([slug, v]) => {
      if (v >= 0) return;
      const main = slug.split('/')[0];
      mainTotals[main] = (mainTotals[main] || 0) + -v;
    });
    const mains = Object.entries(mainTotals).sort((a, b) => b[1] - a[1]);
    const total = mains.reduce((sum, [, v]) => sum + v, 0);
    if (!total) { c.closest('.chart-box').innerHTML = `<div class="type-body-small" style="color:var(--ink2)">${T('No expenses.')}</div>`; return; }
    const pct = v => Math.round(100 * v / total);
    const labels = mains.map(([k, v]) => `${groupName(k)} · ${pct(v)} %`);
    const values = mains.map(([, v]) => v);
    const colors = mains.map(([k]) => resolveColor(catColorFor(k)));
    mkChart(c, {
      type: 'doughnut',
      data: { labels, datasets: [{ data: values, backgroundColor: colors, borderWidth: 0 }] },
      options: { cutout: '58%', plugins: { legend: { position: 'right', labels: { padding: 10 } },
        tooltip: { callbacks: { label: ctx => `${groupName(mains[ctx.dataIndex][0])}: ${pct(ctx.parsed)} %` } } } },
    });
  };
  /* Year costs (the e-bike effect) are kept out of every monthly figure on purpose, so the
     twelve months here summed to less than the year tiles above them — 28% less on a year
     with a big one-off — and nothing on screen said so. Spreading shares the year-cost slice
     evenly across the months and makes the chart reconcile with the tiles. It is a model, not
     a record: no month really paid a twelfth of an e-bike, which is why it is a switch and why
     the caption always states which of the two you are looking at.
     The divisor is the months that have actually happened — dividing a running year by twelve
     would push cost into months that do not exist yet. */
  const spreadWindow = () => {
    const now = new Date();
    return year === now.getFullYear() ? now.getMonth() + 1 : 12;
  };
  const spreadShare = () => {
    const yc = s.year_costs || { income: 0, expenses: 0 };
    const n = spreadWindow();
    return { n, income: (yc.income || 0) / n, expenses: (yc.expenses || 0) / n };
  };
  const spreadNote = () => {
    const yc = s.year_costs || { income: 0, expenses: 0 };
    const total = (yc.income || 0) + (yc.expenses || 0);
    if (!cents(total)) return T('No year costs in {year}, so spreading changes nothing.', { year });
    const { n, income, expenses } = spreadShare();
    return state.spreadYearCosts
      ? T('{total} of year costs spread over {n} months ({each} each). The months add up to the year totals above.',
        { total: fmt(-total), n, each: fmt(-(income + expenses)) })
      : T('Excludes {total} of year costs, which the totals above do include. See the Year costs tab.', { total: fmt(-total) });
  };
  const drawByMonth = () => {
    const c = $('#bymonth-canvas'); if (!c) return;
    const t = CHART_COLORS();
    const { n, income: incShare, expenses: expShare } = spreadShare();
    // Only months that have happened carry a share, so the line never rises into the future.
    const share = (index, value) => (state.spreadYearCosts && index < n ? value : 0);
    const inc = s.months.map((m, i) => m.income + share(i, incShare));
    const exp = s.months.map((m, i) => -(m.expenses + share(i, expShare)));
    const sav = s.months.map((m, i) => m.savings + share(i, incShare + expShare));
    mkChart(c, {
      type: 'line',
      data: { labels: MONTHS.map(T), datasets: [
        { label: T('Income'), data: inc, borderColor: t.c1, backgroundColor: t.c1, tension: .3, pointRadius: 3 },
        { label: T('Expenses'), data: exp, borderColor: t.c2, backgroundColor: t.c2, tension: .3, pointRadius: 3 },
        { label: T('Surplus'), data: sav, borderColor: t.good, backgroundColor: t.good, tension: .3, pointRadius: 3, borderDash: [5, 4] },
      ] },
      options: { plugins: { legend: { position: 'top' } } },
    });
  };
  const toggleSpread = on => {
    state.spreadYearCosts = on;
    try { localStorage.setItem(SPREAD_KEY, on ? '1' : '0'); } catch (_) { /* private mode */ }
    const note = $('#bymonth-note'); if (note) note.innerHTML = spreadNote();
    drawByMonth();
  };
  const lastMonthWithData = () => {
    for (let m = 12; m >= 1; m--) { const d = s.months[m - 1]; if (d.income || d.expenses) return m; }
    return 12;
  };
  // Sankey: a single Income source -> main categories (+Savings) -> subcategories,
  // subcategories grouped under their main category and category-coloured. The
  // per-person view comes from the scope toggle, not a source split.
  const drawSankey = data => {
    const c = $('#sankey-canvas'); if (!c) return;
    const box = c.closest('.chart-box');
    const t = CHART_COLORS();
    const flows = [], labels = {}, colors = {}, column = {};
    const totalIncome = data.income || 0;
    // expense subcategories grouped under their main category
    const mainTotals = {}, subFlows = [];
    Object.entries(data.by_category).forEach(([slug, v]) => {
      if (v >= 0 || !slug.includes('/')) return;
      const mag = -v, main = slug.split('/')[0];
      mainTotals[main] = (mainTotals[main] || 0) + mag;
      subFlows.push({ from: main, to: slug, flow: mag });
      labels[slug] = subName(slug); colors[slug] = resolveColor(catColorFor(slug)); column[slug] = 2;
    });
    // Income -> each main category
    Object.entries(mainTotals).forEach(([main, tot]) => {
      flows.push({ from: 'income', to: main, flow: tot });
      labels[main] = groupName(main); colors[main] = resolveColor(catColorFor(main)); column[main] = 1;
    });
    flows.push(...subFlows);
    // Income -> Savings
    if (totalIncome > 0 && data.savings > 0) {
      flows.push({ from: 'income', to: 'savings', flow: data.savings });
      labels['savings'] = T('Savings'); colors['savings'] = t.good; column['savings'] = 1;
    }
    labels['income'] = totalIncome > 0 ? T('Income') : T('Spending'); colors['income'] = t.c1; column['income'] = 0;
    if (!flows.length || typeof Chart === 'undefined' || !Chart.registry.getController('sankey')) {
      box.innerHTML = `<div class="type-body-small" style="color:var(--ink2)">${T('Not enough data for a flow chart.')}</div>`;
      return;
    }
    box.style.height = Math.max(360, 70 + subFlows.length * 26) + 'px';
    const nc = k => colors[k] || t.ink2;
    mkChart(c, {
      type: 'sankey',
      data: { datasets: [{
        data: flows, labels, colors, column, borderWidth: 0, size: 'max',
        colorFrom: ctx => nc(flows[ctx.dataIndex].from), colorTo: ctx => nc(flows[ctx.dataIndex].to), colorMode: 'gradient',
      }] },
      options: { plugins: { legend: { display: false },
        tooltip: { callbacks: { label: ctx => { const f = flows[ctx.dataIndex]; return `${labels[f.from] || f.from} → ${labels[f.to] || f.to}: ${fmt(f.flow)}`; } } } } },
    });
  };
  const drawTrend = async (view, token) => {
    const endMonth = view === 0 ? lastMonthWithData() : view;
    const r = await api(`/api/trend?year=${year}&month=${endMonth}&n=6&scope=${scope}`);
    if (token !== drawSeq || !renderIsCurrent(renderId, tab, year)) return;
    const c = $('#trend-canvas'); if (!c) return;
    const t = CHART_COLORS();
    const labels = r.months.map(m => T(MONTHS[m.month - 1]) + (m.year !== year ? ` '${String(m.year).slice(2)}` : ''));
    mkChart(c, {
      type: 'line',
      data: { labels, datasets: [
        { label: T('Money in'), data: r.months.map(m => m.income), borderColor: t.good, backgroundColor: t.good, tension: .3 },
        { label: T('Costs'), data: r.months.map(m => -m.expenses), borderColor: t.bad, backgroundColor: t.bad, tension: .3 },
        { label: T('Surplus'), data: r.months.map(m => m.savings), borderColor: t.c1, backgroundColor: t.c1, tension: .3, borderDash: [5, 4] },
      ] },
      options: { plugins: { legend: { position: 'top' } } },
    });
  };

  // shared bar drawer for the two subcategory cost charts: horizontal, sorted
  // biggest-first, each bar the colour of its main category.
  const drawCostBars = (canvasId, entries) => {
    const c = $(`#${canvasId}`); if (!c) return;
    if (!entries.length) { c.closest('.chart-box').innerHTML = `<div class="type-body-small" style="color:var(--ink2)">${T('No expenses.')}</div>`; return; }
    mkChart(c, {
      type: 'bar',
      data: { labels: entries.map(([k]) => catName(k)),
        datasets: [{ data: entries.map(([, v]) => v), backgroundColor: entries.map(([k]) => resolveColor(catColorFor(k))), borderRadius: 4 }] },
      options: { indexAxis: 'y', plugins: { legend: { display: false } }, scales: { y: { ticks: { font: { size: 10 } } } } },
    });
  };
  const expenseEntries = data => Object.entries(data.by_category)
    .filter(([, v]) => v < 0).map(([k, v]) => [k, -v]).sort((a, b) => b[1] - a[1]);
  const drawTopCosts = data => drawCostBars('cat-canvas',
    expenseEntries(data).filter(([k]) => !isExcludedCost(k)).slice(0, 15));
  const drawAllSubcats = data => drawCostBars('allsub-canvas', expenseEntries(data));

  // Watch-list: subcategories flagged with the eye button on the Categories page.
  const watchedSlugs = () => new Set(state.meta.categories
    .filter(g => g.type !== 'income')
    .flatMap(g => g.subs.filter(x => x.watch).map(x => `${g.slug}/${x.slug}`)));
  const watchEmpty = c => { c.closest('.chart-box').innerHTML =
    `<div class="type-body-small" style="color:var(--ink2)">${T('No watched costs yet. Use the eye button on the Categories page to watch a subcategory.')}</div>`; };
  const drawWatchBar = data => {
    const c = $('#watch-bar-canvas'); if (!c) return;
    const watch = watchedSlugs();
    const entries = expenseEntries(data).filter(([k]) => watch.has(k));
    if (!entries.length) return watchEmpty(c);
    drawCostBars('watch-bar-canvas', entries);
  };
  const drawWatchPie = data => {
    const c = $('#watch-pie-canvas'); if (!c) return;
    const watch = watchedSlugs();
    if (!watch.size) return watchEmpty(c);
    const all = expenseEntries(data);
    const totalExp = all.reduce((sum, [, v]) => sum + v, 0);
    if (!totalExp) { c.closest('.chart-box').innerHTML = `<div class="type-body-small" style="color:var(--ink2)">${T('No expenses.')}</div>`; return; }
    const watched = all.filter(([k]) => watch.has(k)).reduce((sum, [, v]) => sum + v, 0);
    const rest = Math.max(0, totalExp - watched);
    const t = CHART_COLORS();
    const pct = v => Math.round(100 * v / totalExp);
    mkChart(c, {
      type: 'doughnut',
      data: { labels: [`${T('Watched')} · ${pct(watched)} %`, `${T('Rest')} · ${pct(rest)} %`],
        datasets: [{ data: [watched, rest], backgroundColor: [t.c1, t.ink2], borderWidth: 0 }] },
      options: { cutout: '58%', plugins: { legend: { position: 'bottom' },
        tooltip: { callbacks: { label: ctx => `${fmt(ctx.parsed)} · ${pct(ctx.parsed)} %` } } } },
    });
  };

  const fillFindings = (view, token) => {
    const host = $('#findings-card'); if (!host || typeof view !== 'number' || view === 0) return;
    api(`/api/findings?year=${year}&month=${view}`).then(f => {
      if (token !== drawSeq || !renderIsCurrent(renderId, tab, year)) return;
      const open = f.findings.map((x, idx) => ({ ...x, idx })).filter(x => !x.dismissed);
      if (!f.exists || !open.length) { host.innerHTML = ''; return; }
      const sevColor = { important: 'var(--bad)', check: '#a05a00', info: 'var(--ink2)' };
      host.innerHTML = `<div class="card p-5 mb-6" style="border-left:4px solid var(--chart-2)">
        <h2 class="font-medium mb-2">🔎 ${T('Anomaly reviewer')} — ${open.length} ${open.length > 1 ? T('findings') : T('finding')}</h2>
        ${open.map(x => `
          <div class="flex items-start gap-3 py-2 border-t type-body-small" style="border-color:var(--line)">
            <span class="chip chip-neutral shrink-0" style="color:${sevColor[x.severity] || 'var(--ink2)'}">${x.severity || 'check'}</span>
            <div class="flex-1"><div>${esc(x.issue)}</div>
              <div class="type-caption mt-0.5" style="color:var(--ink2)">→ ${esc(x.suggestion)}</div></div>
            <md-text-button class="shrink-0" onclick="dismissFinding(${view}, ${x.idx})">${T('Dismiss')}</md-text-button>
          </div>`).join('')}</div>`;
    });
  };

  const renderYoyBody = () => {
    const host = $('#yoy-body'); if (!host || !yoyData) return;
    const ys = yoyData.years;
    if (ys.length < 2) { host.textContent = T('Comparison appears once a second year has data.'); return; }
    const head = `<tr class="yoy-row" style="color:var(--ink2)"><td class="py-1"></td>${ys.map(y => `<td class="text-right px-3 font-medium">${y.year}</td>`).join('')}</tr>`;
    const row = (label, get, color) => `<tr class="yoy-row"><td class="py-1" style="color:var(--ink2)">${label}</td>
      ${ys.map(y => `<td class="text-right px-3" style="color:${color ? color(y) : 'var(--ink)'}">${get(y)}</td>`).join('')}</tr>`;
    const cats = [...new Set(ys.flatMap(y => Object.keys(y.by_category)))]
      .filter(c => ys.some(y => (y.by_category[c] || 0) < 0))
      .sort((a, b) => (ys[ys.length - 1].by_category[a] || 0) - (ys[ys.length - 1].by_category[b] || 0)).slice(0, 10);
    host.innerHTML = `<div style="overflow-x:auto"><table class="w-full type-body-small yoy-table">${head}
        ${row(T('Income'), y => fmt(y.income), () => 'var(--good)')}
        ${row(T('Expenses'), y => fmt(-y.expenses))}
        ${row(T('Savings'), y => fmt(y.savings), y => y.savings >= 0 ? 'var(--good)' : 'var(--bad)')}
        ${row(T('Savings rate'), y => y.savings_rate == null ? '–' : Math.round(y.savings_rate * 100) + ' %')}
        <tr><td colspan="${ys.length + 1}" class="pt-3 pb-1 type-caption font-medium" style="color:var(--ink2)">${T('TOP EXPENSE CATEGORIES')}</td></tr>
        ${cats.map(c => row(catName(c), y => y.by_category[c] ? fmt(-y.by_category[c]) : '–')).join('')}
      </table></div>`;
  };

  const fillRecurring = token => {
    const host = $('#recurring-body'); if (!host) return;
    api(`/api/recurring?year=${year}&scope=${scope}`).then(r => {
      if (token !== drawSeq || !renderIsCurrent(renderId, tab, year)) return;
      if (!r.items.length && !r.candidates.length) { host.textContent = T('No recurring payments detected yet (needs ≥3 months of data per merchant).'); return; }
      state._recCandidates = r.candidates;
      host.innerHTML = `
        <div class="mb-3">${T('Monthly baseline (fixed contracts & subscriptions):')} <b style="color:var(--ink)">${fmt(r.fixed_monthly_base)}</b> / ${T('month')}
          <span class="type-caption">${T('— what we still owe if we stopped everything else.')}</span></div>
        ${r.items.map((i, idx) => `
          <div class="rov-row flex items-center gap-3 py-1 border-t" data-key="${esc(i.key)}" style="border-color:var(--line)">
            <span class="w-64 truncate font-medium" style="color:var(--ink)">${esc(i.merchant)}</span>
            <span class="chip chip-neutral">${i.cadence}</span>
            ${i.override === 'force' ? `<md-icon class="dash-lock" title="${T('Forced on')}" style="color:var(--primary)">push_pin</md-icon>` : ''}
            <span class="flex-1 type-caption flex items-center gap-2">${i.category ? catBadge(i.category) : '—'} · ${i.occurrences}× · ${T('last {date}', { date: fmtDate(i.last_date, true) })}</span>
            <span class="w-24 text-right">${fmt(i.median_amount)}</span>
            <span class="w-28 text-right font-medium" style="color:var(--ink)">${fmt(i.monthly_equivalent)}/${T('mo')}</span>
            <span style="position:relative">
              <md-icon-button id="rov-btn-${idx}" title="${T('Recurring override')}"><md-icon>more_vert</md-icon></md-icon-button>
              <md-menu id="rov-menu-${idx}" anchor="rov-btn-${idx}" positioning="popover">
                <md-menu-item data-st="auto"><div slot="headline">${T('Auto (detected)')}</div></md-menu-item>
                <md-menu-item data-st="never"><div slot="headline">${T('Not recurring')}</div></md-menu-item>
              </md-menu>
            </span>
          </div>`).join('')}
        <div class="mt-3"><md-outlined-button id="rec-add"><md-icon slot="icon">add</md-icon>${T('Mark a payment as recurring')}</md-outlined-button></div>
        <div class="chart-box mt-4"${r.history.length ? '' : ' style="display:none"'}><canvas id="rec-history"></canvas></div>`;
      // wire per-row override menus
      host.querySelectorAll('.rov-row').forEach(rowEl => {
        const btn = rowEl.querySelector('md-icon-button'), menu = rowEl.querySelector('md-menu');
        btn.addEventListener('click', () => { menu.open = !menu.open; });
        menu.querySelectorAll('md-menu-item').forEach(mi => mi.addEventListener('click', () => setRecurringOverride(rowEl.dataset.key, mi.dataset.st)));
      });
      const addBtn = host.querySelector('#rec-add');
      if (addBtn) addBtn.addEventListener('click', () => openRecurringCandidates(state._recCandidates || []));
      if (r.history.length) {
        const t = CHART_COLORS();
        mkChart($('#rec-history'), {
          type: 'line',
          data: { labels: r.history.map(h => MONTHS[+h.ym.slice(5) - 1]), datasets: [
            { label: T('Recurring spend'), data: r.history.map(h => h.total), borderColor: t.c1, backgroundColor: t.c1, tension: .3, fill: false }] },
          options: { plugins: { legend: { display: false } } },
        });
      }
      attachTooltips();
    });
  };

  // Top 5 biggest single purchases for the period, rent (cold + warm) excluded.
  // Always sourced from /api/transactions (unscoped) so the Whose-money filter
  // never changes it, and each buy keeps its own shared/personal marker.
  const RENT_SLUGS = new Set(['living-costs/cold-rent', 'living-costs/nebenkosten']);
  const sharingLabel = sh => {
    if (!sh) return '';
    if (sh === 'shared') return T('Shared');
    if (sh === 'out-of-scope') return T('Out of scope');
    if (sh.startsWith('personal:')) return T('Personal') + ' · ' + personLabelRaw(sh.split(':')[1]);
    return sh;
  };
  /* Liquid net worth over the selected year. Reconstructed from recorded balances + the raw ledger;
     cash & credit-card accounts excluded. Untrusted spans (transactions don't reconcile with
     the anchors) are drawn dashed/red. */
  const nwNote = id => esc(accountLabel(id));
  const drawNetworth = nw => {
    const c = $('#nw-canvas'); if (!c) return;
    const t = CHART_COLORS();
    const labels = nw.points.map(p => p.date);
    const data = nw.points.map(p => p.total_eur);
    const badSpans = [];
    nw.accounts.forEach(a => a.spans.forEach(s => { if (s.ok === false && s.has_txns) badSpans.push([s.from, s.to]); }));
    const untrusted = nw.points.map(p => badSpans.some(([f, to]) => f < p.date && p.date <= to));
    const dash = ctx => (untrusted[ctx.p0DataIndex] || untrusted[ctx.p1DataIndex]) ? [6, 4] : undefined;
    const col = ctx => (untrusted[ctx.p0DataIndex] || untrusted[ctx.p1DataIndex]) ? t.bad : t.c1;
    mkChart(c, {
      type: 'line',
      data: { labels, datasets: [{ data, borderColor: t.c1, backgroundColor: 'transparent', tension: .25, pointRadius: 2, fill: false,
        segment: { borderDash: dash, borderColor: col } }] },
      options: { plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => fmt(ctx.parsed.y) } } },
        scales: { y: { ticks: { callback: v => fmt(v) } } } },
    });
  };
  const fillNetworth = token => {
    const card = $('#networth-card'); if (!card) return;
    api(`/api/networth?year=${year}`).then(nw => {
      if (token !== drawSeq || !renderIsCurrent(renderId, tab, year)) return;
      const totalEl = $('#nw-total'), noteEl = $('#nw-note'), emptyEl = $('#nw-empty'), box = card.querySelector('.chart-box');
      if (!nw.points.length) {
        totalEl.textContent = ''; noteEl.textContent = ''; box.style.display = 'none';
        // Anchors exist but none of them reach this year — a different problem from having none at all.
        emptyEl.innerHTML = nw.accounts.length
          ? T('No recorded balance covers {year}. Net worth starts at your first recorded balance.', { year })
          : T('Record a balance for your bank accounts (Settings › Accounts → “Record balance”) to chart your money over time. Cash and credit-card accounts are not included.');
        return;
      }
      box.style.display = ''; emptyEl.textContent = '';
      // The chart stops at Dec of the shown year, so "Now" is only true for the running year.
      const last = nw.points[nw.points.length - 1];
      totalEl.innerHTML = last.date === nw.as_of
        ? T('Now: {amount}', { amount: `<b>${fmt(nw.current.total_eur)}</b>` })
        : T('End of {year}: {amount}', { year, amount: `<b>${fmt(last.total_eur)}</b>` });
      // Name the scope where the line is read: this is spendable money, not total wealth.
      const notes = [T('Recorded account balances. Cash and credit cards are not included.')];
      if (nw.uncovered.length) notes.push(T('Not yet included (no balance recorded): {list}', { list: nw.uncovered.map(nwNote).join(', ') }));
      if (nw.accounts.some(a => a.spans.some(s => s.ok === false && s.has_txns))) notes.push(T('Dashed red = periods where transactions don’t reconcile with the recorded balances.'));
      noteEl.innerHTML = notes.join(' · ');   // list labels already esc()-d via nwNote
      drawNetworth(nw);
    });
  };
  const fillTopBuys = (view, token) => {
    const host = $('#topbuys-body'); if (!host) return;
    const monthQ = (typeof view === 'number' && view !== 0) ? `&month=${view}` : '';
    api(`/api/transactions?year=${year}${monthQ}`).then(r => {
      if (token !== drawSeq || !renderIsCurrent(renderId, tab, year)) return;
      const buys = r.items
        .filter(t => t.amount_eur < 0 && t.kind !== 'internal-transfer'
          && t.sharing !== 'out-of-scope' && !RENT_SLUGS.has(t.category))
        .sort((a, b) => a.amount_eur - b.amount_eur).slice(0, 5);
      host.innerHTML = buys.length ? buys.map(t => `
        <div class="flex items-center gap-3 py-2 border-t type-body-small" style="border-color:var(--line)">
          <div style="flex:1;min-width:0">
            <div class="truncate font-medium">${esc(t.counterparty || t.purpose || '—')}</div>
            <div class="type-caption" style="color:var(--ink2)">${fmtDate(t.date, true)}${t.sharing ? ' · ' + esc(sharingLabel(t.sharing)) : ''}</div>
          </div>
          ${t.category ? catBadge(t.category) : `<span class="type-caption" style="color:var(--ink2)">${T('Needs review')}</span>`}
          <span class="text-right font-medium" style="color:var(--bad);white-space:nowrap">${fmt(t.amount_eur)}</span>
        </div>`).join('') : `<div class="type-body-small py-2" style="color:var(--ink2)">${T('No purchases.')}</div>`;
      attachTooltips();
    });
  };

  const fillCoverage = token => {
    const host = $('#coverage-card'); if (!host) return;
    api(`/api/coverage?year=${year}`).then(data => {
      if (token !== drawSeq || !renderIsCurrent(renderId, tab, year)) return;
      const current = $('#coverage-card'); if (!current) return;
      current.outerHTML = coverageCard(data);
      attachTooltips();
    });
  };

  /* ---- the per-view renderer ---- */
  const draw = view => {
    const token = ++drawSeq;
    const data = view === 0 ? s : view === 'yc' ? s.year_costs : s.months[view - 1];
    $('#close-toggle').innerHTML = closeBtnHtml(view);   // lives in the sticky bar, updates per view
    const parts = [];
    const sankeyCardHtml = `<div class="card p-5 mb-6"><h2 class="font-medium mb-3">${T('Money flow — income to categories')}</h2>
      <div class="chart-box" id="sankey-box" style="height:420px"><canvas id="sankey-canvas"></canvas></div></div>`;
    const coverageCardHtml = `<div class="card p-5 mb-6" id="coverage-card"><h2 class="font-medium">${T('Statement coverage')}</h2><div class="mt-3 type-body-small" style="color:var(--ink2)">${T('Loading…')}</div></div>`;
    // Above the figures, because it is a statement about the figures below it.
    parts.push(driftBannerHtml(view));
    parts.push(`<div class="flex gap-4 flex-wrap mb-6">${tilesHtml(view, data)}</div>`);
    if (view === 0) parts.push(`<div class="card p-5 mb-6" id="networth-card">
      <div class="flex items-center justify-between mb-1 flex-wrap gap-2"><h2 class="font-medium">${T('Liquid net worth')}</h2><span id="nw-total" class="type-title"></span></div>
      <div id="nw-note" class="type-caption mb-3" style="color:var(--ink2)"></div>
      <div class="chart-box tall"><canvas id="nw-canvas"></canvas></div>
      <div id="nw-empty" class="type-body-small" style="color:var(--ink2)"></div></div>`);
    if (typeof view === 'number' && view !== 0) parts.push('<div id="findings-card"></div>');
    // row 1: money flow (1/3) + where-the-money-goes (2/3)
    parts.push(`<div class="grid-1-2 mb-6">
      ${chartCard(T('Money in · costs · surplus'), 'flow-canvas')}
      ${chartCard(T('Where the money goes'), 'pie-canvas')}</div>`);
    if (view === 'yc') {
      parts.push(`<div class="card p-5 mb-6" id="yc-list"><h2 class="font-medium mb-3">${T('Year-cost transactions')}</h2><div class="type-body-small" style="color:var(--ink2)">${T('Loading…')}</div></div>`);
    } else {
      // income vs expenses by month (year tab only)
      if (view === 0) parts.push(`<div class="mb-6">${chartCard(T('Income vs expenses by month'), 'bymonth-canvas', {
        tall: true,
        header: switchField({ id: 'spread-yc', label: T('Spread year costs'), on: state.spreadYearCosts }),
        note: `<span id="bymonth-note">${spreadNote()}</span>`,
      })}</div>`);
      parts.push(topCostsCardHtml(view));
      parts.push(allSubcatsCardHtml(view));
      // watched costs: bar (by cost) + share of total expenses — above the trend row
      parts.push(`<div class="grid2 gap-6 mb-6">
        ${chartCard(T('Watched costs'), 'watch-bar-canvas')}
        ${chartCard(T('Watched vs total expenses'), 'watch-pie-canvas')}</div>`);
      // row 2: 6-month trend + top-5 biggest buys
      parts.push(`<div class="grid2 gap-6 mb-6">
        ${chartCard(T('6-month trend'), 'trend-canvas')}
        <div class="card p-5" id="topbuys-card">
          <h2 class="font-medium mb-1">${T('Top 5 biggest buys — {period}', { period: view === 0 ? year : T(MONTHS[view - 1]) })}</h2>
          <div class="type-caption mb-3" style="color:var(--ink2)">${T('Excludes rent · shown regardless of the Whose-money filter')}</div>
          <div id="topbuys-body"></div></div></div>`);
      if (view === 0) {
        parts.push(`<div class="card p-5 mb-6" id="recurring-card"><h2 class="font-medium">${T('Recurring payments')}</h2><div id="recurring-body" class="mt-3 type-body-small" style="color:var(--ink2)">${T('Loading…')}</div></div>`);
        // money-flow sits here on the year view (swapped with Statement coverage)
        parts.push(sankeyCardHtml);
        parts.push(`<div class="card p-5 mb-6" id="yoy-card">
          <h2 class="font-medium mb-3">${T('Year over year')}</h2>
          <div id="yoy-body" class="type-body-small" style="color:var(--ink2)">${T('Loading…')}</div></div>`);
      }
    }
    // bottom slot: Statement coverage on the year view (swapped down), money-flow elsewhere
    parts.push(view === 0 ? coverageCardHtml : sankeyCardHtml);
    $('#dash-body').innerHTML = parts.join('');

    // instantiate charts + fill async cards for this view
    drawFlow(data); drawPie(data);
    if (view === 'yc') { fillYcList(token); }
    else {
      if (view === 0) {
        drawByMonth();
        const spread = $('#spread-yc');
        if (spread) spread.addEventListener('change', e => toggleSpread(!!e.target.selected));
      }
      drawTopCosts(data); drawAllSubcats(data);
      drawWatchBar(data); drawWatchPie(data);
      fillTopBuys(view, token); drawTrend(view, token);
      if (typeof view === 'number' && view !== 0) fillFindings(view, token);
      if (view === 0) {
        fillNetworth(token);
        fillRecurring(token);
        fillCoverage(token);
        if (yoyData) renderYoyBody();
        else api(`/api/yoy?scope=${scope}`).then(r => { if (token !== drawSeq || !renderIsCurrent(renderId, tab, year)) return; yoyData = r; renderYoyBody(); });
      }
    }
    drawSankey(data);
    attachTooltips();
  };

  const fillYcList = token => {
    const host = $('#yc-list'); if (!host) return;
    api(`/api/transactions?year=${year}`).then(r => {
      if (token !== drawSeq || !renderIsCurrent(renderId, tab, year)) return;
      const yc = r.items.filter(t => t.year_cost || (t.splits || []).some(sp => sp.year_cost));
      const body = yc.length ? yc.sort((a, b) => a.date.localeCompare(b.date)).map(t => `
        <div class="flex items-center gap-3 py-1 border-t type-body-small" style="border-color:var(--line)">
          <span class="w-16" style="color:var(--ink2)">${fmtDate(t.date, true)}</span>
          <span class="flex-1 truncate">${esc(t.counterparty || t.purpose || '')}</span>
          ${t.category ? catBadge(t.category) : ''}
          <span class="w-28 text-right font-medium" style="color:${t.amount_eur < 0 ? 'var(--bad)' : 'var(--good)'}">${fmt(t.amount_eur)}</span>
        </div>`).join('') : `<div class="type-body-small" style="color:var(--ink2)">${T('No year-cost transactions this year.')}</div>`;
      host.innerHTML = `<h2 class="font-medium mb-3">${T('Year-cost transactions ({n})', { n: yc.length })}</h2>${body}`;
      attachTooltips();
    });
  };

  await customElements.whenDefined('md-tabs');
  const dt = $('#dash-tabs');
  if (typeof state.dashView === 'undefined') state.dashView = 0;
  dt.activeTabIndex = dashViewToIndex(state.dashView);
  dt.addEventListener('change', () => {
    const v = dt.activeTab?.dataset.v;
    const view = v === 'yc' ? 'yc' : +(v ?? 0);
    state.dashView = view; draw(view);
  });
  draw(state.dashView);
}

async function setRecurringOverride(key, st) {
  await api('/api/recurring-override', { key, state: st });
  render();
}

function openRecurringCandidates(candidates) {
  const body = candidates.length ? `<div style="max-height:50vh;overflow-y:auto">${candidates.map(c => `
    <div class="rec-cand flex items-center gap-3 py-2 border-t type-body-small" data-key="${esc(c.key)}" style="border-color:var(--line)">
      <span class="flex-1 truncate font-medium" style="color:var(--ink)">${esc(c.merchant)}</span>
      <span class="type-caption" style="color:var(--ink2)">${T('{n} mo', { n: c.months })} · ${c.occurrences}× · ~${fmt(c.median_amount)}</span>
      <md-filled-tonal-button class="rec-force">${T('Recurring')}</md-filled-tonal-button>
    </div>`).join('')}</div>`
    : `<div class="type-body-small" style="color:var(--ink2)">${T('No other candidate merchants found (needs ≥2 charges).')}</div>`;
  openModal({
    title: T('Mark a payment as recurring'),
    body,
    actions: `<md-text-button class="rc-close">${T('Close')}</md-text-button>`,
    onMount: root => {
      root.querySelector('.rc-close').onclick = () => root._close();
      root.querySelectorAll('.rec-cand').forEach(row => {
        row.querySelector('.rec-force').addEventListener('click', () => { root._close(); setRecurringOverride(row.dataset.key, 'force'); });
      });
    },
  });
}

async function dismissFinding(month, index) {
  await api('/api/finding-dismiss', { year: state.year, month, index });
  render();
}

/* ---------- the integrity check, run where a period is settled ----------
   Closing a period is the moment its figures stop being provisional, so it is the
   moment worth knowing whether they are sound. The check and the missing-statement
   warning answer the same question — is this safe to call settled — so they are one
   dialog rather than two: two in a row means the first is clicked through unread. */
function checkDigestHtml(result) {
  const counts = { error: 0, warning: 0, info: 0 };
  (result.findings || []).forEach(item => { counts[item.severity] += 1; });
  const line = T('{errors} errors, {warnings} warnings, {info} info',
                 { errors: counts.error, warnings: counts.warning, info: counts.info });
  if (!result.findings.length) {
    return `<p style="color:var(--good)"><b>${T('Integrity check: all clear')}</b> — ${esc(line)}</p>`;
  }
  // A flat list, without the action buttons the health panel wires up: a button that
  // does nothing because nothing is listening is worse than no button.
  const rows = result.findings.map(item => {
    const meta = DOCTOR_CHECKS[item.check] || {};
    const colour = item.severity === 'error' ? 'var(--bad)'
      : item.severity === 'warning' ? 'var(--on-warn-container)' : 'var(--ink2)';
    return `<li class="mb-1"><span style="color:${colour}">●</span>
      <b>${esc(meta.label ? T(meta.label) : item.check)}</b>${item.year ? ` · ${esc(item.year)}` : ''}
      <div class="type-caption" style="color:var(--ink2)">${esc(item.message)}</div></li>`;
  }).join('');
  return `<p><b>${T('Integrity check')}</b> — ${esc(line)}</p>
    <ul style="margin-left:1.1rem;list-style:none">${rows}</ul>`;
}

async function runPeriodCheck(year = state.year) {
  return api(`/api/doctor?year=${year}`);
}

function openPeriodCheck(year = state.year) {
  const dialog = openModal({
    title: T('Integrity check — {year}', { year }),
    body: '<div class="flex justify-center p-8"><md-circular-progress indeterminate></md-circular-progress></div>',
    actions: `<md-text-button class="check-close">${T('Close')}</md-text-button>`,
    onMount: async root => {
      root.querySelector('.check-close').onclick = () => root._close();
      let html;
      try {
        html = checkDigestHtml(await runPeriodCheck(year));
      } catch (err) {
        html = `<p style="color:var(--bad)">${esc(err.message || String(err))}</p>`;
      }
      const body = root.querySelector('.generic-modal-body');
      if (body) body.innerHTML = `<div class="type-body-small">${html}</div>`;
    },
  });
  return dialog;
}

/* Close a period, having first said what the check found and what has no statement.
   The check runs every time; the dialog only appears when there is something to say,
   so a clean close is not an extra click that teaches you to click without reading. */
async function closeWithChecks({ title, confirmLabel, gapsHtml, apply }) {
  let result = null;
  try {
    result = await runPeriodCheck();
  } catch (err) {
    showError(err.message || String(err));
    return;
  }
  const findings = result.findings || [];
  if (!gapsHtml && !findings.length) {
    await apply();
    return showMessage(T('Closed. The integrity check found nothing.'));
  }
  return confirmAction({
    title,
    body: `${gapsHtml || ''}${checkDigestHtml(result)}`,
    confirmLabel,
    danger: findings.some(item => item.severity === 'error'),
    onConfirm: apply,
  });
}

async function setMonthState(month, stateStr) {
  await api('/api/close', { year: state.year, month, state: stateStr });
  render();
}

async function setYearState(stateStr) {
  await api('/api/close-year', { year: state.year, state: stateStr });
  render();
}

function acceptClosing(period) {
  const label = period === 'annual' ? T('the year') : period;
  confirmAction({
    title: T('Accept the new figures?'),
    body: T('This records what {period} contains now as the settled figures. The change stays — you are agreeing to it.', { period: label }),
    confirmLabel: T('Accept'),
    onConfirm: async () => {
      try {
        await api('/api/closing-accept', { year: state.year, period });
      } catch (err) {
        showError(err.message || String(err));
        return;
      }
      invalidateYearCache();
      render();
    },
  });
}

async function toggleMonth(month, stateStr) {
  if (stateStr !== 'closed') return setMonthState(month, stateStr);
  const data = await api(`/api/coverage?year=${state.year}`);
  const gaps = coverageGapsFor(data, month);
  return closeWithChecks({
    title: T('Close {month}?', { month: T(MONTH_NAMES[month - 1]) }),
    confirmLabel: T("I'm aware — close month"),
    gapsHtml: gaps.length
      ? `<p>${T('No statement was ingested for these accounts:')}</p>
         <ul class="coverage-gap-list">${gaps.map(gap => `<li>${esc(gap.label)}</li>`).join('')}</ul>`
      : '',
    apply: () => setMonthState(month, stateStr),
  });
}

async function toggleYear(stateStr) {
  if (stateStr !== 'closed') return setYearState(stateStr);
  const data = await api(`/api/coverage?year=${state.year}`);
  const [first, last] = data.active_range || [];
  const gaps = (data.accounts || []).filter(account => !account.low_activity).map(account => {
    const months = [];
    if (Number.isInteger(first) && Number.isInteger(last)) {
      for (let month = first; month <= last; month++) {
        if (!(account.months[month - 1] > 0)) months.push(MONTHS[month - 1]);
      }
    }
    return { label: accountLabel(account.id), months };
  }).filter(account => account.months.length);
  return closeWithChecks({
    title: T('Close year {year}?', { year: state.year }),
    confirmLabel: T("I'm aware — close year"),
    gapsHtml: gaps.length
      ? `<p>${T('No statement was ingested for these accounts:')}</p>
         <ul class="coverage-gap-list">${gaps.map(gap => `<li><b>${esc(gap.label)}</b>: ${gap.months.map(m => T(m)).join(', ')}</li>`).join('')}</ul>`
      : '',
    apply: () => setYearState(stateStr),
  });
}

/* ---------- transactions browser (year-based, month accordions) ---------- */
/* Filter toggle chip: a styled pill that just changes colour when active (no
   checkmark). Optional leading icon in an optional colour (used for people). */
function txnFilterToggle(key, label, { icon = '', iconColor = '' } = {}) {
  const on = state.txnFilters.flags.has(key);
  const ic = icon ? `<md-icon style="color:${iconColor || 'var(--ink2)'}">${icon}</md-icon>` : '';
  return `<button type="button" class="chip filter-chip${on ? ' chip-primary' : ''}" onclick="toggleTxnFilter('${key}')">${ic}${esc(label)}</button>`;
}
function personFilterToggle(p) {
  return txnFilterToggle('personal:' + p, personLabelRaw(p), { icon: personIcon(p), iconColor: personColor(p) });
}
function catFilterChip(slug) {
  return `<md-input-chip label="${esc(catLabel(slug))}" data-slug="${esc(slug)}" style="--cat:${catColorFor(slug)}" onremove="removeCatFilter(this.dataset.slug)"><md-icon slot="icon" style="color:${catColorFor(slug)}">${catIconFor(slug) || 'category'}</md-icon></md-input-chip>`;
}
/* Bulk selection is held as transaction ids, never row indices: the list
   re-renders after every action and re-sorts under filters, so index handles
   would silently point at the wrong rows. */
function txnSelection() {
  if (!state.txnSelection) state.txnSelection = new Set();
  return state.txnSelection;
}
function toggleTxnSelected(id, on) {
  on ? txnSelection().add(id) : txnSelection().delete(id);
  refreshBulkBar();
}
function selectAllFiltered() {
  (window._txns || []).forEach(t => txnSelection().add(t.id));
  document.querySelectorAll('#t-list .txn-select').forEach(box => { box.checked = true; });
  refreshBulkBar();
}
function clearTxnSelection() {
  txnSelection().clear();
  document.querySelectorAll('#t-list .txn-select').forEach(box => { box.checked = false; });
  refreshBulkBar();
}
/* The bulk button stays disabled until something is selected. */
function refreshBulkBar() {
  const host = $('#t-bulkbar');
  if (!host) return;
  const n = txnSelection().size;
  const total = (window._txns || []).length;
  host.innerHTML = `
    <md-outlined-button onclick="selectAllFiltered()" ${total ? '' : 'disabled'} ${tooltip(T('Select every transaction matching the current filters'))}>
      <md-icon slot="icon">checklist</md-icon>${T('Select all ({n})', { n: total })}</md-outlined-button>
    <md-text-button onclick="clearTxnSelection()" ${n ? '' : 'disabled'}>${T('Clear selection')}</md-text-button>
    <md-filled-button onclick="openBulkEdit()" ${n ? '' : 'disabled'}>
      <md-icon slot="icon">edit_note</md-icon>${T('Bulk actions ({n})', { n })}</md-filled-button>`;
  attachTooltips();
}

function txnFilters() {
  if (!state.txnFilters) state.txnFilters = { flags: new Set(), cats: new Set(), accounts: new Set() };
  if (!state.txnFilters.accounts) state.txnFilters.accounts = new Set();
  return state.txnFilters;
}
function toggleTxnFilter(key) { const f = txnFilters().flags; f.has(key) ? f.delete(key) : f.add(key); renderTransactions(); }
function removeCatFilter(slug) { txnFilters().cats.delete(slug); renderTransactions(); }
function removeAccountFilter(key) { txnFilters().accounts.delete(key); renderTransactions(); }
function clearTxnFilters() { state.txnFilters = { flags: new Set(), cats: new Set(), accounts: new Set() }; state.txnSearch = ''; renderTransactions(); }
function openCatFilter() {
  openCatPicker(null, { multi: true, selected: [...txnFilters().cats], onDone: slugs => { txnFilters().cats = new Set(slugs); renderTransactions(); } });
}
function accountFilterChip(key) {
  const g = accountGroups().find(x => x.key === key);
  return `<md-input-chip label="${esc(g ? g.label : key)}" data-key="${esc(key)}" onremove="removeAccountFilter(this.dataset.key)"><md-icon slot="icon">account_balance</md-icon></md-input-chip>`;
}
function openAccountFilter() {
  const sel = new Set(txnFilters().accounts);
  openModal({
    title: T('Filter by account'), width: '420px',
    body: `<md-list class="choice-list">${accountGroups().map(g => `
      <md-list-item type="button" class="acct-opt ${sel.has(g.key) ? 'selected' : ''}" data-key="${esc(g.key)}">
        <md-icon slot="start">account_balance</md-icon><div slot="headline">${esc(g.label)}</div>
        ${g.ids.length > 1 ? `<div slot="supporting-text">${T('{n} accounts', { n: g.ids.length })}</div>` : ''}
        <md-icon slot="end">${sel.has(g.key) ? 'check' : ''}</md-icon>
      </md-list-item>`).join('')}</md-list>`,
    actions: `<md-text-button class="af-clear">${T('Clear')}</md-text-button><div class="ml-auto"></div><md-filled-button class="af-done">${T('Done')}</md-filled-button>`,
    onMount: root => {
      root.querySelectorAll('.acct-opt').forEach(b => b.onclick = () => {
        const k = b.dataset.key; sel.has(k) ? sel.delete(k) : sel.add(k); b.classList.toggle('selected');
        b.querySelector('[slot="end"]').textContent = sel.has(k) ? 'check' : '';
      });
      root.querySelector('.af-clear').onclick = () => { txnFilters().accounts = new Set(); root._close(); renderTransactions(); };
      root.querySelector('.af-done').onclick = () => { txnFilters().accounts = sel; root._close(); renderTransactions(); };
    },
  });
}

async function renderTransactions(renderId = state.renderId) {
  const tab = 'transactions';
  const year = state.year;
  const f = txnFilters();
  const anyFilter = f.flags.size || f.cats.size || f.accounts.size || state.txnSearch;
  const personToggles = state.meta.people.map(personFilterToggle).join('');
  $('#main').innerHTML = `
    <div class="page-sticky">
      <div class="flex items-center gap-2 flex-wrap">
        ${textField({ id: 't-search', placeholder: T('Search merchant or purpose'), className: 't-search-field w-64', value: state.txnSearch || '' })}
        ${txnFilterToggle('review', T('Review'))}
        ${txnFilterToggle('oos', T('Out of scope'))}
        ${txnFilterToggle('tax', T('Tax'))}
        ${txnFilterToggle('year_cost', T('Year cost'))}
        ${personToggles}
        ${txnFilterToggle('split', T('Split'))}
        <md-outlined-button onclick="openCatFilter()"><md-icon slot="icon">category</md-icon>${T('Category…')}</md-outlined-button>
        <md-outlined-button onclick="openAccountFilter()"><md-icon slot="icon">account_balance</md-icon>${T('Account…')}</md-outlined-button>
        <md-outlined-button class="clear-btn" onclick="clearTxnFilters()" ${anyFilter ? '' : 'disabled'}><md-icon slot="icon">filter_alt_off</md-icon>${T('Clear filters')}</md-outlined-button>
        <div class="ml-auto flex items-center gap-2">
          <md-outlined-button onclick="toggleTxnOrder()"><md-icon slot="icon">${state.txnSortAsc ? 'arrow_upward' : 'arrow_downward'}</md-icon>${state.txnSortAsc ? T('Oldest first') : T('Newest first')}</md-outlined-button>
          <span id="expand-toggle-wrap"></span>
        </div>
      </div>
      ${(f.cats.size || f.accounts.size) ? `<md-chip-set class="mt-2">${[...f.accounts].map(accountFilterChip).join('')}${[...f.cats].map(catFilterChip).join('')}</md-chip-set>` : ''}
      <div class="flex items-center gap-3 mt-2 flex-wrap">
        <div id="t-totals" class="type-title txn-totals"></div>
        <div id="t-bulkbar" class="ml-auto flex items-center gap-2"></div>
      </div>
    </div>
    <div id="t-list" class="mt-4"><div class="flex justify-center p-8"><md-circular-progress indeterminate></md-circular-progress></div></div>`;
  const load = async () => {
    const { items } = await cachedYearData('transactions', `/api/transactions?year=${year}`);
    if (!renderIsCurrent(renderId, tab, year)) return;
    const q = (state.txnSearch || '').toLowerCase();
    const persons = [...f.flags].filter(x => x.startsWith('personal:'));
    let rows = items.filter(t => {
      const isOos = t.sharing === 'out-of-scope';
      if (isOos && !f.flags.has('oos')) return false;              // hide out-of-scope unless included
      if (f.flags.has('review') && t.status !== 'needs_review') return false;
      if (f.flags.has('tax') && !t.tax_bucket) return false;
      if (f.flags.has('year_cost') && !t.year_cost) return false;
      if (f.flags.has('split') && !t.splits) return false;
      if (persons.length && !persons.includes(t.sharing)) return false;
      if (f.accounts.size && !f.accounts.has(accountGroupKey(t.account))) return false;
      if (f.cats.size) {
        const cats = t.splits ? t.splits.map(s => s.category) : [t.category];
        if (!cats.some(c => f.cats.has(c))) return false;
      }
      return true;
    });
    if (q) rows = rows.filter(t => ((t.counterparty || '') + ' ' + (t.purpose || '')).toLowerCase().includes(q));
    rows.sort((a, b) => state.txnSortAsc ? a.date.localeCompare(b.date) : b.date.localeCompare(a.date));

    const nonT = items.filter(t => t.sharing !== 'out-of-scope');
    const inc = nonT.filter(t => t.amount_eur > 0).reduce((a, t) => a + t.amount_eur, 0);
    const out = nonT.filter(t => t.amount_eur < 0).reduce((a, t) => a + t.amount_eur, 0);
    $('#t-totals').innerHTML = `${state.year}: ${T('In')} <b style="color:var(--good)">${fmt(inc)}</b> · ${T('Out')} <b>${fmt(-out)}</b> · ${T('Net')} <b style="color:${inc + out >= 0 ? 'var(--good)' : 'var(--bad)'}">${fmt(inc + out)}</b> · ${T('{n} shown', { n: rows.length })}`;

    window._txns = rows;  // flat list; txnRow indices point here
    const byMonth = {};
    rows.forEach((t, i) => { (byMonth[+t.date.slice(5, 7)] ||= []).push(i); });
    window._txnMonthIdx = byMonth;
    state.txnCollapsedByYear ||= {};
    if (!state.txnCollapsedByYear[year]) {
      const firstMonth = Math.max(0, ...Object.keys(byMonth).map(Number));
      state.txnCollapsedByYear[year] = new Set(Array.from({ length: 12 }, (_, i) => i + 1));
      if (firstMonth) state.txnCollapsedByYear[year].delete(firstMonth);
    }
    const collapsed = state.txnCollapsedByYear[year];
    const monthOrder = state.txnSortAsc ? [1,2,3,4,5,6,7,8,9,10,11,12] : [12,11,10,9,8,7,6,5,4,3,2,1];
    let html = '';
    for (const m of monthOrder) {
      const idxs = byMonth[m] || [];
      const net = idxs.filter(i => rows[i].sharing !== 'out-of-scope').reduce((a, i) => a + rows[i].amount_eur, 0);
      const open = !collapsed.has(m);
      html += accordion({
        cls: 'card mb-3 month-acc',
        attrs: `data-month="${m}" data-rendered="${open ? '1' : '0'}"`,
        open,
        headerHtml: `<span class="font-medium">${T(MONTHS[m - 1])} ${state.year}</span>
          <span class="chip chip-neutral">${idxs.length}</span>
          <span class="flex-1"></span>
          ${idxs.length ? `<span class="type-body-small font-medium ${net >= 0 ? 'text-positive' : 'text-negative'}">${fmt(net)}</span>` : `<span class="type-caption" style="color:var(--ink2)">${T('no entries this month')}</span>`}`,
        bodyHtml: open && idxs.length ? idxs.map(i => txnRow(rows[i], i)).join('') : '',
        onToggle: `toggleTxnMonth(this.closest('.acc'), ${m})`,
      });
    }
    $('#t-list').innerHTML = html;
    // Forget ids that this year no longer contains; a selection surviving a year
    // switch would send unknown ids to the bulk endpoint and 404 the whole batch.
    const visible = new Set(items.map(t => t.id));
    [...txnSelection()].forEach(id => { if (!visible.has(id)) txnSelection().delete(id); });
    refreshBulkBar();
    const withEntries = Object.keys(byMonth).map(Number).filter(m => byMonth[m].length);
    const allOpen = withEntries.length > 0 && withEntries.every(m => !collapsed.has(m));
    $('#expand-toggle-wrap').innerHTML = expandToggleBtn(allOpen);
    attachTooltips();
  };
  // Delegated so it keeps working for month bodies rendered lazily on expand.
  $('#t-list').addEventListener('change', event => {
    const box = event.target.closest('.txn-select');
    if (box) toggleTxnSelected(box.dataset.id, box.checked);
  });
  $('#t-search').oninput = e => { state.txnSearch = e.target.value; clearTimeout(state._deb); state._deb = setTimeout(load, 250); };
  await load();
}

function renderTxnMonth(acc, month) {
  if (acc.dataset.rendered === '1') return;
  const idxs = (window._txnMonthIdx || {})[month] || [];
  acc.querySelector('.acc-body').innerHTML = idxs.map(i => txnRow(window._txns[i], i)).join('');
  acc.dataset.rendered = '1';
  attachTooltips();
}

function toggleTxnMonth(acc, month) {
  const collapsed = state.txnCollapsedByYear[state.year];
  if (acc.classList.contains('open')) { collapsed.delete(month); renderTxnMonth(acc, month); }
  else collapsed.add(month);
}

function toggleAllMonths(open) {
  const collapsed = state.txnCollapsedByYear?.[state.year];
  if (!collapsed) return;
  document.querySelectorAll('#t-list .month-acc').forEach(d => {
    if (open) renderTxnMonth(d, +d.dataset.month);
    setAccordion(d, open);
    const m = +d.dataset.month;
    if (open) collapsed.delete(m); else collapsed.add(m);
  });
}

/* One button that flips between expand-all and collapse-all, reflecting state. */
function expandToggleBtn(allOpen) {
  return `<md-outlined-button onclick="toggleExpandAll()"><md-icon slot="icon">${allOpen ? 'unfold_less' : 'unfold_more'}</md-icon>${allOpen ? T('Collapse all') : T('Expand all')}</md-outlined-button>`;
}
function toggleExpandAll() {
  const collapsed = state.txnCollapsedByYear?.[state.year];
  const withEntries = Object.keys(window._txnMonthIdx || {}).map(Number).filter(m => (window._txnMonthIdx[m] || []).length);
  const allOpen = withEntries.length > 0 && withEntries.every(m => !collapsed.has(m));
  toggleAllMonths(!allOpen);
  const wrap = $('#expand-toggle-wrap');
  if (wrap) wrap.innerHTML = expandToggleBtn(!allOpen);
}
/* Reorder the month list + rows between newest-first and oldest-first. */
function toggleTxnOrder() { state.txnSortAsc = !state.txnSortAsc; renderTransactions(); }

/* ---------- ingest (upload -> stage -> process -> tracked uploads) ---------- */
function cap(w) { return w ? w[0].toUpperCase() + w.slice(1) : ''; }
function accountById(id) { return state.meta.accounts.find(x => x.id === id); }
function ownerOf(id) { const a = accountById(id); return a ? a.owner : ''; }
/* Own display name of a single account (ignores grouping). */
/* Account types: stable English slugs stored on the account; the label shown to the user is
   translated. 'cash' is special — manual entries require a cash account. Keep the slugs in
   sync with ACCOUNT_TYPES in app/server.py. */
const ACCOUNT_TYPES = ['giro', 'savings', 'credit-card', 'cash', 'brokerage', 'other'];
function accountTypeLabel(slug) {
  return ({
    'giro': T('Checking account'),
    'savings': T('Savings account'),
    'credit-card': T('Credit card'),
    'cash': T('Cash'),
    'brokerage': T('Brokerage'),
    'other': T('Other'),
  })[slug] || slug;   // unknown/legacy slug shows as-is
}
/* The ONE account-type picker (add + edit). Preserves an unknown legacy slug as an option so
   editing an account never silently drops its type. */
function accountTypeSelect(cls, sel) {
  const slugs = (sel && !ACCOUNT_TYPES.includes(sel)) ? [...ACCOUNT_TYPES, sel] : ACCOUNT_TYPES;
  return selectField({ label: T('Type'), value: sel || 'giro', options: slugs.map(t => [t, accountTypeLabel(t)]), className: `${cls} acct-field` });
}
function accountOwnLabel(a) { return a ? (a.label || (a.bank + (a.type ? ' · ' + accountTypeLabel(a.type) : ''))) : ''; }
/* Display label shown in lists: the GROUP label if grouped, else own label. */
function accountLabel(id) {
  const a = accountById(id);
  if (!a) return id || '';
  return a.group || accountOwnLabel(a);
}
/* Filter/grouping key: the group name if set, else the account id. */
function accountGroupKey(id) { const a = accountById(id); return a ? (a.group || a.id) : (id || ''); }
/* Distinct filterable buckets: one per group, plus each ungrouped account. */
function accountGroups() {
  const m = new Map();
  for (const a of state.meta.accounts) {
    const key = a.group || a.id;
    if (!m.has(key)) m.set(key, { key, label: a.group || accountOwnLabel(a), ids: [] });
    m.get(key).ids.push(a.id);
  }
  return [...m.values()];
}
async function renderIngest() {
  const [stage, up] = await Promise.all([api('/api/ingest/staging'), api('/api/ingest/uploads')]);
  const files = stage.files;
  const uploads = up.uploads.filter(u => u.status === 'processed');
  const extractors = stage.extractors || [];
  state.ingestFiles = files;
  state.ingestExtractors = extractors;
  state.ingestUploads = uploads;
  // A PDF is ready once it has an account + extractor. A bank-name mismatch is only an advisory
  // warning (see stagingRow) — the user chose the account, so let them process into it.
  const canProcess = files.some(f => f.account && (f.kind !== 'pdf' || f.extractor));

  $('#main').innerHTML = `
    <div class="card p-6 mb-4">
      <h2 class="font-medium mb-3">${T('Upload statements')}</h2>
      <div id="drop-zone" class="drop-zone">
        <md-icon>cloud_upload</md-icon>
        <div class="type-body-small">${T('Drag &amp; drop files here, or')}</div>
        ${fileField({ id: 'file-input', label: T('Choose files'), accept: '.csv,.xlsx,.xls,.pdf', multiple: true })}
        <div class="type-caption" style="color:var(--ink2)">${T('CSV / XLSX files are validated before import. For PDFs, choose a deterministic extractor. Nothing is saved unless validation succeeds.')}</div>
      </div>
    </div>

    <div class="card p-6 mb-4">
      <div class="flex items-center justify-between mb-3">
        <h2 class="font-medium">${T('Pre-processing ({n})', { n: files.length })}</h2>
        <md-filled-button id="process-btn" ${canProcess ? '' : 'disabled'}><md-icon slot="icon">play_arrow</md-icon>${T('Process files')}</md-filled-button>
      </div>
      <div id="staging-list">${files.length ? files.map(f => stagingRow(f, extractors)).join('') : `<div class="type-body-small" style="color:var(--ink2)">${T('No files staged — upload above.')}</div>`}</div>
    </div>

    <div id="process-result">${processResultHtml(state.ingestResult)}</div>

    <div class="card p-6">
      <h2 class="font-medium mb-3" id="uploads-count"></h2>
      <div class="flex items-center gap-2 flex-wrap mb-3">
        ${selectField({ id: 'up-account', label: T('Account'), value: ingestFilters().account,
          options: [['', T('All accounts')], ...uploadAccountOptions(uploads)],
          attrs: 'onchange="setIngestFilter(\'account\', this.value)"' })}
        ${selectField({ id: 'up-owner', label: T('Owner'), value: ingestFilters().owner,
          options: [['', T('All owners')], ...state.meta.people.map(p => [p, personLabelRaw(p)]), ['couple', T('Both (couple)')]],
          attrs: 'onchange="setIngestFilter(\'owner\', this.value)"' })}
        ${checkboxField({ id: 'up-all-years', label: T('All years'), checked: ingestFilters().allYears,
          className: 'type-label', attrs: 'onchange="setIngestFilter(\'allYears\', this.checked)"' })}
        <span class="type-caption" style="color:var(--ink2)">${T('Showing {year}. A statement spanning two years appears in both.', { year: state.year })}</span>
      </div>
      <div id="uploads-list"></div>
    </div>`;

  const input = $('#file-input');
  wireFileField($('#drop-zone'), fileInput => { if (fileInput.files.length) ingestUploadFiles(fileInput.files); });
  const dz = $('#drop-zone');
  dz.ondragover = e => { e.preventDefault(); dz.classList.add('drag'); };
  dz.ondragleave = () => dz.classList.remove('drag');
  dz.ondrop = e => { e.preventDefault(); dz.classList.remove('drag'); if (e.dataTransfer.files.length) ingestUploadFiles(e.dataTransfer.files); };
  const pb = $('#process-btn'); if (pb) pb.onclick = processFiles;
  refreshUploadsList();
  attachTooltips();
}

/* Only accounts that actually have processed files — a filter listing accounts with
   nothing behind them is just more noise on a page we are trying to de-clutter. */
function uploadAccountOptions(uploads) {
  const ids = [...new Set(uploads.map(u => u.account).filter(Boolean))];
  return ids.map(id => [id, accountLabel(id)]).sort((a, b) => a[1].localeCompare(b[1]));
}

function pickerTrigger(icon, label, onclick, filled = false) {
  return `<md-outlined-button type="button" class="${filled ? 'filled' : ''}" onclick="${onclick}">
    <md-icon slot="icon">${esc(icon)}</md-icon>${esc(label)}
  </md-outlined-button>`;
}

function stageAccountField(f) {
  const account = accountById(f.account);
  return pickerTrigger('account_balance', account ? accountOwnLabel(account) : T('Choose account'),
    `openStageAccountPicker('${f.id}')`, !!account);
}

function stageExtractorField(f, extractors) {
  if (f.kind !== 'pdf') return '';
  const extractor = extractors.find(x => x.id === f.extractor);
  return `<div class="flex items-center gap-2 flex-wrap">
    ${pickerTrigger('document_scanner', extractor ? extractor.label : T('Choose extractor'), `openStageExtractorPicker('${f.id}')`, !!extractor)}
    <label class="flex items-center type-caption" style="color:var(--ink2)" ${tooltip(T('Only use this if strict extraction reports a questionable text field whose amount is still proven by the running balance.'))}>
      <md-checkbox touch-target="wrapper" ${f.allow_review ? 'checked' : ''} onchange="stageSetAllowReview('${f.id}', this.checked)"></md-checkbox>
      ${T('Send safe exceptions to Review')}
    </label>
  </div>`;
}

function openStageAccountPicker(id) {
  const file = (state.ingestFiles || []).find(f => f.id === id);
  if (!file) return;
  openChoicePicker({
    title: T('Select bank account'),
    current: file.account || '',
    options: state.meta.accounts.map(a => ({
      value: a.id, label: accountOwnLabel(a), icon: 'account_balance',
      note: `${personLabelRaw(a.owner)} · ${a.id}${a.group ? ` · ${a.group}` : ''}`,
    })),
    onPick: account => stageSetAccount(id, account),
  });
}

function openStageExtractorPicker(id) {
  const file = (state.ingestFiles || []).find(f => f.id === id);
  if (!file) return;
  openChoicePicker({
    title: T('Select PDF extractor'),
    current: file.extractor || '',
    options: (state.ingestExtractors || []).map(x => ({
      value: x.id, label: x.label, icon: 'document_scanner',
      note: x.description || T('Deterministic PDF statement extractor'),
    })),
    onPick: extractor => stageSetExtractor(id, extractor),
  });
}

function pdfAccountCompatible(f, extractors) {
  if (f.kind !== 'pdf' || !f.extractor || !f.account) return false;
  const extractor = extractors.find(x => x.id === f.extractor);
  const account = accountById(f.account);
  return !!extractor && !!account && (!extractor.account_bank_contains ||
    (account.bank || '').toLowerCase().includes(extractor.account_bank_contains));
}

function stagingRow(f, extractors) {
  // A valid statement can be empty (a month with no activity), so it has no date
  // range or currency to show — say so rather than rendering blank separators.
  const preview = f.preview
    ? (f.preview.transactions
      ? `${f.preview.format} · ${T('{n} transactions', { n: f.preview.transactions })} · ${fmtDate(f.preview.date_min, true)}–${fmtDate(f.preview.date_max, true)} · ${f.preview.currencies.join(', ')}`
      : `${f.preview.format} · ${T('empty statement — no activity this period')}`)
    : f.kind === 'pdf' ? T('PDF · extractor required') : T('Awaiting validation');
  const extraction = f.extraction
    ? `<div class="type-caption" style="color:var(--ink2)">${T('{n} rows found', { n: f.extraction.transactions_extracted || 0 })} · ${T('discrepancy {amount}', { amount: fmt(f.extraction.discrepancy || 0) })}</div>` : '';
  let incompatible = '';
  if (f.kind === 'pdf' && f.account && f.extractor && !pdfAccountCompatible(f, extractors)) {
    const ex = extractors.find(x => x.id === f.extractor);
    const acct = accountById(f.account);
    incompatible = `<div class="type-caption flex items-center gap-1" style="color:var(--ink2)"><md-icon style="font-size:16px;color:var(--on-warn-container)">warning</md-icon>${T('The {extractor} extractor usually goes with an account whose bank contains “{bank}”, but this account\'s bank is “{account_bank}”. It will still import into this account — make sure that\'s the one you want.', { extractor: esc((ex && ex.label) || T('selected')), bank: esc((ex && ex.account_bank_contains) || ''), account_bank: esc((acct && acct.bank) || '—') })}</div>`;
  }
  return `<div class="stage-row py-3 border-t" data-id="${f.id}" style="border-color:var(--line)">
    <div class="flex items-start gap-3">
      <md-icon style="color:var(--primary)">${f.kind === 'pdf' ? 'picture_as_pdf' : 'table_view'}</md-icon>
      <div class="min-w-0 flex-1">
        <div class="truncate type-body-small font-medium">${esc(f.original_name)}</div>
        <div class="type-caption" style="color:var(--ink2)">${(f.size / 1024).toFixed(0)} KB · ${esc(preview)}</div>
        ${extraction}
        ${incompatible}
        ${f.error ? `<div class="type-caption text-negative"><md-icon style="font-size:1em">error</md-icon> ${esc(f.error)}</div>` : ''}
      </div>
      <md-icon-button onclick="stageDelete('${f.id}')" ${tooltip(T('Remove staged file'))}><md-icon>delete</md-icon></md-icon-button>
    </div>
    <div class="flex items-center gap-3 flex-wrap mt-3">
      ${stageAccountField(f)}
      ${stageExtractorField(f, extractors)}
      ${textField({ label: T('Comment'), className: 'stage-comment flex-1', placeholder: T('Optional'), value: f.comment || '', attrs: `onchange="stageSetComment('${f.id}', this.value)"` })}
    </div>
  </div>`;
}

/* Which calendar years an upload's contents fall in. `years` is authoritative and
   already lists both when a statement straddles a year boundary. A statement for a
   period with no activity produced no transactions and so has no years — fall back
   to the period it declared, because a file the filter cannot place would become
   invisible, and an invisible upload cannot be deleted. */
function uploadYears(u) {
  if (u.years && u.years.length) return u.years.map(Number);
  const month = String(u.period || '').match(/^(\d{4})-\d{2}$/);
  if (month) return [Number(month[1])];
  const spanned = String((u.extraction && u.extraction.period) || '').match(/\d{4}/g);
  if (spanned) return [...new Set(spanned.map(Number))];
  return [];
}

function ingestFilters() {
  if (!state.ingestFilters) state.ingestFilters = { account: '', owner: '', allYears: false };
  return state.ingestFilters;
}
function setIngestFilter(key, value) {
  ingestFilters()[key] = value;
  refreshUploadsList();
}
function filteredUploads() {
  const f = ingestFilters();
  return (state.ingestUploads || []).filter(u => {
    if (f.account && u.account !== f.account) return false;
    if (f.owner && u.owner !== f.owner) return false;
    if (!f.allYears) {
      const years = uploadYears(u);
      // An upload we cannot date is never hidden.
      if (years.length && !years.includes(state.year)) return false;
    }
    return true;
  });
}
function refreshUploadsList() {
  const host = $('#uploads-list');
  if (!host) return;
  const all = state.ingestUploads || [];
  const shown = filteredUploads();
  host.innerHTML = shown.length
    ? shown.map(uploadRow).join('')
    : `<div class="type-body-small" style="color:var(--ink2)">${all.length
      ? T('No processed files match these filters.')
      : T('Nothing processed yet.')}</div>`;
  const count = $('#uploads-count');
  if (count) {
    count.textContent = shown.length === all.length
      ? T('Processed files ({n})', { n: all.length })
      : T('Processed files ({shown} of {total})', { shown: shown.length, total: all.length });
  }
  attachTooltips();
}

function uploadRow(u) {
  const badge = `<span class="chip chip-good">${T('processed')}</span>`;
  const years = u.years || [];
  // An empty statement has no entries, date range or year to report — one clear
  // line beats "0 entries · ? → ?".
  const stats = (u.kind === 'table' && !u.total)
    ? T('empty statement — no activity this period')
    : `${T('{n} entries', { n: u.added ?? u.total ?? '?' })}${u.duplicates ? ` ${T('(+{n} duplicates skipped)', { n: u.duplicates })}` : ''} · ${u.date_min ? fmtDate(u.date_min, true) : '?'} → ${u.date_max ? fmtDate(u.date_max, true) : '?'}${years.length ? ` · ${years.length > 1 ? T('years') : T('year')} ${years.join(', ')}` : ''}`;
  const extracted = u.extraction
    ? `<div class="type-caption" style="color:var(--ink2)">${esc(u.extraction.period)} · ${T('{n} extracted', { n: u.extraction.transactions_extracted })} · ${T('reconciled exactly')}${u.extraction.transactions_for_review ? ` · ${T('{n} sent to Review', { n: u.extraction.transactions_for_review })}` : ''}</div>` : '';
  return `<div class="flex items-start gap-3 py-2 border-t" style="border-color:var(--line)">
    <md-icon style="color:var(--primary)">${u.kind === 'pdf' ? 'picture_as_pdf' : 'description'}</md-icon>
    <div class="flex-1 min-w-0">
      <div class="flex items-center gap-2"><span class="font-medium truncate">${esc(u.original_name)}</span>${badge}</div>
      <div class="type-caption" style="color:var(--ink2)">${esc(u.account)} · ${personLabel(u.owner || '')} · ${T('processed {when}', { when: (u.processed_at || '').replace('T', ' ').slice(0, 16) })}</div>
      <div class="type-caption" style="color:var(--ink2)">${stats}</div>
      ${extracted}
      ${u.comment ? `<div class="type-caption" style="color:var(--ink2)">📝 ${esc(u.comment)}</div>` : ''}
    </div>
    <md-text-button onclick="deleteUpload('${u.id}', ${JSON.stringify(u.original_name).replace(/"/g, '&quot;')})" style="--md-text-button-label-text-color:var(--bad)">${T('Delete')}</md-text-button>
  </div>`;
}

/* ---------- accounts management (labels, grouping, owner) ---------- */
function ownerSelect(cls, sel) {
  const opts = [...state.meta.people.map(p => [p, personLabelRaw(p)]), ['couple', T('Both (couple)')]];
  return selectField({ label: T('Owner'), value: sel, options: opts, className: `${cls} acct-field` });
}
function accountChoices() {
  return state.meta.accounts.map(a => [a.id, accountOwnLabel(a), `${personLabelRaw(a.owner)}${a.group ? ' · ' + a.group : ''}`]);
}

/* Accounts management lives inside Settings › Accounts. Returns the panel markup
   (fetches per-account usage counts); the add/save/delete handlers stay global. */
async function accountsAreaHtml() {
  const usage = (await api('/api/account-usage')).counts;
  const accts = state.meta.accounts;
  return `
    <div class="card p-5 mb-4">
      <h2 class="font-medium mb-3">${T('Add account')}</h2>
      <div class="flex gap-2 items-end flex-wrap" id="acct-add">
        ${textField({ label: T('ID (permanent)'), className: 'a-id w-44', placeholder: T('e.g. giro-anna') })}
        ${textField({ label: T('Bank'), className: 'a-bank w-44', placeholder: 'N26' })}
        ${accountTypeSelect('a-type', 'giro')}
        ${ownerSelect('a-owner', state.meta.people[0])}
        <md-filled-button onclick="addAccount()">${T('Add')}</md-filled-button>
      </div>
      <div class="type-caption mt-2" style="color:var(--ink2)">${T('The {id} is the permanent internal key every transaction links to — it cannot be changed later. Use {label} for the display name and {group} to show several accounts under one name.', { id: `<b>${T('id')}</b>`, label: `<b>${T('label')}</b>`, group: `<b>${T('group')}</b>` })}</div>
    </div>
    <div class="flex flex-col gap-3">
      ${accts.map(a => accountRow(a, usage[a.id] || 0)).join('')}
    </div>`;
}

/* One card per account; fields wrap to fit the panel width (no horizontal scroll). */
function accountRow(a, n) {
  return `<div class="card acct-card p-4" data-id="${esc(a.id)}">
    <div class="flex items-center gap-2 flex-wrap mb-3">
      <div class="flex-1 min-w-0">
        <span class="font-medium">${esc(a.id)}</span>
        <span class="type-caption ml-2" style="color:var(--ink2)">${T('{n} txns', { n })}</span>
      </div>
      <md-text-button onclick="saveAccount('${esc(a.id)}')"><md-icon slot="icon">save</md-icon>${T('Save')}</md-text-button>
      ${n ? `<md-icon-button disabled ${tooltip(T('In use — cannot delete'))}><md-icon>delete</md-icon></md-icon-button>`
          : `<md-icon-button onclick="deleteAccount('${esc(a.id)}')" ${tooltip(T('Delete account'))}><md-icon>delete</md-icon></md-icon-button>`}
    </div>
    <div class="flex flex-wrap gap-3 items-start">
      ${textField({ label: T('Label'), className: 'ar-label acct-field', value: a.label || accountOwnLabel(a) })}
      ${textField({ label: T('Group'), className: 'ar-group acct-field', value: a.group || '', placeholder: '—' })}
      ${ownerSelect('ar-owner', a.owner)}
      ${textField({ label: T('Bank'), className: 'ar-bank acct-field', value: a.bank || '' })}
      ${accountTypeSelect('ar-type', a.type)}
      ${selectField({ label: T('Currency'), className: 'ar-currency acct-field',
        value: (a.currency || 'EUR'), options: currencyOptions() })}
    </div>
    <div class="type-caption mt-2" style="color:var(--ink2)">${T('The currency this account is held in. Transactions keep their own currency and are converted at the rate of their date — this is what a balance you record here is read as.')}</div>
    <div class="account-checks mt-3">
      ${lowActivitySwitch(a.low_activity ?? (a.type === 'cash'))}
      <md-text-button onclick="openRecordBalance('${esc(a.id)}')"><md-icon slot="icon">account_balance_wallet</md-icon>${T('Record balance…')}</md-text-button>
      <md-icon class="help-icon" tabindex="0" ${tooltip(T('Save the real balance your bank shows for this account on a date. With two or more dates, the app checks that the transactions between them add up to the cent — flagging any missing or duplicated entries on the Dashboard. CSV exports carry no running total, so this is the manual reconciliation check.'))}>help</md-icon>
    </div>
  </div>`;
}

async function acctRefresh() { state.meta = await api('/api/meta'); state.settingsArea = 'accounts'; renderSettings(); }

async function addAccount() {
  const f = $('#acct-add');
  const id = f.querySelector('.a-id').value.trim();
  if (!id) { showError(T('Account ID is required.')); return; }
  const body = {
    id, bank: f.querySelector('.a-bank').value.trim(), type: f.querySelector('.a-type').value.trim(),
    owner: f.querySelector('.a-owner').value,
  };
  await api('/api/account-add', body);
  acctRefresh();
}

async function saveAccount(id) {
  const r = document.querySelector(`.acct-card[data-id="${id}"]`);
  await api('/api/account-update', {
    id,
    label: r.querySelector('.ar-label').value.trim(),
    group: r.querySelector('.ar-group').value.trim(),
    owner: r.querySelector('.ar-owner').value,
    bank: r.querySelector('.ar-bank').value.trim(),
    type: r.querySelector('.ar-type').value.trim(),
    currency: r.querySelector('.ar-currency').value,
    low_activity: readLowActivity(r),
  });
  acctRefresh();
}

function openRecordBalance(accountId) {
  openModal({
    title: T('Record balance — {account}', { account: accountLabel(accountId) }),
    body: `<div class="grid2 gap-3">
      ${textField({ label: T('Balance date'), type: 'date', className: 'anchor-date', value: localDateISO() })}
      ${textField({ label: T('Account balance'), type: 'number', className: 'anchor-balance', placeholder: '0.00', attrs: 'step="0.01"' })}
    </div>
    <div class="type-caption mt-3" style="color:var(--ink2)">${T("Enter the balance shown by the bank in this account's currency.")}</div>`,
    actions: `<md-text-button class="anchor-cancel">${T('Cancel')}</md-text-button><md-filled-button class="anchor-save">${T('Record balance')}</md-filled-button>`,
    onMount: root => {
      root.querySelector('.anchor-cancel').onclick = () => root._close();
      root.querySelector('.anchor-save').onclick = async () => {
        const dateField = root.querySelector('.anchor-date');
        const balanceField = root.querySelector('.anchor-balance');
        const balance = Number(balanceField.value);
        dateField.error = !dateField.value;
        balanceField.error = balanceField.value.trim() === '' || !Number.isFinite(balance);
        if (dateField.error || balanceField.error) return;
        await api('/api/anchor', { account: accountId, date: dateField.value, balance: cents(balance) / 100 });
        root._close();
        showMessage(T('Balance anchor recorded. Reconciliation status is shown on the Dashboard.'));
      };
    },
  });
}

function deleteAccount(id) {
  openModal({
    title: T('Delete account?'),
    body: `<div class="type-body-small" style="color:var(--ink2)">${T('Delete {id}? Only possible because it has no transactions.', { id: `<b>${esc(id)}</b>` })}</div>`,
    actions: `<md-text-button class="ad-cancel">${T('Cancel')}</md-text-button><md-filled-button class="ad-go">${T('Delete')}</md-filled-button>`,
    onMount: root => {
      root.querySelector('.ad-cancel').onclick = () => root._close();
      root.querySelector('.ad-go').onclick = async () => { root._close(); await api('/api/account-delete', { id }); acctRefresh(); };
    },
  });
}

/* ---- Settings › Balances -------------------------------------------------
   The year grid of recorded balances. Two kinds of number live here and the UI
   must never blur them: a RECORDED balance came from the bank and can prove the
   ledger right or wrong; a DERIVED one is only what the ledger computed from the
   last recorded balance. Derived values are shown to compare against your banking
   app — there is deliberately no button that adopts one as recorded, because that
   would turn every cell green while proving nothing.
   The grid reads; the per-account dialog writes, because a statement covers one
   account for one year and that is how the numbers arrive. */

const BAL_STATUS = {
  ok: { cls: 'bal-ok', icon: 'check' },
  mismatch: { cls: 'bal-bad', icon: 'priority_high' },
  recorded: { cls: 'bal-rec', icon: '' },
  derived: { cls: 'bal-derived', icon: '' },
  unknown: { cls: 'bal-unknown', icon: '' },
  future: { cls: 'bal-future', icon: '' },
};

function balancesYears() {
  const years = (state.meta.years || []).slice();
  const now = new Date().getFullYear();
  if (!years.includes(now)) years.push(now);
  return years.sort((a, b) => a - b);
}

async function fillBalancesArea() {
  const host = $('#settings-area'); if (!host) return;
  const years = balancesYears();
  if (!years.includes(state.balancesYear)) state.balancesYear = years.includes(state.year) ? state.year : years[years.length - 1];
  const data = await api(`/api/balances?year=${state.balancesYear}`);
  if (state.settingsArea !== 'balances') return;      // navigated away while loading
  state.balancesData = data;
  const current = $('#settings-area'); if (!current) return;
  current.innerHTML = balancesAreaHtml(data, years);
  attachTooltips();
}

function setBalancesYear(year) { state.balancesYear = Number(year); fillBalancesArea(); }

/* EUR uses the app-wide format; only genuinely foreign accounts get a currency code. */
const balMoney = (v, currency) => (currency === 'EUR' ? fmt(v) : fmtCur(v, currency));

function balanceCellHtml(row, cell) {
  const meta = BAL_STATUS[cell.status] || BAL_STATUS.unknown;
  const shown = cell.balance != null ? cell.balance : cell.derived;
  const label = accountLabel(row.id);
  const fmtC = v => balMoney(v, row.currency);
  const when = fmtDate(cell.date, true);
  let tip;
  if (cell.status === 'ok') {
    tip = T('{label} · {date}: {amount} recorded. Transactions since {from} add up to the cent.',
      { label, date: when, amount: fmtC(cell.balance), from: fmtDate(cell.span_from, true) });
  } else if (cell.status === 'mismatch') {
    tip = T('{label} · {date}: {amount} recorded, but the transactions since {from} are off by {diff}.',
      { label, date: when, amount: fmtC(cell.balance), from: fmtDate(cell.span_from, true),
        diff: fmtC(cell.diff_cents / 100) });
  } else if (cell.status === 'recorded') {
    tip = T('{label} · {date}: {amount} recorded. No earlier balance to check it against yet.',
      { label, date: when, amount: fmtC(cell.balance) });
  } else if (cell.status === 'derived') {
    tip = T('{label} · {date}: not recorded. The ledger computes {amount}. Check it against your bank and enter the real figure.',
      { label, date: when, amount: fmtC(cell.derived) });
  } else if (cell.status === 'future') {
    tip = T('{label} · {date}: still to come.', { label, date: when });
  } else {
    tip = T('{label} · {date}: nothing recorded and nothing to compute from. Enter a balance to start this account.', { label, date: when });
  }
  // An anchor dated off the month end (a statement cut on the 29th) is still that month's
  // balance — show it, and mark the real date so it is never mistaken for a month-end figure.
  const offDate = cell.anchor_date && cell.anchor_date !== cell.date;
  const text = shown == null ? (cell.status === 'future' ? '' : '—') : fmtC(shown);
  return `<button type="button" class="bal-cell ${meta.cls}" data-bal-account="${esc(row.id)}" data-bal-key="${esc(cell.key)}"
    ${tooltip(esc(tip))} aria-label="${esc(tip)}">
    <span class="bal-value">${esc(text)}</span>
    ${offDate ? `<span class="bal-flag type-caption">${esc(fmtDate(cell.anchor_date))}</span>` : ''}
    ${meta.icon ? `<md-icon class="bal-icon">${meta.icon}</md-icon>` : ''}
  </button>`;
}

/* A month's total across the EUR accounts that count toward net worth. When some account has
   no figure the sum is still worth seeing, but it is marked partial and says how many accounts
   it could read — an unmarked partial total is the kind of number people plan around. */
function balTotalHtml(total) {
  if (total.total == null) {
    return `<div class="bal-total bal-total-col type-body-small" ${tooltip(T('No balance is known for any account this month.'))}><span style="color:var(--ink2)">—</span></div>`;
  }
  if (total.complete) return `<div class="bal-total bal-total-col type-body-small">${esc(fmt(total.total))}</div>`;
  return `<div class="bal-total bal-total-col type-body-small partial"
    ${tooltip(T('Partial: {covered} of {accounts} accounts have a figure for this month. The rest are missing, not zero.', { covered: total.covered, accounts: total.accounts }))}>
    ${esc(fmt(total.total))}<span class="bal-partial type-caption">${total.covered}/${total.accounts}</span></div>`;
}

/* Months run down the page and accounts across it: a column is one account's year, which is
   the shape a bank statement arrives in, and a row is one month across the household — the
   net-worth line itemized. */
function balancesAreaHtml(data, years) {
  const yearTabs = years.map(y => {
    const kind = y === data.year ? 'filled' : 'outlined';
    return `<md-${kind}-button onclick="setBalancesYear(${y})">${y}</md-${kind}-button>`;
  }).join('');
  // Total sits next to the month, not after the accounts: it is the headline and the columns
  // to its right are the breakdown. Pushed to the end it scrolls out of sight on a narrow panel.
  const head = `<div class="bal-head type-label">${T('Month')}</div>
    <div class="bal-head bal-total-col type-label" ${tooltip(T('Only EUR accounts that count toward net worth, and only when every one of them has a figure for that month. A total that quietly drops what it could not read is not a total.'))}>${T('Total')}</div>
    ${data.accounts.map(row => {
      const marks = [row.currency !== 'EUR' ? esc(row.currency) : '', row.in_networth ? '' : T('not in net worth')].filter(Boolean).join(' · ');
      return `<div class="bal-head">
        <button type="button" class="bal-account-btn type-label" data-bal-account="${esc(row.id)}" data-bal-key="opening"
          ${tooltip(T('Enter a year of balances for {account}', { account: esc(accountLabel(row.id)) }))}>${esc(accountLabel(row.id))}</button>
        ${marks ? `<span class="bal-mark type-caption">${marks}</span>` : ''}
      </div>`;
    }).join('')}`;
  const rows = data.periods.map((period, index) => {
    const name = index === 0 ? T('Opening') : T(MONTHS[index - 1]);
    const total = data.totals[index];
    return `<div class="bal-period type-body-small ${index === 0 ? 'opening' : ''}"
        ${index === 0 ? tooltip(T('The closing balance of the year before. A year opens where the last one closed — one number, not two.')) : tooltip(fmtDate(period.date, true))}>${name}</div>
      ${balTotalHtml(total)}
      ${data.accounts.map(row => balanceCellHtml(row, row.cells[index])).join('')}`;
  }).join('');
  return settingsSection(
    T('Balances per month'),
    T('What each account was really worth, month by month. Recorded balances come from your bank and prove the ledger right or wrong; grey figures are only what the ledger computes. Click any cell to enter a year of balances for that account.'),
    `<div class="flex gap-2 flex-wrap items-center mb-1">${yearTabs}</div>
     <div class="scroll-x"><div class="bal-grid" style="--bal-cols:${data.accounts.length}">${head}${rows}</div></div>
     <div class="bal-legend type-caption" style="color:var(--ink2)">
       <span class="bal-chip bal-ok"></span>${T('recorded, reconciles')}
       <span class="bal-chip bal-bad"></span>${T('recorded, does not add up')}
       <span class="bal-chip bal-rec"></span>${T('recorded, nothing to check against')}
       <span class="bal-chip bal-derived"></span>${T('computed by the ledger')}
       <span class="bal-chip bal-unknown"></span>${T('unknown')}
     </div>`,
    'wide');
}

/* Enter a whole year for one account: the shape a statement actually arrives in. */
function openBalanceYear(accountId, focusKey) {
  const data = state.balancesData;
  const row = (data.accounts || []).find(a => a.id === accountId);
  if (!row) return;
  const fields = row.cells.map((cell, index) => {
    const period = data.periods[index];
    const name = index === 0 ? T('Opening ({date})', { date: fmtDate(period.date, true) })
      : `${T(MONTH_NAMES[index - 1])} ${fmtDate(period.date, true)}`;
    const hint = cell.status === 'mismatch'
      ? T('ledger: {amount} — off by {diff}', { amount: balMoney(cell.derived, row.currency), diff: balMoney(cell.diff_cents / 100, row.currency) })
      : cell.derived != null && cell.balance == null ? T('ledger: {amount}', { amount: balMoney(cell.derived, row.currency) })
        : cell.status === 'ok' ? T('reconciles') : '';
    const hintClass = cell.status === 'mismatch' ? 'bad' : cell.status === 'ok' ? 'good' : 'muted';
    return `<div class="bal-row ${cell.key === focusKey ? 'focus' : ''}">
      <label class="type-body-small" for="bal-f-${index}">${esc(name)}</label>
      ${textField({ id: `bal-f-${index}`, label: '', type: 'number', className: 'bal-in',
        value: cell.balance == null ? '' : cell.balance,
        attrs: `step="0.01" data-index="${index}" data-date="${esc(cell.anchor_date || cell.date)}" data-original="${cell.balance == null ? '' : cell.balance}"` })}
      <span class="bal-hint type-caption ${hintClass}">${esc(hint)}</span>
    </div>`;
  }).join('');
  openModal({
    title: T('Balances — {account} · {year}', { account: accountLabel(accountId), year: data.year }),
    width: '620px',   // three columns: month, the figure, and what the ledger says about it
    body: `<div class="type-caption mb-3" style="color:var(--ink2)">${T('Enter the balance your bank shows at each month end, in {currency}. Leave a month empty if you do not have its statement — an empty month is honest, a guess is not. Clearing a figure deletes the recorded balance.', { currency: esc(row.currency) })}</div>
      <div class="bal-form">${fields}</div>`,
    actions: `<md-text-button class="bal-cancel">${T('Cancel')}</md-text-button><md-filled-button class="bal-save">${T('Save balances')}</md-filled-button>`,
    onMount: root => {
      root.querySelector('.bal-cancel').onclick = () => root._close();
      const inputs = [...root.querySelectorAll('.bal-in')];
      // Enter moves down the column: a year of balances is typed, not clicked.
      inputs.forEach((el, i) => el.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); (inputs[i + 1] || root.querySelector('.bal-save')).focus(); }
      }));
      const focused = root.querySelector('.bal-row.focus .bal-in') || inputs[0];
      if (focused) setTimeout(() => focused.focus(), 50);
      root.querySelector('.bal-save').onclick = async () => {
        const changes = [];
        for (const el of inputs) {
          const original = el.dataset.original;
          const typed = el.value.trim();
          if (typed === original) continue;
          if (typed === '') changes.push({ kind: 'delete', date: el.dataset.date });
          else if (Number.isFinite(Number(typed))) changes.push({ kind: 'set', date: el.dataset.date, balance: cents(Number(typed)) / 100, replace: original !== '' });
        }
        if (!changes.length) { root._close(); return; }
        root._close();
        let saved = 0, removed = 0;
        for (const change of changes) {
          if (change.kind === 'delete') { await api('/api/anchor-delete', { account: accountId, date: change.date }); removed++; }
          else { await api('/api/anchor', { account: accountId, date: change.date, balance: change.balance, replace: change.replace }); saved++; }
        }
        showMessage(T('{saved} balances recorded, {removed} removed.', { saved, removed }));
        fillBalancesArea();
      };
    },
  });
}

document.addEventListener('click', e => {
  const hit = e.target.closest('[data-bal-account]');
  if (hit) openBalanceYear(hit.dataset.balAccount, hit.dataset.balKey);
});

async function ingestUploadFiles(fileList) {
  const results = [];
  for (const f of fileList) {
    const fd = new FormData();
    fd.append('file', f);
    const res = await fetch('/api/ingest/upload', { method: 'POST', body: fd });
    if (!res.ok) {
      let d; try { d = JSON.parse(await res.text()).detail; } catch { d = T('Upload failed'); }
      results.push({ file: f.name, status: 'error', detail: d });
    } else {
      const staged = await res.json();
      const detail = staged.preview
        ? (staged.preview.transactions
          ? T('Validated {n} transactions as {format}. Choose the account, then process.', { n: staged.preview.transactions, format: staged.preview.format })
          : T('Validated as {format}: an empty statement with no activity. Choose the account, then process.', { format: staged.preview.format }))
        : T('PDF staged. Choose the account and extractor, then process.');
      results.push({ file: f.name, status: 'processed', detail });
    }
  }
  state.ingestResult = results;
  renderIngest();
}

async function stageSetAccount(id, account) { await api('/api/ingest/staging-update', { id, account }); renderIngest(); }
async function stageSetComment(id, comment) { await api('/api/ingest/staging-update', { id, comment }); }
async function stageSetExtractor(id, extractor) { await api('/api/ingest/staging-update', { id, extractor }); renderIngest(); }
async function stageSetAllowReview(id, allow_review) { await api('/api/ingest/staging-update', { id, allow_review }); }
async function stageDelete(id) { await api('/api/ingest/staging-delete', { id }); renderIngest(); }

function processResultHtml(result) {
  if (!result || !result.length) return '';
  const rows = result.map(x => {
    const c = x.status === 'processed' ? 'chip-good' : 'chip-bad';
    return `<div class="flex items-start gap-2 py-1 type-body-small border-t" style="border-color:var(--line)">
      <span class="chip ${c}">${esc(x.status)}</span>
      <div><b>${esc(x.file)}</b><div class="type-caption" style="color:var(--ink2)">${esc(x.detail)}</div></div></div>`;
  }).join('');
  return `<div class="card p-5 mb-4" style="border-left:4px solid var(--primary)">
    <h2 class="font-medium mb-2">${T('Processing result')}</h2>${rows}</div>`;
}

function processFiles() {
  openModal({
    title: T('Start processing?'),
    body: `<div class="type-body-small" style="color:var(--ink2)">${T('Ready files will be validated, PDFs will be extracted and reconciled, and only successful results will be imported. Failed files remain here with an error and create no entries.')}</div>`,
    actions: `<md-text-button class="p-cancel">${T('Cancel')}</md-text-button><md-filled-button class="p-go">${T('Process')}</md-filled-button>`,
    onMount: root => {
      root.querySelector('.p-cancel').onclick = () => root._close();
      root.querySelector('.p-go').onclick = async () => {
        root._close();
        const r = await api('/api/ingest/process', {});
        state.ingestResult = r.results;
        renderIngest();
      };
    },
  });
}

/* Deleting an import is irreversible, so the confirmation states exactly what it
   will take — counted from the store, not from the upload record — and refuses
   outright when any of it sits in a closed month. */
function deleteUpload(id, name) {
  openModal({
    title: T('Delete this file and its entries?'),
    body: `<div class="type-body-small" style="color:var(--ink2)">${T('All transactions imported from {name} will be permanently removed from every month/year, and their manual decisions dropped. Merchant rules are kept. This cannot be undone.', { name: `<b>${esc(name)}</b>` })}</div>
      <div class="d-detail type-body-small mt-3">${T('Checking what this would remove…')}</div>`,
    actions: `<md-text-button class="d-cancel">${T('Cancel')}</md-text-button><md-filled-button class="d-go" disabled style="--md-filled-button-container-color:var(--bad)">${T('Delete')}</md-filled-button>`,
    onMount: async root => {
      const go = root.querySelector('.d-go');
      const detail = root.querySelector('.d-detail');
      root.querySelector('.d-cancel').onclick = () => root._close();
      let info;
      try {
        info = await api(`/api/ingest/upload-contents?id=${encodeURIComponent(id)}`);
      } catch (_) { root._close(); return; }
      const years = Object.keys(info.years || {}).sort();
      if (info.closed_months.length) {
        detail.innerHTML = `<span style="color:var(--bad)">${T('Cannot delete: {months} is closed. Reopen it first.', { months: esc(info.closed_months.join(', ')) })}</span>`;
        return;   // leave Delete disabled
      }
      detail.innerHTML = `${T('{n} transactions', { n: info.transactions })}${years.length ? ` · ${years.length > 1 ? T('years') : T('year')} ${esc(years.join(', '))}` : ''}${info.decisions ? ` · ${T('{n} manual decisions will be dropped', { n: info.decisions })}` : ''}`;
      go.disabled = false;
      go.onclick = async () => {
        try { await api('/api/ingest/upload-delete', { id }); } catch (_) { return; }
        root._close(); renderIngest();
      };
    },
  });
}

/* One shared grid template (.txn-grid) for main rows AND split sub-rows, so all
   columns line up. Columns: date | name | purpose | personal | year-cost | tax |
   category | amount | edit. personal = the sharing value; year-cost & tax are
   boolean state pills (off = greyed/disabled). */
function flagPill(on, label) { return `<span class="col-flag ${on ? 'on' : 'off'}">${label}</span>`; }

function txnRow(t, i) {
  const isTransfer = t.sharing === 'out-of-scope';
  const isSplit = !!t.splits;
  const statusChip = isTransfer
    ? `<span class="chip chip-neutral">${T('out of scope')}</span>`
    : t.status === 'needs_review'
      ? `<span class="chip chip-bad">${T('review')}</span>`
      : (isSplit ? `<span class="chip chip-primary">${T('split')}</span>` : catBadge(t.category));
  // split parents are containers: their flags/amount live in the parts, so mute them
  const needsReview = !isTransfer && t.status === 'needs_review';
  return `<div class="txn-item ${isSplit ? 'is-split' : ''} ${needsReview ? 'needs-review' : ''}" style="${isTransfer ? 'opacity:.6' : ''}">
    <div class="txn-grid">
      <span class="flex justify-center"><md-checkbox class="txn-select" touch-target="wrapper" data-id="${esc(t.id)}" ${txnSelection().has(t.id) ? 'checked' : ''} aria-label="${T('Select transaction')}"></md-checkbox></span>
      <span style="color:var(--ink2)">${fmtDate(t.date)}</span>
      <span class="truncate" style="min-width:0; color:var(--ink2)" ${tooltip(accountLabel(t.account))}>${esc(accountLabel(t.account))}</span>
      <span class="flex items-center gap-1" style="min-width:0">
        <span class="truncate font-medium" ${tooltip(t.counterparty || '')}>${esc(t.counterparty) || T('(no name)')}</span>
        ${notePopover(t.id, t.note)}
        ${t.manual_edit ? `<md-icon-button class="alert-btn shrink-0" onclick="openEditModal(${i})" ${tooltip(T('Manually edited — the imported date, name or amount was changed by hand. Open Edit to view or reset the original.'))}><md-icon>warning</md-icon></md-icon-button>` : ''}
        ${transferHintChip(t)}
      </span>
      <span class="truncate" style="min-width:0; color:var(--ink2)" ${tooltip(t.purpose || '')}>${esc(t.purpose)}</span>
      <span class="flex justify-end">${isSplit ? '' : shareBadge(t.sharing)}</span>
      <span class="flex justify-end">${isSplit ? '' : flagPill(t.year_cost, T('year cost'))}</span>
      <span class="flex justify-end">${isSplit ? '' : flagPill(!!t.tax_bucket, T('tax'))}</span>
      <span class="flex items-center justify-end">${statusChip}</span>
      <span class="txn-amount ${isSplit ? 'txn-amount-muted' : (t.amount_eur > 0 ? 'text-positive' : 'text-negative')}">${fxBadge(t)}${fmt(t.amount_eur)}</span>
      <span class="flex items-center justify-end">${t.kind === 'internal-transfer' ? `<md-icon-button onclick="markNotTransfer('${esc(t.id)}')" ${tooltip(T('Not a transfer'))}><md-icon>sync_disabled</md-icon></md-icon-button>` : ''}<md-icon-button class="txn-edit" onclick="openEditModal(${i})" ${tooltip(T('Edit'))}><md-icon>edit</md-icon></md-icon-button></span>
    </div>
    ${splitChildren(t)}
  </div>`;
}

/* Split parts as sub-rows on the SAME grid. The date column holds the branch
   icon (parts have no date), so the name lines up with the main entries. */
function splitChildren(t) {
  if (!t.splits) return '';
  return t.splits.map(s => `
    <div class="txn-grid split-child">
      <span></span>
      <span class="flex justify-center"><md-icon class="split-child-arrow" style="font-size:18px; color:var(--ink2)">subdirectory_arrow_right</md-icon></span>
      <span></span>
      <span class="truncate" style="min-width:0; color:var(--ink2)">${esc(s.purpose) || catName(s.category)}</span>
      <span></span>
      <span class="flex justify-end">${shareBadge(s.sharing || 'shared')}</span>
      <span class="flex justify-end">${flagPill(s.year_cost, T('year cost'))}</span>
      <span class="flex justify-end">${flagPill(!!s.tax_bucket, T('tax'))}</span>
      <span class="flex items-center justify-end">${catBadge(s.category)}</span>
      <span class="txn-amount ${s.amount > 0 ? 'text-positive' : 'text-negative'}">${fmt(s.amount)}</span>
      <md-icon-button class="txn-edit" onclick="openSplit('${t.id}', ${t.amount_eur})" ${tooltip(T('Edit split'))}><md-icon>edit</md-icon></md-icon-button>
    </div>`).join('');
}

/* Reusable edit modal (built on the openModal shell). Opened from the row's
   edit icon; reusable from other areas too. */
function openEditModal(i) {
  openEditTransaction(window._txns[i]);
}
function openEditTransaction(t) {
  const isIncome = t.amount_eur > 0;
  openModal({
    title: T('Edit transaction'),
    width: '780px',
    body: `
      <div class="type-body mb-2 flex items-center gap-2 flex-wrap" style="color:var(--ink2)">
        <span>${fmtDate(t.date, true)} · ${esc(t.counterparty || '')}${t.purpose ? ' · ' + esc(t.purpose) : ''} · <b class="${t.amount_eur > 0 ? 'text-positive' : 'text-negative'}">${fmt(t.amount_eur)}</b> · ${esc(accountLabel(t.account))}</span>
        <md-text-button class="e-edit-entry" ${tooltip(T('Correct the raw date, name, amount or bank account of this entry'))}><md-icon slot="icon">edit</md-icon>${T('Edit entry')}</md-text-button>
      </div>
      <div class="e-note-host mb-4" data-note="${esc(t.note || '')}">
        <md-text-button class="e-note" onclick="openLocalNote(this)"><md-icon slot="icon">${t.note ? 'edit_note' : 'note_add'}</md-icon><span data-note-label>${t.note ? T('Edit note') : T('Add note')}</span></md-text-button>
        <div data-note-text class="type-body-small mt-1 note-text" ${t.note ? '' : 'hidden'}>${esc(t.note || '')}</div>
      </div>
      <div class="flex flex-col gap-4">
        <div><div class="type-label mb-1" style="color:var(--ink2)">${T('Category')}</div>${catField('id="e-cat"', t.category)}</div>
        <div><div class="type-label mb-1" style="color:var(--ink2)">${isIncome ? T('Income owner') : T('Sharing')}</div>${isIncome ? ownerField('e-owner', t.income_owner, t.account, t.sharing) : sharingOptions('e-share', t.sharing)}</div>
        ${taxReviewFields(t)}
        <div class="flex items-center gap-3 flex-wrap">${yearCostSwitch('e-yc', t.year_cost)} ${attachButton(t)}</div>
      </div>`,
    actions: `<md-outlined-button class="e-reset" ${tooltip(T('Remove manual decision; falls back to rules / review'))}>${T('Reset')}</md-outlined-button>
      <md-outlined-button class="e-split">${T('Split')}</md-outlined-button>
      <md-outlined-button class="e-review" ${tooltip(T('Send this transaction back to the review queue'))}>${T('Send to review')}</md-outlined-button>
      <div class="ml-auto"></div>
      <md-text-button class="e-cancel">${T('Cancel')}</md-text-button>
      <md-filled-button class="e-save">${T('Save')}</md-filled-button>`,
    onMount: root => {
      const save = root.querySelector('.e-save');
      const updateSave = () => { save.disabled = !root.querySelector('#e-cat').value && readSharingCtx(root) !== 'out-of-scope'; };
      root.addEventListener('change', updateSave);
      updateSave();
      root.querySelector('.e-cancel').onclick = () => root._close();
      root.querySelector('.e-edit-entry').onclick = () => openEditEntry(t);
      root.querySelector('.e-reset').onclick = async () => { root._close(); await clearDecision(t.id); };
      root.querySelector('.e-split').onclick = () => { root._close(); openSplit(t.id, t.amount_eur); };
      root.querySelector('.e-review').onclick = async () => {
        const category = root.querySelector('#e-cat').value || null;
        const sharing = readSharingCtx(root);
        root._close(); await sendToReview(t.id, { category, sharing });
      };
      save.onclick = async () => {
        const category = root.querySelector('#e-cat').value || null;
        const sharing = readSharingCtx(root);
        if (!category && sharing !== 'out-of-scope') { showError(T('Pick a category first.')); return; }
        const fields = { category, sharing, year_cost: readYearCost(root), force_review: false, ...readTaxReview(root) };
        const owner = readOwner(root); if (owner) fields.income_owner = owner;
        // The note is edited in the DOM and saved with the rest, so writing it
        // cannot re-render the page out from under this open dialog.
        fields.note = root.querySelector('.e-note-host').dataset.note || null;
        await api('/api/decision', { year: state.year, id: t.id, fields });
        root._close(); render();
      };
    },
  });
}

/* Bulk editor for the Transactions page. Built from the same shared field
   components as the single-transaction editor, so a change to a picker reaches
   both. Every field is gated by its own "change this" switch and only gated
   fields are sent: /api/decisions-bulk merges into the existing decision, so an
   untouched field keeps whatever each transaction already had. Split, raw entry
   edits and attachments are deliberately absent — they are per-transaction. */
function openBulkEdit() {
  const ids = [...txnSelection()];
  const chosen = (window._txns || []).filter(t => ids.includes(t.id));
  if (!chosen.length) return;
  const net = chosen.reduce((sum, t) => sum + t.amount_eur, 0);
  const incomeRows = chosen.filter(t => t.amount_eur > 0);
  const splitRows = chosen.filter(t => t.splits);
  const gate = (id, label) => `<label class="md-check"><md-checkbox id="${id}" touch-target="wrapper"></md-checkbox><span class="type-label">${label}</span></label>`;
  openModal({
    title: T('Bulk edit {n} transactions', { n: chosen.length }),
    width: '760px',
    body: `
      <div class="type-body-small mb-3" style="color:var(--ink2)">
        ${T('{n} selected · net {amount}', { n: chosen.length, amount: fmt(net) })}
      </div>
      <div class="type-caption mb-4" style="color:var(--ink2)">
        ${T('Only the fields you switch on are written. Everything else keeps its current value on each transaction.')}
      </div>
      ${splitRows.length ? `<div class="type-caption mb-4" style="color:var(--on-warn-container)">
        ${T('{n} of these are split transactions. Category and tax here apply to the parent only — the parts keep their own.', { n: splitRows.length })}
      </div>
      <div class="b-split-danger type-body-small mb-4 p-3" hidden style="color:var(--md-sys-color-on-error-container); background:var(--md-sys-color-error-container); border-radius:8px">
        ${T('Sharing is different: marking a split transaction out of scope removes the WHOLE transaction from every total, including parts that are categorized and counted today. Check those {n} before applying.', { n: splitRows.length })}
      </div>` : ''}
      <div class="flex flex-col gap-4">
        <div>${gate('b-do-cat', T('Change category'))}<div class="mt-1">${catField('id="b-cat"', '')}</div></div>
        <div>${gate('b-do-share', T('Change sharing'))}<div class="mt-1">${sharingOptions('b-share', 'shared')}</div></div>
        ${incomeRows.length ? `<div>${gate('b-do-owner', T('Change income owner ({n} income rows)', { n: incomeRows.length }))}<div class="mt-1">${ownerField('b-owner', '')}</div></div>` : ''}
        <div>${gate('b-do-tax', T('Change tax bucket'))}<div class="mt-1">${taxField('')}</div></div>
        <div>${gate('b-do-yc', T('Change year cost'))}<div class="mt-1">${yearCostSwitch('b-yc', false)}</div></div>
      </div>`,
    actions: `<md-outlined-button class="b-reset" ${tooltip(T('Remove manual decisions; they fall back to rules / review'))}>${T('Reset decisions')}</md-outlined-button>
      <md-outlined-button class="b-oos">${T('Out of scope')}</md-outlined-button>
      <md-outlined-button class="b-review">${T('Send to review')}</md-outlined-button>
      <div class="ml-auto"></div>
      <md-text-button class="b-cancel">${T('Cancel')}</md-text-button>
      <md-filled-button class="b-apply">${T('Apply')}</md-filled-button>`,
    onMount: root => {
      const apply = root.querySelector('.b-apply');
      const on = id => !!root.querySelector('#' + id)?.checked;
      const syncApply = () => {
        apply.disabled = !['b-do-cat', 'b-do-share', 'b-do-owner', 'b-do-tax', 'b-do-yc'].some(on);
      };
      // The split warning only matters once sharing is actually in play, so it
      // appears with the switch rather than shouting from the start.
      const danger = root.querySelector('.b-split-danger');
      const syncDanger = () => { if (danger) danger.hidden = !on('b-do-share'); };
      root.addEventListener('change', () => { syncApply(); syncDanger(); });
      syncApply();
      syncDanger();
      root.querySelector('.b-cancel').onclick = () => root._close();
      root.querySelector('.b-reset').onclick = () => bulkRun(root, ids, null);
      root.querySelector('.b-oos').onclick = () => {
        // Excluding a split parent silently drops its counted parts too. That warning rides
        // along in the one confirmation rather than opening a second dialog on top of it.
        const counted = splitRows.reduce((sum, t) => sum + (t.splits || [])
          .filter(p => (p.sharing || t.sharing) !== 'out-of-scope')
          .reduce((s, p) => s + p.amount, 0), 0);
        bulkRun(root, ids, { sharing: 'out-of-scope' }, splitRows.length
          ? T('{n} of them are split. Excluding a split removes the whole transaction, including parts worth {amount} that are counted today.',
            { n: splitRows.length, amount: fmt(counted) })
          : '');
      };
      root.querySelector('.b-review').onclick = () => bulkRun(root, ids, { force_review: true });
      apply.onclick = () => {
        const fields = {};
        if (on('b-do-cat')) fields.category = root.querySelector('#b-cat').value || null;
        // Not readSharingCtx(): that reader assumes a context shows EITHER the
        // owner segments (income) OR the sharing segments (expense), and returns
        // the owner-derived value whenever both exist. Here they are two
        // independent gated fields over a mixed selection, so read this one.
        if (on('b-do-share')) fields.sharing = readSeg(root.querySelector('.sharing-field')) || 'shared';
        if (on('b-do-owner')) { const owner = readOwner(root); if (owner) fields.income_owner = owner; }
        if (on('b-do-tax')) fields.tax_bucket = readTax(root);
        if (on('b-do-yc')) fields.year_cost = readYearCost(root);
        bulkRun(root, ids, fields);
      };
    },
  });
}

/* What a bulk operation is about to do, in words, for the confirmation. Reads the same
   `fields` object that is sent, so it can never describe something other than what happens. */
function bulkChangeList(fields) {
  if (fields === null) return [T('Remove the manual decision — each transaction falls back to its rule, or to review')];
  const out = [];
  if ('category' in fields) out.push(fields.category ? T('Category → {value}', { value: catName(fields.category) }) : T('Category → cleared'));
  if ('sharing' in fields) out.push(T('Sharing → {value}', { value: shareInfo(fields.sharing).label }));
  if ('income_owner' in fields) out.push(T('Income earned by → {value}', { value: personLabelRaw(fields.income_owner) }));
  if ('tax_bucket' in fields) out.push(fields.tax_bucket ? T('Tax bucket → {value}', { value: fields.tax_bucket }) : T('Tax bucket → cleared'));
  if ('year_cost' in fields) out.push(fields.year_cost ? T('Marked as a year cost') : T('No longer a year cost'));
  if ('force_review' in fields && fields.force_review) out.push(T('Sent back to the review queue'));
  return out;
}

/* Runs one bulk operation. `fields` null means "clear the decisions".

   Every path goes through the confirmation here rather than at each button, so a bulk action
   added later cannot forget to ask. One wrong click can rewrite hundreds of transactions, and
   the only thing standing between that and a long evening is this dialog. Errors keep the
   editor open — a batch touching a closed month is rejected whole, and losing the chosen
   values to a closed-month message would be its own small disaster. */
async function bulkRun(root, ids, fields, extraWarning = '') {
  const changes = bulkChangeList(fields);
  const body = `${T('This rewrites {n} transactions at once. It cannot be undone in one step.', { n: ids.length })}
    <ul class="bulk-confirm-list">${changes.map(c => `<li>${esc(c)}</li>`).join('')}</ul>
    ${extraWarning ? `<div class="bulk-confirm-warn">${esc(extraWarning)}</div>` : ''}`;
  confirmAction({
    title: T('Apply to {n} transactions?', { n: ids.length }),
    body,
    danger: fields === null || fields?.sharing === 'out-of-scope',
    confirmLabel: T('Apply to {n}', { n: ids.length }),
    onConfirm: () => bulkCommit(root, ids, fields),
  });
}

async function bulkCommit(root, ids, fields) {
  try {
    if (fields === null) {
      await api('/api/decisions-clear-bulk', { year: state.year, ids });
    } else {
      await api('/api/decisions-bulk', { year: state.year, items: ids.map(id => ({ id, fields })) });
    }
  } catch (_) {
    return;   // api() already surfaced the reason; keep the selection and the modal
  }
  root._close();
  clearTxnSelection();
  showMessage(fields === null
    ? T('{n} transactions reset to their rules.', { n: ids.length })
    : T('{n} transactions updated.', { n: ids.length }));
  render();
}

/* Close every open dialog (used after a modal-over-modal action so both the
   entry editor and the value editor dismiss together, then the page re-renders). */
function closeAllModals() {
  document.querySelectorAll('.app-dialog').forEach(dialog => dialog._close && dialog._close());
}

/* Modal-over-modal: correct an entry's raw values. Opens on top of the entry
   editor, so it picks up the stacked-dialog shadow automatically. The original
   values are preserved server-side; once edited, this modal offers a reset. */
function openEditEntry(t) {
  const modified = !!t.manual_edit;
  const original = t.original || {};
  openModal({
    title: T('Edit entry values'),
    width: '460px',
    body: `
      <div class="type-caption mb-4" style="color:var(--ink2)">${T("Correct this entry's raw values — e.g. the bank restated it, or it was imported under the wrong account. The original is kept so you can reset, and all totals use the corrected values.")}</div>
      <div class="flex flex-col gap-4">
        ${textField({ id: 'ee-date', label: T('Date'), type: 'date', value: t.date })}
        ${textField({ id: 'ee-name', label: T('Name'), value: t.counterparty || '' })}
        ${textField({ id: 'ee-amount', label: T('Amount (€)'), type: 'number', value: t.amount_eur, attrs: 'step="0.01"' })}
        ${selectField({ id: 'ee-account', label: T('Bank account'), value: t.account, options: accountChoices(), className: 'stage-acct', attrs: `supporting-text="${T('Also changes its owner and who it counts for in settlement.')}"` })}
      </div>
      ${modified ? `<div class="type-caption mt-4 pt-3 border-t" style="color:var(--ink2)">${T('Original (imported):')} ${fmtDate(original.date, true)} · ${esc(original.counterparty || '')} · ${fmt(original.amount_eur)} · ${esc(accountLabel(original.account))}</div>` : ''}`,
    actions: `${modified ? `<md-text-button class="ee-reset" ${tooltip(T('Restore the imported values'))}>${T('Reset to original')}</md-text-button>` : ''}
      <div class="ml-auto"></div>
      <md-text-button class="ee-cancel">${T('Cancel')}</md-text-button>
      <md-filled-button class="ee-save">${T('Save')}</md-filled-button>`,
    onMount: root => {
      root.querySelector('.ee-cancel').onclick = () => root._close();
      const reset = root.querySelector('.ee-reset');
      if (reset) reset.onclick = async () => {
        await api('/api/transaction-edit-reset', { year: state.year, id: t.id });
        closeAllModals(); render();
      };
      root.querySelector('.ee-save').onclick = async () => {
        const date = root.querySelector('#ee-date').value;
        const counterparty = root.querySelector('#ee-name').value.trim();
        const amount_eur = parseFloat(root.querySelector('#ee-amount').value);
        if (!date) { showError(T('Pick a date.')); return; }
        if (!counterparty) { showError(T('Name is required.')); return; }
        if (!isFinite(amount_eur) || cents(amount_eur) === 0) { showError(T('Amount must not be zero.')); return; }
        const account = root.querySelector('#ee-account').value;
        await api('/api/transaction-edit', { year: state.year, id: t.id, date, counterparty, amount_eur, account });
        closeAllModals(); render();
      };
    },
  });
}

function markNotTransfer(id) {
  confirmAction({
    title: T('Not an internal transfer?'),
    body: T('This transaction will return to the normal category and sharing workflow.'),
    confirmLabel: T('Not a transfer'),
    onConfirm: async () => { await api('/api/decision', { year: state.year, id, fields: { kind: 'normal' } }); render(); },
  });
}

/* ---- attachments ---- : the ONE attachment UI. attachList() reads the txn's
   attachments (migrating a legacy single `receipt`); attachButton() renders the
   trigger; openAttachments() is the unified add/list/download/delete modal. */
function attachList(t) {
  if (t && t.attachments && t.attachments.length) return t.attachments;
  if (t && t.receipt) return [{ file: t.receipt, description: '' }];
  return [];
}
function attachButton(t) {
  const n = attachList(t).length;
  return n
    ? `<md-outlined-button onclick="openAttachments('${t.id}')"><md-icon slot="icon">attachment</md-icon>${T('Has attachment')}${n > 1 ? ` (${n})` : ''}</md-outlined-button>`
    : `<md-text-button onclick="openAttachments('${t.id}')"><md-icon slot="icon">attach_file</md-icon>${T('Attach')}</md-text-button>`;
}
function openAttachments(id) {
  const t = (window._txns || []).find(x => x.id === id) ||
            (window._groups || []).flatMap(([_, ts]) => ts).find(x => x.id === id) ||
            (state.taxEditingTxn?.id === id ? state.taxEditingTxn : null);
  let atts = t ? attachList(t) : [];

  const fileName = f => f.split('__').pop();
  const bodyHtml = () => `
    <div class="att-list space-y-2 mb-4">
      ${atts.length ? atts.map(a => `
        <div class="flex items-center gap-2 att-item" data-file="${esc(a.file)}">
          <md-icon style="color:var(--primary)">description</md-icon>
          <div class="flex-1 min-w-0">
            <div class="truncate type-body-small">${esc(fileName(a.file))}</div>
            ${a.description ? `<div class="type-caption truncate" style="color:var(--ink2)">${esc(a.description)}</div>` : ''}
          </div>
          <a href="/receipts/${esc(a.file)}" download="${esc(fileName(a.file))}" title="${T('Download')}"><md-icon-button><md-icon>download</md-icon></md-icon-button></a>
          <md-icon-button class="att-del" title="${T('Delete')}"><md-icon>delete</md-icon></md-icon-button>
        </div>`).join('') : `<div class="type-body-small" style="color:var(--ink2)">${T('No attachments yet.')}</div>`}
    </div>
    <div class="pt-3 border-t" style="border-color:var(--line)">
      <div class="type-label mb-2" style="color:var(--ink2)">${T('Add a file')}</div>
      <div class="flex items-center gap-2 flex-wrap">
        ${fileField({ label: T('Choose attachment'), className: 'att-file' })}
        ${textField({ label: T('Description'), className: 'att-desc w-52', placeholder: T('Optional') })}
        <md-filled-button class="att-upload"><md-icon slot="icon">upload</md-icon>${T('Upload')}</md-filled-button>
      </div>
      <div class="att-status type-caption mt-2" style="color:var(--ink2)"></div>
    </div>`;

  const back = openModal({
    title: T('Attachments'),
    width: '560px',
    body: bodyHtml(),
    actions: `<md-filled-button class="att-done">${T('Done')}</md-filled-button>`,
    onClose: () => render(),
    onMount: root => {
      const bodyEl = root.querySelector('.generic-modal-body');
      const refresh = () => { bodyEl.innerHTML = bodyHtml(); wire(); };
      const wire = () => {
        wireFileField(bodyEl);
        root.querySelector('.att-done').onclick = () => root._close();
        bodyEl.querySelectorAll('.att-item').forEach(item => {
          item.querySelector('.att-del').onclick = async () => {
            const r = await api('/api/attachment-delete', { year: state.year, txn_id: id, file: item.dataset.file });
            atts = r.attachments; refresh();
          };
        });
        bodyEl.querySelector('.att-upload').onclick = async () => {
          const fileEl = bodyEl.querySelector('.att-file .md-file-input');
          if (!fileEl.files.length) { showError(T('Choose a file first.')); return; }
          const status = bodyEl.querySelector('.att-status');
          status.textContent = T('Uploading…');
          const fd = new FormData();
          fd.append('year', state.year);
          fd.append('txn_id', id);
          fd.append('description', bodyEl.querySelector('.att-desc').value.trim());
          fd.append('file', fileEl.files[0]);
          const res = await fetch('/api/attachment-add', { method: 'POST', body: fd });
          if (!res.ok) { status.textContent = T('Upload failed: ') + await res.text(); return; }
          atts = (await res.json()).attachments; refresh();
        };
      };
      wire();
    },
  });
  return back;
}


async function clearDecision(id) {
  await api('/api/decision-clear', { year: state.year, id });
  render();
}

/* Force one transaction back into the review queue while KEEPING its chosen category/sharing
   (they still count in all math). Saving/applying later clears force_review (see below). */
async function sendToReview(id, keep = {}) {
  const fields = { force_review: true };
  if (keep.category) fields.category = keep.category;
  if (keep.sharing) fields.sharing = keep.sharing;
  await api('/api/decision', { year: state.year, id, fields });
  render();
}

/* ---------- rules ---------- */
async function renderRules() {
  const data = await api('/api/rules');
  const scope = state.rulesScope || 'family';
  const cap = w => w[0].toUpperCase() + w.slice(1);
  const rules = data.rules.filter(r => (r.scope || 'family') === scope);
  $('#main').innerHTML = `
    ${state.ruleResult ? `<div class="card p-4 mb-4 flex items-center gap-2" style="border-left:4px solid var(--good)"><md-icon style="color:var(--good)">check_circle</md-icon><span>${esc(state.ruleResult)}</span></div>` : ''}
    <div class="card p-5 mb-4">
      <h2 class="font-medium mb-3">${T('Add rule')}</h2>
      <div class="flex gap-2 items-center flex-wrap" id="r-form" data-note="">
        ${textField({ id: 'r-pattern', label: T('Match pattern'), placeholder: 'e.g. VODAFONE', className: 'w-52' })}
        <md-icon class="cursor-pointer" style="color:var(--primary); font-size:22px;" onclick="openLocalNote(this)" ${tooltip(T('Add note to rule'))}>note_add</md-icon>
        ${segControl('r-field', [['counterparty', T('Merchant name')], ['purpose', T('Purpose text')], ['any', T('Either')]], 'counterparty')}
        ${selectField({ id: 'r-scope', label: T('Rule scope'), value: scope, options: scopeChoices() })}
        ${catField('id="r-cat"', '')}
        ${sharingOptions('r-share', '')}
        ${checkboxField({ id: 'r-review', label: T('Always send to Review'), className: 'type-label' })}
        <md-filled-button onclick="saveRule()">${T('Add')}</md-filled-button>
      </div>
      <div class="type-caption mt-2" style="color:var(--ink2)">${T("Person rules apply only to that person's accounts and override family rules; family rules apply to everyone (incl. the joint account). Rules apply to ALL history instantly — past months recalculate automatically.")}</div>
    </div>
    <div class="flex items-center gap-3 mb-3">
      <span class="type-body-small" style="color:var(--ink2)">${T('Show rules of:')}</span>
      ${selectField({ id: 'rules-filter', label: T('Rule profile'), value: scope, options: [
        ['family', T('Family ({n})', { n: data.rules.filter(r => (r.scope || 'family') === 'family').length })],
        ...state.meta.people.map(p => [p, `${personLabelRaw(p)} (${data.rules.filter(r => r.scope === p).length})`]),
      ] })}
    </div>
    <div class="card">
      ${rules.map(r => `
        <div class="flex items-center gap-3 px-4 py-2 type-body-small border-b" style="border-color:var(--line)">
          <span class="chip chip-primary">${esc(r.match.contains)}</span>
          <span class="type-caption" style="color:var(--ink2)">${T('in {field}', { field: esc(r.match.field) })}</span>
          <md-icon class="cursor-pointer" data-note="${esc(r.note || '')}" style="font-size:19px; color:${r.note ? 'var(--primary)' : 'var(--ink2)'}" ${tooltip(r.note ? r.note : T('Add note'))} onclick="openRuleNote('${r.id}', this.dataset.note)">${r.note ? 'edit_note' : 'note_add'}</md-icon>
          <span class="flex-1">${r.action === 'review' ? `<i style="color:var(--on-warn-container)">${T('→ always review')}</i>` : catBadge(r.category)}</span>
          <span class="type-caption" style="color:var(--ink2)">${esc(r.sharing || '')} ${r.tax_bucket ? '· tax:' + esc(r.tax_bucket) : ''}</span>
          ${selectField({ label: T('Scope'), value: r.scope || 'family', options: scopeChoices(), attrs: `onchange="moveRule('${r.id}', this.value)" ${tooltip(T('Move this rule to another profile'))}` })}
          <md-outlined-button data-rule-id="${esc(r.id)}" onclick="openApplyRule(this.dataset.ruleId)" ${tooltip(T('Replace manual classifications in the selected year with this live rule'))}>${T('Apply to entries')}</md-outlined-button>
          <md-text-button data-rule-id="${esc(r.id)}" onclick="openRuleEdit(this.dataset.ruleId)" ${tooltip(T('Change this rule’s pattern, category, sharing or scope'))}><md-icon slot="icon">edit</md-icon>${T('Edit')}</md-text-button>
          <md-text-button onclick="deleteRule('${r.id}')">${T('Delete')}</md-text-button>
        </div>`).join('') || `<div class="p-6 type-body-small text-center" style="color:var(--ink2)">${scope === 'family' ? T('No family rules yet. Create them here or via “Apply + rule” in Review.') : T("No {name}'s rules yet. Create them here or via “Apply + rule” in Review.", { name: personLabel(scope) })}</div>`}
    </div>`;
  $('#rules-filter').onchange = e => { state.rulesScope = e.target.value; render(); };
}

async function saveRule() {
  const pattern = $('#r-pattern').value.trim();
  if (pattern.length < 3) { showError(T('Pattern must contain at least 3 characters to avoid over-matching.')); return; }
  const review = $('#r-review').checked;
  if (!review && !$('#r-cat').value) { showError(T('Pick a category or enable “Always send to Review”.')); return; }
  await api('/api/rule', {
    pattern,
    field: readSeg(document, 'r-field'),
    category: $('#r-cat').value || null,
    sharing: readSeg($('#r-share')) || 'shared',
    action: review ? 'review' : null,
    scope: $('#r-scope').value,
    note: $('#r-form').dataset.note || null,
  });
  render();
}

function openRuleNote(id, current) {
  // send '' (not null) to clear — the endpoint treats null as "unchanged"
  noteModal(current, async text => { await api('/api/rule-update', { id, note: text }); render(); });
}

async function moveRule(id, scope) {
  await api('/api/rule-update', { id, scope });
  state.rulesScope = scope;
  render();
}

async function openApplyRule(id) {
  const impact = await api(`/api/rule-impact?rule_id=${encodeURIComponent(id)}&year=${state.year}`);
  const needsFieldFix = impact.matched === 0 && (impact.field_matches?.any || 0) > 0;
  const detail = [
    T('{n} transactions currently match this rule', { n: impact.matched }),
    T('{n} already use the rule', { n: impact.already_rule_controlled }),
    T('{n} have manual classification overrides', { n: impact.manual_overrides }),
    impact.skipped_splits ? T('{n} split transactions will be preserved', { n: impact.skipped_splits }) : '',
    impact.skipped_closed ? T('{n} transactions in closed months will be preserved', { n: impact.skipped_closed }) : '',
  ].filter(Boolean);
  openModal({
    title: T('Apply rule to {year} entries?', { year: state.year }),
    body: `<div class="space-y-2 type-body-small">
      ${detail.map(line => `<div class="flex items-center gap-2"><md-icon>check_circle</md-icon><span>${esc(line)}</span></div>`).join('')}
      ${needsFieldFix ? `<div class="card p-3 mt-3" style="border-left:4px solid var(--warn)">
        <div class="font-medium">${T('The pattern exists, but not in the selected field.')}</div>
        <div class="type-caption mt-1" style="color:var(--ink2)">${T('{cp} match in merchant name · {pp} match in purpose text. Change this rule to “Either” to include them.', { cp: impact.field_matches.counterparty, pp: impact.field_matches.purpose })}</div>
      </div>` : ''}
      <div class="type-caption mt-3" style="color:var(--ink2)">${T('This removes only manual category, sharing and tax overrides from eligible transactions. Notes, attachments, account changes and splits are preserved. Future rule edits will continue to update these entries.')}</div>
    </div>`,
    actions: `<md-text-button class="ra-cancel">${T('Cancel')}</md-text-button>${needsFieldFix ? `<md-outlined-button class="ra-either">${T('Use Either')}</md-outlined-button>` : ''}<md-filled-button class="ra-apply" ${impact.eligible ? '' : 'disabled'}>${T('Apply to {n}', { n: impact.eligible })}</md-filled-button>`,
    onMount: root => {
      root.querySelector('.ra-cancel').onclick = () => root._close();
      const either = root.querySelector('.ra-either');
      if (either) either.onclick = async () => {
        await api('/api/rule-update', { id, field: 'any' });
        root._close();
        openApplyRule(id);
      };
      root.querySelector('.ra-apply').onclick = async () => {
        const result = await api('/api/rule-apply', { id, year: state.year });
        root._close();
        state.ruleResult = T('{n} existing transactions now use this rule.', { n: result.applied });
        render();
      };
    },
  });
}

function deleteRule(id) {
  confirmAction({
    title: T('Delete rule?'), danger: true, confirmLabel: T('Delete'),
    body: T('Transactions categorized by this rule will return to Review unless they have a manual decision.'),
    onConfirm: async () => { await api('/api/rule-delete', { id }); render(); },
  });
}

/* ---------- categories ---------- */
async function renderCategories() {
  const { usage, rules } = await api('/api/category-usage');
  const cap = w => w[0].toUpperCase() + w.slice(1);
  const showArch = !!state.showArchived;
  const cats = state.meta.categories;
  const useTxn = slug => usage[slug] || 0;
  const useRule = slug => rules[slug] || 0;
  const groupTxn = g => g.subs.reduce((n, s) => n + useTxn(`${g.slug}/${s.slug}`), 0) + useTxn(g.slug);
  const groupRule = g => g.subs.reduce((n, s) => n + useRule(`${g.slug}/${s.slug}`), 0) + useRule(g.slug);

  const usageChip = (t, r) => {
    if (!t && !r) return `<span class="type-caption" style="color:var(--ink2)">${T('unused')}</span>`;
    const parts = [];
    if (t) parts.push(T('{n} txns', { n: t }));
    if (r) parts.push(T('{n} rules', { n: r }));
    return `<span class="type-caption" style="color:var(--ink2)">${parts.join(' · ')}</span>`;
  };
  const iconBtn = (icon, title, onclick, disabled) =>
    `<md-icon-button title="${esc(title)}" ${disabled ? 'disabled' : ''} ${onclick ? `onclick="${onclick}"` : ''}><md-icon>${icon}</md-icon></md-icon-button>`;
  const actions = (slug, name, archived, canDelete) => `<span class="cat-actions">
    ${iconBtn('edit', T('Rename'), `renameCategory('${slug}', ${JSON.stringify(name).replace(/"/g, '&quot;')})`)}
    ${iconBtn(archived ? 'unarchive' : 'archive', archived ? T('Restore') : T('Archive'), `archiveCategory('${slug}', ${!archived})`)}
    ${iconBtn('delete', canDelete ? T('Delete') : T('In use — archive instead of deleting'), canDelete ? `deleteCategory('${slug}')` : '', !canDelete)}
  </span>`;

  const groupSlugs = cats.map(c => c.slug);
  $('#main').innerHTML = `
    <div class="card p-5 mb-4">
      <h2 class="font-medium mb-3">${T('Add category')}</h2>
      <div class="flex flex-wrap items-stretch" style="gap:40px">
        <div>
          <div class="type-label mb-2" style="color:var(--ink2)">${T('New group')}</div>
          <div class="flex gap-2 items-center flex-wrap">
            ${textField({ id: 'cat-new-name', label: T('Name'), placeholder: T('e.g. Health'), className: 'w-52' })}
            ${selectField({ id: 'cat-new-type', label: T('Type'), value: 'expense', options: [['expense', T('Expense')], ['income', T('Income')]] })}
            <md-filled-button onclick="addGroup()">${T('Add group')}</md-filled-button>
          </div>
        </div>
        <div style="width:1px;background:var(--line);align-self:stretch"></div>
        <div>
          <div class="type-label mb-2" style="color:var(--ink2)">${T('New subcategory')}</div>
          <div class="flex gap-2 items-center flex-wrap">
            ${selectField({ id: 'sub-group', label: T('Parent group'), value: groupSlugs[0] || '', options: groupSlugs.map((s, i) => [s, cats[i].name]) })}
            ${textField({ id: 'sub-new-name', label: T('Name'), placeholder: T('e.g. Pharmacy'), className: 'w-52' })}
            <md-filled-button onclick="addSubCategory()">${T('Add sub')}</md-filled-button>
          </div>
        </div>
      </div>
      <div class="type-caption mt-3" style="color:var(--ink2)">${T('Renaming changes the label only — the underlying id and all past transactions stay linked. Archiving hides a category from new assignment but keeps historical reports intact. A category can only be deleted while it is completely unused.')}</div>
    </div>
    <div class="flex items-center gap-3 mb-3">
      ${checkboxField({ id: 'cat-show-arch', label: T('Show archived'), checked: showArch, className: 'type-body-small' })}
    </div>
    <div class="space-y-3">
      ${cats.filter(g => showArch || !g.archived).map(g => `
        <div class="card cat-card" style="--acc:${catColor(g)};${g.archived ? 'opacity:.55' : ''}">
          <div class="cat-head flex items-center gap-3 px-4 py-3 border-b" style="border-color:var(--line)">
            <md-icon style="color:${catColor(g)}">${defaultIcon(g)}</md-icon>
            <span class="font-medium">${g.name}</span>
            <span class="chip ${g.type === 'income' ? 'chip-primary' : ''}">${g.type === 'income' ? T('Income') : T('Expense')}</span>
            ${g.dynamic ? `<span class="chip">${T('dynamic')}</span>` : ''}
            ${g.archived ? `<span class="chip chip-bad">${T('archived')}</span>` : ''}
            ${usageChip(groupTxn(g), groupRule(g))}
            <span class="flex-1"></span>
            <span class="cat-actions">
              ${iconBtn(state.styleOpen === g.slug ? 'close' : 'palette', state.styleOpen === g.slug ? T('Close style') : T('Style'), `toggleStyle('${g.slug}')`)}
              ${actions(g.slug, g.name, g.archived, g.subs.length === 0 && !groupTxn(g) && !groupRule(g))}
            </span>
          </div>
          ${state.styleOpen === g.slug ? styleEditor(g) : ''}
          ${g.subs.filter(s => showArch || !s.archived).map(s => {
            const full = `${g.slug}/${s.slug}`;
            const t = useTxn(full), r = useRule(full);
            return `<div class="cat-sub flex items-center gap-3 px-4 py-2 type-body-small border-b" style="border-color:var(--line);${s.archived ? 'opacity:.55' : ''}">
              <span style="padding-left:12px">${s.name}</span>
              ${s.ratio_income ? `<span class="chip chip-primary" ${tooltip(T('Settlement income basis'))}>${T('ratio')}</span>` : ''}
              ${s.archived ? `<span class="chip chip-bad">${T('archived')}</span>` : ''}
              ${s.watch ? `<span class="chip chip-primary" ${tooltip(T('On the dashboard watch-list'))}>${T('watching')}</span>` : ''}
              ${usageChip(t, r)}
              <span class="flex-1"></span>
              ${g.type === 'income' ? '' : `<md-icon-button title="${s.watch ? T('Watching this cost closely — click to stop') : T('Watch this cost closely')}" onclick="toggleWatch('${full}', ${!s.watch})"><md-icon style="color:${s.watch ? 'var(--primary)' : 'var(--ink2)'}">visibility</md-icon></md-icon-button>`}
              ${s.ratio_income ? '' : actions(full, s.name, s.archived, !t && !r)}
            </div>`;
          }).join('') || `<div class="cat-sub px-4 py-2 type-caption" style="color:var(--ink2)">${T('No subcategories yet.')}</div>`}
        </div>`).join('')}
    </div>`;
  $('#cat-show-arch').onchange = e => { state.showArchived = e.target.checked; render(); };
}

function styleEditor(g) {
  const cur = catColor(g).toLowerCase(), curIcon = defaultIcon(g);
  return `<div class="px-4 py-3 border-b" style="border-color:var(--line);background:var(--md-sys-color-surface-variant)">
    <div class="type-label mb-2" style="color:var(--ink2)">${T('Color')}</div>
    <div class="flex flex-wrap gap-2 mb-3">
      ${MATERIAL_PALETTE.map(([name, hex]) => `<button type="button" class="swatch-pick ${hex.toLowerCase() === cur ? 'selected' : ''}" title="${name}" style="background:${hex}" onclick="setCatColor('${g.slug}','${hex}')"></button>`).join('')}
    </div>
    <div class="type-label mb-2" style="color:var(--ink2)">${T('Icon')}</div>
    <div class="flex flex-wrap gap-1">
      ${CATEGORY_ICONS.map(ic => `<button type="button" class="icon-pick ${ic === curIcon ? 'selected' : ''}" title="${ic}" onclick="setCatIcon('${g.slug}','${ic}')"><md-icon>${ic}</md-icon></button>`).join('')}
    </div>
  </div>`;
}

function toggleStyle(slug) {
  state.styleOpen = state.styleOpen === slug ? null : slug;
  render();
}

async function setCatColor(slug, color) {
  await api('/api/category-style', { slug, color });
  state.meta = await api('/api/meta');
  render();
}

async function setCatIcon(slug, icon) {
  await api('/api/category-style', { slug, icon });
  state.meta = await api('/api/meta');
  render();
}

async function addGroup() {
  const name = $('#cat-new-name').value.trim();
  if (!name) { showError(T('Name is required.')); return; }
  await api('/api/category-add', { parent: null, name, type: $('#cat-new-type').value });
  state.meta = await api('/api/meta');
  render();
}

async function addSubCategory() {
  const name = $('#sub-new-name').value.trim();
  if (!name) { showError(T('Name is required.')); return; }
  await api('/api/category-add', { parent: $('#sub-group').value, name });
  state.meta = await api('/api/meta');
  render();
}

function renameCategory(slug, current) {
  promptText({
    title: T('Rename category'), label: T('Display name'), value: current,
    onConfirm: async name => {
      if (name === current) return;
      await api('/api/category-rename', { slug, name });
      state.meta = await api('/api/meta'); render();
    },
  });
}

async function archiveCategory(slug, archived) {
  await api('/api/category-archive', { slug, archived });
  state.meta = await api('/api/meta');
  render();
}

async function toggleWatch(slug, watch) {
  await api('/api/category-watch', { slug, watch });
  state.meta = await api('/api/meta');
  render();
}

function deleteCategory(slug) {
  confirmAction({
    title: T('Delete category?'), danger: true, confirmLabel: T('Delete'),
    body: T('Delete “{slug}” permanently? This is only allowed while it is unused.', { slug: esc(slug) }),
    onConfirm: async () => { await api('/api/category-delete', { slug }); state.meta = await api('/api/meta'); render(); },
  });
}

/* ---------- review ---------- */
function groupKey(t) { return (t.counterparty || t.purpose || 'unknown').toUpperCase().replace(/[0-9]/g, '').trim().slice(0, 28); }

function defaultScope(txns) {
  const owners = [...new Set(txns.map(t => {
    const a = state.meta.accounts.find(a => a.id === t.account);
    return a ? a.owner : null;
  }))];
  return owners.length === 1 && state.meta.people.includes(owners[0]) ? owners[0] : 'family';
}

const REVIEW_BATCH_SIZE = 30;

function reviewDetailsHtml(txns, gi) {
  return `<div class="space-y-1 pt-1">${txns.map((t, j) => `
    <div class="flex gap-3 type-body-small items-center py-1 border-t" style="border-color:var(--line)" id="rev-row-${gi}-${j}">
      <span class="w-24 shrink-0" style="color:var(--ink2)">${fmtDate(t.date, true)}</span>
      <span class="flex items-center gap-1 min-w-0" style="max-width:340px">
        <span class="truncate" ${tooltip((t.purpose || '') + ' [' + t.account + ']')}>${esc(t.purpose || t.counterparty)}</span>
        ${notePopover(t.id, t.note)}
      </span>
      ${transferHintChip(t)}
      ${t.error ? `<span class="chip chip-bad" ${tooltip(t.error)}>${T('Fix:')} ${esc(t.error)}</span>` : ''}
      <span class="shrink-0 font-medium ${t.amount_eur > 0 ? 'text-positive' : 'text-negative'}">${fmt(t.amount_eur)}${t.currency !== 'EUR' ? ` <span class="type-caption" style="color:var(--ink2)">(${t.amount_original} ${t.currency})</span>` : ''}</span>
      <span class="flex-1"></span>
      ${selectField({ label: T('Bank account'), value: t.account, options: accountChoices(), className: 'stage-acct shrink-0', attrs: `onchange="setReviewAccount('${t.id}', this.value)" ${tooltip(T('Bank account'))}` })}
      <div id="act-${gi}-${j}" class="flex items-center gap-2 shrink-0">
        <md-outlined-button onclick="reviewSplit(${gi}, ${j})">${T('Split')}</md-outlined-button>
        <md-outlined-button onclick="reviewOOS(${gi}, ${j})" ${tooltip(T('Mark as out of scope'))}>${T('Out of scope')}</md-outlined-button>
      </div>
    </div>`).join('')}</div>`;
}

function reviewGroupHtml([key, txns], gi) {
  const total = txns.reduce((a, t) => a + t.amount_eur, 0);
  const isIncome = total > 0;
  const open = window._reviewExpanded.has(key);
  return `<div class="card p-4 mb-3" id="g${gi}">
      <div class="flex items-center gap-3 flex-wrap">
        <div class="font-medium type-title">${esc(txns[0].counterparty || txns[0].purpose) || T('(no name)')}</div>
        <span class="chip chip-primary">${txns.length}×</span>
        <span class="type-headline font-medium ${isIncome ? 'text-positive' : 'text-negative'}">${fmt(total)}</span>
        <div class="ml-auto flex items-center gap-2 flex-wrap">
          ${yearCostSwitch('yc-' + gi, false)}
          ${taxField(txns[0].tax_bucket)}
          ${isIncome
            ? ownerField('owner-' + gi, txns[0].income_owner, txns[0].account, txns[0].sharing)
            : sharingOptions('share-' + gi, '')}
          ${catField('id="cat-' + gi + '"', '')}
          <md-filled-button onclick="applyGroup(${gi})">${T('Apply')}</md-filled-button>
        </div>
      </div>
      <div class="flex items-center gap-2 mt-2">
        <span class="chip ${txns[0].matched_rule ? 'chip-good' : 'chip-neutral'}" id="rule-status-${gi}">${txns[0].matched_rule ? T('Rule saved ({rule})', { rule: txns[0].matched_rule }) : T('Rule not saved')}</span>
        <md-filled-button onclick="openRuleModal(${gi})">${T('Save rule')}</md-filled-button>
      </div>
      ${accordion({
        cls: 'acc-compact mt-2',
        attrs: `data-key="${esc(key)}" data-group-index="${gi}" data-rendered="${open ? '1' : '0'}"`,
        open,
        headerHtml: `<span class="type-label">${T('show transactions')}</span>`,
        onToggle: `toggleReviewGroup(this.closest('.acc'), ${gi})`,
        bodyHtml: open ? reviewDetailsHtml(txns, gi) : '',
      })}
      <div id="split-area-${gi}"></div>
    </div>`;
}

/* Returns false when there was nothing left to append, so the replay loop in
   renderReview() knows to stop. state.reviewBatches remembers how far the user
   had loaded, so an in-place re-render rebuilds the page at its previous height
   instead of collapsing back to the first batch under a restored scroll offset. */
function appendReviewGroups() {
  const host = $('#review-groups');
  if (!host) return false;
  const start = +(host.dataset.rendered || 0);
  const end = Math.min(start + REVIEW_BATCH_SIZE, window._groups.length);
  if (end === start) return false;
  host.insertAdjacentHTML('beforeend', window._groups.slice(start, end).map((group, offset) => reviewGroupHtml(group, start + offset)).join(''));
  host.dataset.rendered = String(end);
  state.reviewBatches = Math.ceil(end / REVIEW_BATCH_SIZE);
  const more = $('#review-more');
  if (more) {
    more.hidden = end >= window._groups.length;
    more.textContent = T('Load {n} more groups', { n: Math.min(REVIEW_BATCH_SIZE, window._groups.length - end) });
  }
  attachTooltips();
  return true;
}

function toggleReviewGroup(acc, gi) {
  const key = acc.dataset.key;
  if (!acc.classList.contains('open')) { window._reviewExpanded.delete(key); return; }
  window._reviewExpanded.add(key);
  if (acc.dataset.rendered === '1') return;
  acc.querySelector('.acc-body').innerHTML = reviewDetailsHtml(window._groups[gi][1], gi);
  acc.dataset.rendered = '1';
  attachTooltips();
}

/* ---------- detected internal transfers ----------
   Marking a transaction as a transfer between our own accounts removes it from
   every total. That is the only judgement the app makes on its own that money
   depends on, and until it was listed here a wrong one was invisible: the
   transaction simply stopped existing. Nothing is auto-accepted — each pair waits
   for a human, staying excluded meanwhile so the totals never swing on a guess. */
const TRANSFER_REASONS = {
  'pair:same-owner': 'Opposite amounts on two accounts you both own',
  'pair:named': 'Opposite amounts, and your name appears in the text',
  'pair:fx-tolerant': 'Opposite amounts across currencies, allowing for conversion',
  marker: 'The text matches one of your transfer markers',
  decision: 'You marked this yourself',
};

function transferLegRow(leg) {
  const text = esc(leg.counterparty || leg.purpose || '—');
  return `<div class="flex items-center gap-3 py-1">
      <span style="width:5.5rem;color:var(--ink2)">${fmtDate(leg.date, true)}</span>
      <span style="width:7rem;text-align:right;font-variant-numeric:tabular-nums">${fmt(leg.amount_eur)}</span>
      <span style="width:11rem;color:var(--ink2)">${esc(accountLabel(leg.account))}</span>
      <span class="flex-1 truncate" title="${text}">${text}</span>
    </div>`;
}

function transferCard(group) {
  const reason = T(TRANSFER_REASONS[group.reason] || group.reason);
  const single = group.legs.length < 2;
  return `<div class="card p-4 mb-3">
      <div class="flex items-center justify-between gap-3 mb-2">
        <span class="type-title-small">${fmt(group.amount_eur)}</span>
        <span class="type-body-small" style="color:var(--ink2)">${esc(reason)}</span>
      </div>
      <div class="type-body-small mb-3">${group.legs.map(transferLegRow).join('')}</div>
      ${single ? `<div class="type-body-small mb-2" style="color:var(--on-warn-container)">${T('No matching second leg was found for this one.')}</div>` : ''}
      <div class="flex gap-2 items-center">
        <md-filled-button onclick="confirmTransfer('${esc(group.id)}', true)">
          <md-icon slot="icon">check</md-icon>${T('Yes, my own money moving')}</md-filled-button>
        <md-outlined-button onclick="confirmTransfer('${esc(group.id)}', false)">
          <md-icon slot="icon">close</md-icon>${T('No, count it')}</md-outlined-button>
        ${group.month_closed ? `<span class="type-body-small" style="color:var(--ink2)">${T('Month is closed — confirming is fine, rejecting needs it reopened.')}</span>` : ''}
      </div>
    </div>`;
}

function transfersReviewSection(data) {
  const pending = (data.items || []).filter(g => g.status === 'pending');
  if (!pending.length) return '';
  return `<section class="mb-6">
      <h2 class="type-title-medium mb-1">${T('Money moved between your own accounts')}</h2>
      <p class="type-body-small mb-3" style="color:var(--ink2)">${T('{n} of these are being left out of your totals. They are not counted while you decide. Confirm each one, or say it is a real transaction and it goes back into the numbers.', { n: pending.length })}</p>
      ${pending.map(transferCard).join('')}
    </section>`;
}

async function confirmTransfer(id, confirmed) {
  try {
    await api('/api/transfer-confirm', { year: state.year, id, confirmed });
  } catch (err) {
    showError(err.message || String(err));
    return;
  }
  invalidateYearCache();   // the verdict moves totals, review counts and the badge
  render();
}

async function renderReview(renderId = state.renderId) {
  const tab = 'review';
  const year = state.year;
  window._reviewExpanded = window._reviewExpanded || new Set();
  window._decided = {};   // ids split / out-of-scoped in this render (skipped by applyGroup)
  const [{ items }, transfers] = await Promise.all([
    cachedYearData('review', `/api/review?year=${year}`),
    cachedYearData('transfers', `/api/transfers?year=${year}`),
  ]);
  if (!renderIsCurrent(renderId, tab, year)) return;
  setReviewBadge(items.length + transfers.pending);
  const transferSection = transfersReviewSection(transfers);
  if (!items.length) {
    $('#main').innerHTML = transferSection ||
      `<div class="card p-8 text-center" style="color:var(--good)">✓ ${T('Nothing to review.')}</div>`;
    return;
  }
  const groups = {};
  items.forEach(t => (groups[groupKey(t)] = groups[groupKey(t)] || []).push(t));
  const sorted = Object.entries(groups).sort((a, b) => b[1].length - a[1].length);
  window._groups = sorted;
  $('#main').innerHTML = `${transferSection || ''}<div class="mb-4 type-body-small" style="color:var(--ink2)">${T('{items} transactions in {groups} groups. Showing {shown} at a time. Pick a category once per group — “Apply + rule” books everything matching and remembers it forever.', { items: items.length, groups: sorted.length, shown: Math.min(REVIEW_BATCH_SIZE, sorted.length) })}</div>
    <div id="review-groups" data-rendered="0"></div>
    <div class="flex justify-center mt-4"><md-outlined-button id="review-more" onclick="appendReviewGroups()"></md-outlined-button></div>`;
  for (let batch = Math.max(1, state.reviewBatches); batch > 0; batch--) {
    if (!appendReviewGroups()) break;
  }
}

async function applyGroup(gi) {
  const [key, txns] = window._groups[gi];
  const card = $(`#g${gi}`);
  const category = $(`#cat-${gi}`).value || null;
  const sharing = readSharingCtx(card);
  const owner = readOwner(card);
  const yearCost = readYearCost(card);
  const tTaxBucket = readTax(card);

  if (!category && sharing !== 'out-of-scope') { showError(T('Pick a category first.')); return; }
  const decided = window._decided || {};
  const items = [];
  for (const t of txns) {
    if (decided[t.id]) continue;   // already split / out-of-scoped individually — never overwrite
    const fields = { category, sharing, year_cost: yearCost, tax_bucket: tTaxBucket, force_review: false };
    if (owner) fields.income_owner = owner;
    items.push({ id: t.id, fields });
  }
  if (items.length) {
    await api('/api/decisions-bulk', { year: state.year, items });
    // The bulk endpoint is quiet by default because callers know more than it does: here we
    // can name what was booked and where, which is the difference between "something
    // happened" and being able to spot the wrong category before the queue scrolls away.
    showMessage(category
      ? T('{n} booked to {category}.', { n: items.length, category: catName(category) })
      : T('{n} marked out of scope.', { n: items.length }));
  }
  render();
}

/* Per-row actions in the review queue. Each acts on ONE transaction, marks it
   as individually decided (so applyGroup skips it), and offers Undo. */
function reviewSplit(gi, j) {
  const t = window._groups[gi][1][j];
  openSplit(t.id, t.amount_eur, parts => markReviewDone(gi, j, t.id, T('Split into {n} parts', { n: parts.length })));
}
function reviewOOS(gi, j) {
  const t = window._groups[gi][1][j];
  markOutOfScope(t.id, () => markReviewDone(gi, j, t.id, T('Out of scope')));
}
function markReviewDone(gi, j, id, label) {
  (window._decided = window._decided || {})[id] = true;
  const act = $(`#act-${gi}-${j}`);
  if (act) act.innerHTML = `<span class="chip chip-good">${esc(label)}</span><md-text-button onclick="reviewUndo('${id}')">${T('Undo')}</md-text-button>`;
}
async function reviewUndo(id) {
  await api('/api/decision-clear', { year: state.year, id });
  if (window._decided) delete window._decided[id];
  render();
}
async function setReviewAccount(id, account) {
  await api('/api/decision', { year: state.year, id, fields: { account } });
}

/* THE rule form — one component behind both "create rule from a review group"
   (openRuleModal) and "edit an existing rule" (openRuleEdit). Render with
   ruleFormBody(values), read it back with readRuleForm(root). Never hand-build a
   second copy: a field added here must appear on both screens. */
function ruleFormBody(values = {}) {
  const { pattern = '', field = 'counterparty', category = '', sharing = 'shared', scope = 'family', review = false, note = '' } = values;
  return `<div class="space-y-3" id="rm-modal-body" data-note="${esc(note)}">
      <div class="flex gap-2 items-end">
        <div class="flex-1">
          <label class="type-label" style="color:var(--ink2)">${T('Pattern (min 3 chars)')}</label>
          ${textField({ id: 'rm-pattern', label: T('Match pattern'), className: 'w-full mt-1', value: pattern })}
        </div>
        <md-icon class="cursor-pointer pb-2" style="color:var(--primary); font-size:24px;" onclick="openLocalNote(this)" ${tooltip(T('Add note to rule'))}>${note ? 'edit_note' : 'note_add'}</md-icon>
      </div>
      <div>
        <label class="type-label" style="color:var(--ink2)">${T('Match in')}</label>
        <div class="mt-1">${segControl('rm-field', [['counterparty', T('Merchant name')], ['purpose', T('Purpose text')], ['any', T('Either')]], field)}</div>
      </div>
      <div>
        <label class="type-label" style="color:var(--ink2)">${T('Category & Sharing')}</label>
        <div class="flex flex-col gap-2 mt-1">
          ${catField('id="rm-cat"', category)}
          ${sharingOptions('rm-share', sharing)}
        </div>
      </div>
      <div>
        <label class="type-label" style="color:var(--ink2)">${T('Scope')}</label>
        ${selectField({ id: 'rm-scope', label: T('Rule scope'), value: scope, options: scopeChoices(), className: 'w-full mt-1' })}
      </div>
      ${checkboxField({ id: 'rm-review', label: T('Always send to Review'), checked: review, className: 'type-label mt-2' })}
    </div>`;
}

/* Returns the rule values, or null after showing the reason it is not valid. */
function readRuleForm(root) {
  const pattern = root.querySelector('#rm-pattern').value.trim();
  if (pattern.length < 3) { showError(T('Pattern must contain at least 3 characters.')); return null; }
  const review = root.querySelector('#rm-review').checked;
  const category = root.querySelector('#rm-cat').value;
  if (!review && !category) { showError(T('Pick a category or enable “Always send to Review”.')); return null; }
  return {
    pattern,
    field: readSeg(root, 'rm-field'),
    category: category || null,
    sharing: readSeg(root.querySelector('#rm-share')) || 'shared',
    scope: root.querySelector('#rm-scope').value,
    review,
    note: root.querySelector('#rm-modal-body').dataset.note || '',
  };
}

function openRuleModal(gi) {
  const [key, txns] = window._groups[gi];
  const t = txns[0];
  const isPaypal = t.counterparty?.toLowerCase().includes('paypal');
  const modal = openModal({
    title: T('Save rule'),
    body: ruleFormBody({
      pattern: isPaypal ? (t.purpose || '').split(' ')[0] : (t.counterparty || ''),
      field: isPaypal ? 'purpose' : 'counterparty',
      category: $(`#cat-${gi}`).value || '',
      sharing: readSeg($(`#share-${gi}`)) || 'shared',
      scope: defaultScope(txns),
    }),
    actions: `<md-text-button class="rm-cancel">${T('Cancel')}</md-text-button><md-filled-button class="rm-save">${T('Save rule')}</md-filled-button>`,
    onMount: root => {
      root.querySelector('.rm-cancel').onclick = () => root._close();
      root.querySelector('.rm-save').onclick = async () => {
        const values = readRuleForm(root);
        if (!values) return;
        await api('/api/rule', {
          pattern: values.pattern, field: values.field, category: values.category,
          sharing: values.sharing, action: values.review ? 'review' : null,
          scope: values.scope, note: values.note || null,
        });
        root._close();
        const pill = $(`#rule-status-${gi}`);
        if (pill) { pill.textContent = T('Rule saved'); pill.className = 'chip chip-good'; }
      };
    },
  });
  setTimeout(() => modal.querySelector('#rm-pattern').focus(), 50);
}

/* Edit an existing rule from the Rules page. Sends every field, so clearing one
   (empty string) really clears it server-side. */
async function openRuleEdit(id) {
  const data = await api('/api/rules');
  const rule = data.rules.find(r => r.id === id);
  if (!rule) { showError(T('That rule no longer exists.')); render(); return; }
  const modal = openModal({
    title: T('Edit rule'),
    body: ruleFormBody({
      pattern: rule.match.contains,
      field: rule.match.field,
      category: rule.category || '',
      sharing: rule.sharing || 'shared',
      scope: rule.scope || 'family',
      review: rule.action === 'review',
      note: rule.note || '',
    }),
    actions: `<md-text-button class="rm-cancel">${T('Cancel')}</md-text-button><md-filled-button class="rm-save">${T('Save changes')}</md-filled-button>`,
    onMount: root => {
      root.querySelector('.rm-cancel').onclick = () => root._close();
      root.querySelector('.rm-save').onclick = async () => {
        const values = readRuleForm(root);
        if (!values) return;
        await api('/api/rule-update', {
          id, pattern: values.pattern, field: values.field,
          category: values.review ? '' : values.category, sharing: values.sharing,
          action: values.review ? 'review' : '', scope: values.scope, note: values.note,
        });
        root._close();
        state.rulesScope = values.scope;   // follow the rule if it changed profile
        state.ruleResult = T('Rule updated. Matching transactions were recategorized.');
        render();
      };
    },
  });
  setTimeout(() => modal.querySelector('#rm-pattern').focus(), 50);
}

function markOutOfScope(id, onSaved) {
  const eventTarget = window.event?.target;
  openModal({
    title: T('Mark as out of scope?'),
    body: `<div class="type-body-small" style="color:var(--ink2)">${T('This transaction will be excluded from all budget and expense calculations.')}</div>`,
    actions: `<md-text-button class="oos-cancel">${T('Cancel')}</md-text-button><md-filled-button class="oos-save">${T('Confirm')}</md-filled-button>`,
    onMount: root => {
      root.querySelector('.oos-cancel').onclick = () => root._close();
      root.querySelector('.oos-save').onclick = async () => {
        await api('/api/decision', { year: state.year, id, fields: { sharing: 'out-of-scope' } });
        root._close();
        if (onSaved) { onSaved(); return; }
        if (eventTarget) {
          const row = eventTarget.closest('.border-t') || eventTarget.closest('.flex');
          if (row) {
            row.classList.add('txn-readonly');
            row.insertAdjacentHTML('beforeend', `<span class="txn-readonly-msg type-caption font-italic pl-4">${T('(Out of scope)')}</span>`);
          }
        }
      };
    },
  });
}

function notePopover(id, note) {
  if (!note) {
    return `<md-icon style="font-size:19px; color:var(--ink2); cursor:pointer" onclick="openNote('${id}', '')" ${tooltip(T('Add note'))}>edit_note</md-icon>`;
  }
  return `<div class="note-popover-container">
    <md-icon style="font-size:19px; color:var(--primary); cursor:default">edit_note</md-icon>
    <div class="note-popover">
      <div class="note-popover-text">${esc(note)}</div>
      <div class="flex items-center justify-end" style="margin: 0 -8px;">
        <md-text-button data-note="${esc(note)}" onclick="openNote('${id}', this.dataset.note)">${T('Edit')}</md-text-button>
        <md-text-button style="--md-text-button-label-text-color: var(--bad);" onclick="deleteNote('${id}')">${T('Delete')}</md-text-button>
      </div>
    </div>
  </div>`;
}

window.deleteNote = function(id) {
  confirmAction({
    title: T('Delete note?'), danger: true, confirmLabel: T('Delete'), body: T('This removes the note from this transaction.'),
    onConfirm: async () => { await api('/api/decision', { year: state.year, id, fields: { note: null } }); render(); },
  });
};

function openSplit(id, total, onSaved) {
  const eventTarget = window.event?.target;
  let uid = 0;

  const txn = (window._txns || []).find(t => t.id === id) ||
              (window._groups || []).flatMap(([_, txns]) => txns).find(t => t.id === id);
  const existingSplits = txn?.splits || [];

  const getRowHtml = (amount = '', purpose = '', note = '', yc = false, tax = '', cat = '', share = 'shared') => {
    uid++;
    const rowId = `s-row-${uid}`;
    return `<div class="split-row card p-4 mb-3 border border-solid relative" style="border-color:var(--line)" id="${rowId}" data-note="${(note||'').replace(/"/g, '&quot;')}">
      <md-icon-button class="absolute top-2 right-2" onclick="this.closest('.split-row').remove(); window._updateSplitMath()" title="${T('Remove split part')}"><md-icon>close</md-icon></md-icon-button>
      <div class="flex flex-wrap gap-3 items-center mb-3">
        <div class="flex-1 min-w-[200px] flex items-end gap-2">
          <div class="flex-1">
            ${textField({ label: T('Purpose / description'), className: 's-purpose w-full', placeholder: T('e.g. Grocery'), value: purpose || '' })}
          </div>
          <md-icon class="cursor-pointer pb-2" style="color:var(--primary); font-size:24px;" onclick="openLocalNote(this)" ${tooltip(T('Add note'))}>${note ? 'edit_note' : 'note_add'}</md-icon>
        </div>
        <div class="flex-1 min-w-[200px]">
          ${textField({ label: T('Amount (EUR)'), type: 'number', className: 's-amt w-full', placeholder: '0.00', value: amount, attrs: 'step="0.01" min="0" oninput="window._updateSplitMath()"' })}
        </div>
      </div>
      <div class="flex flex-wrap gap-2 items-center">
        ${yearCostSwitch('s-yc-' + uid, yc)}
        ${taxField(tax)}
        ${sharingOptions('s-share-' + Date.now() + '-' + uid, share, 's-share')}
        ${catField('class="s-cat"', cat)}
      </div>
    </div>`;
  };
  
  const body = `<div class="flex gap-2 items-center justify-end mb-3">
        <md-filled-tonal-button id="s-add-remaining" disabled>${T('Add remaining')}</md-filled-tonal-button>
        <div id="split-math" class="type-body-small font-medium px-3 py-1 rounded" style="background:var(--md-sys-color-surface-container-high)">
          ${T('Remaining:')} <span id="split-remaining">${fmt(Math.abs(total))}</span>
        </div>
      </div>
      <div id="split-rows">
        ${existingSplits.length ? 
          existingSplits.map(s => getRowHtml(Math.abs(s.amount), s.purpose, s.note, s.year_cost, s.tax_bucket, s.category, s.sharing)).join('') : 
          getRowHtml() + getRowHtml()
        }
      </div>`;
  const m = openModal({
    title: T('Split {amount}', { amount: fmt(Math.abs(total)) }), body, width: '700px',
    actions: `<md-text-button id="s-add">${T('Add part')}</md-text-button><span class="flex-1"></span><md-text-button class="s-cancel">${T('Cancel')}</md-text-button><md-filled-button id="s-save" disabled>${T('Save split')}</md-filled-button>`,
  });
  m.querySelector('.s-cancel').onclick = () => m._close();

  window._updateSplitMath = () => {
    const rows = Array.from(m.querySelectorAll('.split-row'));
    const sum = rows.reduce((a, r) => a + cents(Math.abs(+(r.querySelector('.s-amt').value.replace(',', '.') || 0))), 0);
    const target = cents(Math.abs(total));
    const diff = target - sum;
    
    const remainingEl = m.querySelector('#split-remaining');
    const saveBtn = m.querySelector('#s-save');
    const remBtn = m.querySelector('#s-add-remaining');
    
    if (diff === 0) {
      remainingEl.innerText = '0.00 EUR';
      remainingEl.className = 'text-positive';
      saveBtn.removeAttribute('disabled');
      remBtn.setAttribute('disabled', 'true');
    } else if (diff > 0) {
      remainingEl.innerText = (diff / 100).toFixed(2) + ' EUR';
      remainingEl.className = '';
      saveBtn.setAttribute('disabled', 'true');
      remBtn.removeAttribute('disabled');
      remBtn.onclick = () => {
        m.querySelector('#split-rows').insertAdjacentHTML('beforeend', getRowHtml((diff / 100).toFixed(2)));
        window._updateSplitMath();
      };
    } else {
      remainingEl.innerText = `${T('Extra')} ${(Math.abs(diff) / 100).toFixed(2)} EUR`;
      remainingEl.className = 'text-negative';
      saveBtn.setAttribute('disabled', 'true');
      remBtn.setAttribute('disabled', 'true');
    }
  };

  m.querySelector('#s-add').onclick = () => {
    m.querySelector('#split-rows').insertAdjacentHTML('beforeend', getRowHtml());
    window._updateSplitMath();
  };
  // Run once on open. Reopening an existing split otherwise leaves Save disabled and
  // "Remaining" showing the full amount until an amount field is touched, so editing
  // only a category or sharing looked impossible.
  window._updateSplitMath();
  m.querySelector('#s-save').onclick = async () => {
    if (m.querySelector('#s-save').hasAttribute('disabled')) return;
    const parts = Array.from(m.querySelectorAll('.split-row')).map(r => ({
      amount: cents(Math.abs(+(r.querySelector('.s-amt').value.replace(',', '.')))) / 100 * Math.sign(total),
      purpose: r.querySelector('.s-purpose').value.trim() || null,
      note: r.dataset.note || null,
      year_cost: readYearCost(r),
      tax_bucket: readTax(r),
      category: r.querySelector('.s-cat').value || null,
      sharing: readSeg(r.querySelector('.s-share')) || 'shared',
    }));
    
    if (parts.some(p => !p.amount)) { showError(T('Fill in every split amount.')); return; }
    // Every part must be classified or explicitly excluded. The server enforces this
    // too, but its message names a part number without saying which control to touch.
    const unclassified = parts.findIndex(p => !p.category && p.sharing !== 'out-of-scope');
    if (unclassified !== -1) {
      showError(T('Part {n} needs a category — or set its sharing to “Out of scope” to leave it uncategorized.', { n: unclassified + 1 }));
      return;
    }

    // Fallback category
    const mainCategory = parts.find(p => p.category)?.category || null;
    
    await api('/api/decision', { year: state.year, id, fields: { splits: parts, category: mainCategory, sharing: 'shared' } });
    m._close();

    if (onSaved) { onSaved(parts); return; }
    if (eventTarget) {
      const row = eventTarget.closest('.border-b') || eventTarget.closest('.flex');
      if (row) {
        row.classList.add('txn-readonly');
        row.insertAdjacentHTML('beforeend', `<span class="txn-readonly-msg type-caption font-italic">${T('(Split into {n} parts)', { n: parts.length })}</span>`);
      }
    }
  };
}

/* ---------- settlement ---------- */
async function renderSettlement() {
  const annual = await api(`/api/settlement?year=${state.year}`);
  const [p1, p2] = state.meta.people;
  const cap = s => s[0].toUpperCase() + s.slice(1);
  const block = (s, title, explain = false, key = 'annual', log = null) => `<div class="card p-5 mb-4">
    <div class="flex items-center gap-2 mb-1">
      <h2 class="font-medium flex-1">${title}</h2>
      ${s.ratio_source === 'manual override' ? `<span class="chip chip-warn">${T('manual ratio')}</span>` : ''}
      <md-icon-button title="${T('Adjust ratio')}" onclick="openRatioModal('${key}')"><md-icon>tune</md-icon></md-icon-button>
    </div>
    <div class="type-caption mb-3" style="color:var(--ink2)">${T('ratio')} ${personLabel(p1)} ${(s.ratio[p1] * 100).toFixed(1)}% / ${personLabel(p2)} ${(s.ratio[p2] * 100).toFixed(1)}% — ${T(s.ratio_source)}</div>
    <table class="w-full type-body-small settle-table">
      <tr style="color:var(--ink2)"><td></td><td class="text-right">${personLabel(p1)}</td><td class="text-right">${personLabel(p2)}</td></tr>
      <tr><td class="py-1">${T('Salary income counted')}</td><td class="text-right">${fmt(s.ratio_income[p1])}</td><td class="text-right">${fmt(s.ratio_income[p2])}</td></tr>
      <tr><td class="py-1">${T('Paid for shared expenses')}</td><td class="text-right">${fmt(s.paid[p1])}</td><td class="text-right">${fmt(s.paid[p2])}</td></tr>
      <tr><td class="py-1">${T('Fair share ({total} total)', { total: fmt(s.total_shared_expenses) })}</td><td class="text-right">${fmt(s.fair_share[p1])}</td><td class="text-right">${fmt(s.fair_share[p2])}</td></tr>
      <tr class="font-medium"><td class="py-1">${T('Balance')}</td>
        <td class="text-right" style="color:${s.balances[p1] >= 0 ? 'var(--good)' : 'var(--bad)'}">${fmt(s.balances[p1])}</td>
        <td class="text-right" style="color:${s.balances[p2] >= 0 ? 'var(--good)' : 'var(--bad)'}">${fmt(s.balances[p2])}</td></tr>
    </table>
    ${s.transfer ? (() => {
      const rem = Math.round((s.transfer.amount - (log ? log.netPaid : 0)) * 100) / 100;
      const loggedNote = log && log.count ? `<div class="type-caption mt-1" style="opacity:.85">${T('{n} transfers logged · {total} total · target {target}', { n: log.count, total: fmt(log.total), target: fmt(s.transfer.amount) })}</div>` : '';
      return rem <= 0.005
        ? `<div class="mt-4 p-4 type-title chip-good" style="border-radius:12px">✓ ${T('Settled up')}${log && log.count ? ` — ${T('{total} logged in {n} transfers', { total: fmt(log.total), n: log.count })}` : ''}.</div>`
        : `<div class="mt-4 p-4 type-title chip-primary" style="border-radius:12px">💸 ${T('{from} transfers {amount} to {to} to equalize.', { from: `<b>${personLabel(s.transfer.from)}</b>`, amount: `<b>${fmt(rem)}</b>`, to: `<b>${personLabel(s.transfer.to)}</b>` })}${loggedNote}</div>`;
    })() : `<div class="mt-4 p-4 type-title chip-good" style="border-radius:12px">✓ ${T('Balanced — no transfer needed.')}</div>`}
    ${explain ? `<div class="mt-3 pt-3 border-t type-body-small" style="border-color:var(--line);color:var(--ink2);line-height:1.55">
      <b style="color:var(--ink)">${T('How this is calculated')}</b><br>
      ${T("1. Ratio = each person's salary ÷ the couple's total salary ({p1n} {p1v} + {p2n} {p2v} = {sum}). Only salary counts toward the ratio (couple salary splits 50/50); personal, out-of-scope, and internal transfers never count.", { p1n: personLabel(p1), p1v: fmt(s.ratio_income[p1]), p2n: personLabel(p2), p2v: fmt(s.ratio_income[p2]), sum: fmt(s.ratio_income[p1] + s.ratio_income[p2]) })}<br>
      ${T('2. Fair share = that ratio × the total shared expenses ({total}): {p1n} {p1pct}% = {p1fs}, {p2n} {p2pct}% = {p2fs}.', { total: fmt(s.total_shared_expenses), p1n: personLabel(p1), p1pct: (s.ratio[p1] * 100).toFixed(1), p1fs: fmt(s.fair_share[p1]), p2n: personLabel(p2), p2pct: (s.ratio[p2] * 100).toFixed(1), p2fs: fmt(s.fair_share[p2]) })}<br>
      ${T('3. Balance = what each actually paid for shared expenses − their fair share. A negative balance means they paid less than their share, so they transfer that difference to the other to equalize.')}<br>
      <span class="type-caption">${T('Monthly figures are estimates using the same reference ratio; the annual number here is the binding true-up on actual salary.')}</span></div>` : ''}
  </div>`;
  // all monthly estimates at once (no dropdown)
  const months = await Promise.all(MONTHS.map((_, i) => api(`/api/settlement?year=${state.year}&month=${i + 1}`)));
  // keep each period's ratio so the adjust modal can preset the slider
  state.settleByKey = { annual, ...Object.fromEntries(months.map((s, i) => [String(i + 1), s])) };
  const { transfers } = await api(`/api/settlement-transfers?year=${state.year}`);

  const net = (a, bb) => transfers.filter(t => t.sender === a && t.receiver === bb).reduce((x, t) => x + t.amount, 0);
  const tgtA = annual.transfer;
  const annualLog = {
    netPaid: tgtA ? net(tgtA.from, tgtA.to) - net(tgtA.to, tgtA.from) : 0,
    total: transfers.reduce((x, t) => x + t.amount, 0),
    count: transfers.length,
  };
  const settleLog = () => {
    const tgt = annual.transfer;  // {from, to, amount} or null
    let status;
    if (tgt) {
      const paid = net(tgt.from, tgt.to) - net(tgt.to, tgt.from);
      const rem = Math.round((tgt.amount - paid) * 100) / 100;
      if (rem > 0.005) status = `${T('Remaining:')} <b style="color:var(--bad)">${fmt(rem)}</b> — <b>${personLabel(tgt.from)}</b> → <b>${personLabel(tgt.to)}</b> <span class="type-caption" style="color:var(--ink2)">${T('(target {target}, {paid} logged)', { target: fmt(tgt.amount), paid: fmt(paid) })}</span>`;
      else if (rem < -0.005) status = `<b style="color:var(--good)">✓ ${T('Settled')}</b> — ${T('{name} was overpaid by {amount}', { name: personLabel(tgt.to), amount: `<b>${fmt(-rem)}</b>` })}`;
      else status = `<b style="color:var(--good)">✓ ${T('Settled up')}</b> <span class="type-caption" style="color:var(--ink2)">${T('({paid} of {target})', { paid: fmt(paid), target: fmt(tgt.amount) })}</span>`;
    } else {
      status = `<b style="color:var(--good)">✓ ${T('Balanced')}</b> — ${T('no transfer needed; anything logged is extra.')}`;
    }
    const dir = (a, bb) => [`${a}>${bb}`, `${personLabelRaw(a)} → ${personLabelRaw(bb)}`];
    const dirDefault = tgt ? `${tgt.from}>${tgt.to}` : `${p1}>${p2}`;
    return `<div class="card p-5 mb-4">
      <h2 class="font-medium mb-1">${T('Settle up — logged transfers')}</h2>
      <div class="type-body-small mb-3" style="color:var(--ink2)">${T('Record the real money you move between each other; it nets against the amount owed above.')}</div>
      <div class="p-4 mb-4 type-title chip-neutral" style="border-radius:12px">${status}</div>
      <div class="flex gap-2 items-end flex-wrap mb-4" id="xfer-add">
        ${textField({ label: T('Amount (€)'), type: 'number', className: 'xf-amt w-32', attrs: 'step="0.01" min="0"' })}
        ${selectField({ id: 'xf-dir', label: T('Direction'), value: dirDefault, options: [dir(p1, p2), dir(p2, p1)] })}
        ${textField({ label: T('Note'), className: 'xf-note w-64', placeholder: T('e.g. bank transfer') })}
        <md-filled-button class="btn-lg" onclick="addSettlementTransfer()"><md-icon slot="icon">add</md-icon>${T('Add transfer')}</md-filled-button>
      </div>
      ${transfers.length ? `<div style="overflow-x:auto"><table class="w-full type-body-small settle-table">
        <tr style="color:var(--ink2)"><td class="py-1">${T('Date')}</td><td>${T('From → To')}</td><td class="text-right">${T('Amount')}</td><td style="padding-left:24px">${T('Note')}</td><td></td></tr>
        ${[...transfers].reverse().map(t => `<tr>
          <td class="py-1">${fmtDate(t.date, true)}</td>
          <td>${personLabel(t.sender)} → ${personLabel(t.receiver)}</td>
          <td class="text-right font-medium">${fmt(t.amount)}</td>
          <td class="truncate" style="max-width:320px;padding-left:24px;color:var(--ink2)">${esc(t.note || '')}</td>
          <td class="text-right"><md-icon-button title="${T('Delete')}" onclick="confirmDeleteTransfer('${t.id}', ${t.amount}, '${t.sender}', '${t.receiver}')"><md-icon>delete</md-icon></md-icon-button></td>
        </tr>`).join('')}
      </table></div>` : `<div class="type-body-small" style="color:var(--ink2)">${T('No transfers logged yet.')}</div>`}
    </div>`;
  };

  // chart 1: shared expenses paid per person per month
  const paidMax = Math.max(1, ...months.map(s => Math.max(s.paid[p1] || 0, s.paid[p2] || 0)));
  const paidBars = months.map((s, i) => `
    <div class="flex flex-col items-center justify-end gap-1" style="height:180px">
      <div class="flex items-end" style="height:150px">
        <div class="mbar income" style="height:${150 * (s.paid[p1] || 0) / paidMax}px" ${tooltip(T('{month} — {name} paid {amount}', { month: T(MONTHS[i]), name: personLabel(p1), amount: fmt(s.paid[p1]) }))}></div>
        <div class="mbar expense" style="height:${150 * (s.paid[p2] || 0) / paidMax}px" ${tooltip(T('{month} — {name} paid {amount}', { month: T(MONTHS[i]), name: personLabel(p2), amount: fmt(s.paid[p2]) }))}></div>
      </div>
      <div class="type-caption" style="color:var(--ink2)">${T(MONTHS[i])}</div></div>`).join('');

  // chart 2: annual paid vs fair share per person (over/under paid on the year)
  const cmpMax = Math.max(1, annual.paid[p1], annual.paid[p2], annual.fair_share[p1], annual.fair_share[p2]);
  const cmpRow = (label, val, color) => `<div class="barrow flex items-center gap-3 py-1" ${tooltip(`${label}: ${fmt(val)}`)}>
    <div class="w-40 type-body-small text-right truncate" style="color:var(--ink2)">${label}</div>
    <div class="flex-1"><div class="bar" style="width:${100 * val / cmpMax}%; background:${color}"></div></div>
    <div class="w-24 type-body-small text-right">${fmt(val)}</div></div>`;

  window.openRatioModal = key => {
    const [q1, q2] = state.meta.people;
    const cur = (state.settleByKey && state.settleByKey[key]) || annual;
    const isManual = cur.ratio_source === 'manual override';
    const start = Math.round((cur.ratio[q1] || 0.5) * 100);
    const label = key === 'annual' ? T('the whole year {year}', { year: state.year }) : `${T(MONTHS[+key - 1])} ${state.year}`;
    openModal({
      title: T('Adjust settlement ratio'), width: '460px',
      body: `<div class="type-body-small mb-4" style="color:var(--ink2)">${T('Set the income split used to compute fair shares for {label}. This overrides the salary-based ratio.', { label: `<b>${label}</b>` })}</div>
        ${ratioSlider('ratio-range', start, { min: 0, max: 100 })}
        <div class="type-caption mt-3" style="color:var(--ink2)">${T('Drag to split. Reset to go back to the automatic salary-proportional ratio.')}</div>`,
      actions: `<md-text-button class="ratio-reset" ${isManual ? '' : 'disabled'}>${T('Reset to salary-based')}</md-text-button>
        <span class="flex-1"></span><md-text-button class="ratio-cancel">${T('Cancel')}</md-text-button><md-filled-button class="ratio-save">${T('Save')}</md-filled-button>`,
      onMount: root => {
        const range = root.querySelector('#ratio-range');
        wireRatioSlider('ratio-range');
        root.querySelector('.ratio-cancel').onclick = () => root._close();
        root.querySelector('.ratio-reset').onclick = async () => { await api('/api/ratio-override', { year: state.year, key, ratio: null }); root._close(); renderSettlement(); };
        root.querySelector('.ratio-save').onclick = async () => {
          const v = +range.value / 100;
          await api('/api/ratio-override', { year: state.year, key, ratio: { [q1]: v, [q2]: 1 - v } });
          root._close(); renderSettlement();
        };
      },
    });
  };

  $('#main').innerHTML =
    block(annual, T('Annual settlement {year} (binding — salary-proportional)', { year: state.year }), true, 'annual', annualLog) +
    settleLog() +
    `<h2 class="font-medium mb-3">${T('Monthly estimates')}</h2>
    <div class="grid3">${months.map((s, i) => block(s, `${String(i + 1).padStart(2, '0')} ${T(MONTHS[i])}`, false, String(i + 1))).join('')}</div>
    <h2 class="font-medium mb-3 mt-2">${T('Insights')}</h2>
    <div class="card p-5 mb-4">
      <div class="flex items-center gap-4 mb-3">
        <h2 class="font-medium">${T('Shared expenses paid per month')}</h2>
        <span class="flex items-center gap-1 type-label" style="color:var(--ink2)"><span class="swatch income"></span>${personLabel(p1)}</span>
        <span class="flex items-center gap-1 type-label" style="color:var(--ink2)"><span class="swatch expense"></span>${personLabel(p2)}</span>
      </div>
      <div class="flex justify-between px-2">${paidBars}</div>
    </div>
    <div class="card p-5 mb-4">
      <h2 class="font-medium mb-3">${T('Annual: paid vs fair share')}</h2>
      ${cmpRow(personLabel(p1) + ' — ' + T('paid'), annual.paid[p1], 'var(--chart-1)')}
      ${cmpRow(personLabel(p1) + ' — ' + T('fair share'), annual.fair_share[p1], 'var(--chart-2)')}
      <div class="my-2"></div>
      ${cmpRow(personLabel(p2) + ' — ' + T('paid'), annual.paid[p2], 'var(--chart-1)')}
      ${cmpRow(personLabel(p2) + ' — ' + T('fair share'), annual.fair_share[p2], 'var(--chart-2)')}
    </div>`;
  attachTooltips();
}

async function addSettlementTransfer() {
  const f = $('#xfer-add');
  const amount = parseFloat(f.querySelector('.xf-amt').value);
  if (!(amount > 0)) { showError(T('Enter a positive amount.')); return; }
  const [sender, receiver] = $('#xf-dir').value.split('>');
  const note = f.querySelector('.xf-note').value;
  await api('/api/settlement-transfers', { year: state.year, sender, receiver, amount, note });
  renderSettlement();
}
function confirmDeleteTransfer(id, amount, sender, receiver) {
  const cap = w => w[0].toUpperCase() + w.slice(1);
  confirmAction({
    title: T('Delete transfer?'),
    body: T('Remove the logged transfer of {amount} — {from} → {to}? This only deletes the log entry, not any real money.', { amount: `<b>${fmt(amount)}</b>`, from: personLabel(sender), to: personLabel(receiver) }),
    confirmLabel: T('Delete'), danger: true,
    onConfirm: async () => { await api('/api/settlement-transfer-delete', { year: state.year, id }); renderSettlement(); },
  });
}

/* ---------- tax ---------- */
function taxItemStatus(item) {
  if (!item.confirmed) return `<span class="chip chip-warn">${T('candidate')}</span>`;
  if (item.ready) return `<span class="chip chip-good">${T('ready')}</span>`;
  return `<span class="chip chip-bad">${T('missing evidence')}</span>`;
}
function taxEvidenceStatus(item) {
  const flag = (ok, icon, label) => `<span class="flex items-center gap-1 type-caption" style="color:${ok ? 'var(--good)' : 'var(--bad)'}" ${tooltip(label)}><md-icon>${ok ? 'check_circle' : icon}</md-icon>${esc(label)}</span>`;
  return `<div class="flex items-center gap-3 flex-wrap mt-1">
    ${flag(item.has_receipt, 'attach_file', item.has_receipt ? T('Attachment ready') : T('Attachment missing'))}
    ${flag(item.payment_proof, 'payments', item.payment_proof ? T('Bank payment recorded') : T('Payment proof missing'))}
    ${flag(item.owner_confirmed, 'person', item.owner_confirmed ? T('Claimed by {name}', { name: personLabelRaw(item.tax_owner) }) : T('Tax owner not confirmed'))}
  </div>`;
}
function taxBucketGuide(bucket, reportBucket) {
  const owners = reportBucket?.owners || {};
  const entryCount = Object.values(owners).reduce((n, owner) => n + owner.items.length, 0);
  const candidateCount = reportBucket?.candidate_count || 0;
  const confirmedCount = reportBucket?.confirmed_count || 0;
  const readyCount = reportBucket?.ready_count || 0;
  const examples = bucket.examples || [];
  const mapped = bucket.category_map || [];
  const entries = Object.entries(owners).map(([owner, data]) => `
    <div class="mt-4">
      <div class="type-body-small font-medium mb-1">${T('Claimed by {name} — confirmed {amount}', { name: esc(personLabelRaw(owner)), amount: fmt(-data.confirmed_total) })}</div>
      <div class="space-y-05">${data.items.map(item => `
        <div class="py-2 border-t" style="border-color:var(--line)">
          <div class="flex items-center gap-3 type-body-small">
            <span class="w-24" style="color:var(--ink2)">${fmtDate(item.date, true)}</span>
            <span class="flex-1 truncate">${esc(item.counterparty || item.purpose)}</span>
            ${taxItemStatus(item)}
            <span class="w-28 text-right">${fmt(item.amount)}</span>
            <md-icon-button class="txn-edit" onclick="openTaxTransaction('${esc(item.id)}')" ${tooltip(T('Open transaction'))}><md-icon>open_in_new</md-icon></md-icon-button>
          </div>
          <div class="px-4">${taxEvidenceStatus(item)}</div>
        </div>`).join('')}</div>
    </div>`).join('');

  const body = `<div class="type-body-small mb-3" style="color:var(--ink2)">${esc(taxBucketSummary(bucket))}</div>
    <div class="type-body mb-4"><b>${T('Rule:')}</b> ${esc(bucket.rule || taxBucketSummary(bucket))}</div>
    <div class="grid2 gap-4">
      <div>
        <div class="type-label mb-2">${T('Examples you can add')}</div>
        <div class="space-y-1">${examples.map(example => `<div class="flex items-start gap-2 type-body-small"><md-icon style="color:var(--good)">check</md-icon><span>${esc(example)}</span></div>`).join('') || `<div class="type-body-small" style="color:var(--ink2)">${T('No examples configured.')}</div>`}</div>
      </div>
      <div>
        <div class="type-label mb-2">${T('What to keep')}</div>
        <div class="type-body-small">${esc(bucket.evidence || T('Keep the invoice and payment evidence for review.'))}</div>
        <div class="type-caption mt-3" style="color:var(--ink2)"><b>${T('Reference:')}</b> ${esc(bucket.legal_reference || T('Review with a tax adviser'))}</div>
      </div>
    </div>
    ${mapped.length ? `<div class="type-caption mt-4" style="color:var(--ink2)"><b>${T('Automatically tagged from:')}</b> ${mapped.map(catName).map(esc).join(' · ')}</div>` : ''}
    ${entries || `<div class="type-body-small mt-4" style="color:var(--ink2)">${T('No transactions tagged in this category.')}</div>`}`;
  return accordion({
    cls: 'card mb-3 tax-bucket-acc', open: false,
    headerHtml: `<md-icon style="color:var(--primary)">receipt_long</md-icon><span class="font-medium">${esc(bucket.name)}</span>
      <span class="flex-1"></span>
      ${candidateCount ? `<span class="chip chip-warn">${T('{n} candidates', { n: candidateCount })}</span>` : ''}
      ${confirmedCount ? `<span class="chip chip-primary">${T('{n} confirmed', { n: confirmedCount })}</span>` : ''}
      ${readyCount ? `<span class="chip chip-good">${T('{n} ready', { n: readyCount })}</span>` : ''}
      ${entryCount ? `<span class="type-body-small font-medium">${fmt(-(reportBucket?.confirmed_total || 0))}</span>` : `<span class="chip chip-neutral">${T('0 tagged')}</span>`}`,
    bodyHtml: `<div class="p-5">${body}</div>`,
  });
}

async function openTaxTransaction(id) {
  const { items } = await cachedYearData('transactions', `/api/transactions?year=${state.year}`);
  const txn = items.find(item => item.id === id);
  if (!txn) { showError(T('Transaction not found in this year.')); return; }
  state.taxEditingTxn = txn;
  openEditTransaction(txn);
}

async function renderTax() {
  state.taxView = state.taxView || 'guide';
  state.taxFilters = state.taxFilters || { owner: '', bucket: '', status: '', from: '', to: '', q: '' };
  const { report } = await api(`/api/tax?year=${state.year}`);
  const reportBySlug = new Map(report.map(bucket => [bucket.bucket, bucket]));
  state.taxReport = report;
  state.taxReportBySlug = reportBySlug;
  const totals = report.reduce((sum, bucket) => ({
    candidates: sum.candidates + bucket.candidate_count,
    confirmed: sum.confirmed + bucket.confirmed_count,
    ready: sum.ready + bucket.ready_count,
    missing: sum.missing + bucket.missing_evidence_count,
    amount: sum.amount - bucket.confirmed_total,
  }), { candidates: 0, confirmed: 0, ready: 0, missing: 0, amount: 0 });
  const summaryRows = state.meta.tax_buckets.map(bucket => {
    const data = reportBySlug.get(bucket.slug);
    return `<tr>
      <td class="py-2">${esc(bucket.name)}</td>
      <td class="text-right">${data?.candidate_count || 0}</td>
      <td class="text-right">${data?.confirmed_count || 0}</td>
      <td class="text-right">${data?.ready_count || 0}</td>
      <td class="text-right">${data?.missing_evidence_count || 0}</td>
      <td class="text-right font-medium">${fmt(-(data?.confirmed_total || 0))}</td>
    </tr>`;
  }).join('');
  $('#main').innerHTML = `<div class="card p-5 mb-4">
    <div class="flex items-center gap-4 flex-wrap">
      <div class="flex-1">
        <h2 class="mb-1">${T('Tax evidence guide {year}', { year: state.year })}</h2>
        <div class="type-body-small" style="color:var(--ink2)">${T('All available German tax categories are shown below. Tag candidate transactions here for your evidence pack; final eligibility depends on your circumstances and should be checked before filing.')}</div>
      </div>
      <md-outlined-button href="/api/tax-export?year=${state.year}">${T('Download Excel (Steuerberater)')}</md-outlined-button>
    </div>
    <div class="flex items-center gap-2 flex-wrap mt-4">
      <span class="chip chip-warn">${T('{n} candidates', { n: totals.candidates })}</span>
      <span class="chip chip-primary">${T('{n} confirmed', { n: totals.confirmed })}</span>
      <span class="chip chip-good">${T('{n} ready', { n: totals.ready })}</span>
      <span class="chip ${totals.missing ? 'chip-bad' : 'chip-neutral'}">${T('{n} missing evidence', { n: totals.missing })}</span>
      <span class="type-title">${T('Confirmed total {amount}', { amount: fmt(totals.amount) })}</span>
    </div>
    <div class="scroll-x"><table class="w-full type-body-small settle-table mt-4 tax-summary">
      <tr style="color:var(--ink2)"><td>${T('Category')}</td><td class="text-right">${T('Candidates')}</td><td class="text-right">${T('Confirmed')}</td><td class="text-right">${T('Ready')}</td><td class="text-right">${T('Missing')}</td><td class="text-right">${T('Confirmed total')}</td></tr>
      ${summaryRows}
    </table></div>
  </div>
  <div class="tax-toggle mb-3">${segControl('tax-view', [['guide', T('By category')], ['all', T('All entries')]], state.taxView)}</div>
  <div id="tax-body"></div>`;
  $('#tax-view')?.addEventListener('click', event => { const btn = event.target.closest('.seg-option'); if (btn) setTaxView(btn.dataset.value); });
  renderTaxBody();
  attachTooltips();
}

function setTaxView(v) { if (v && v !== state.taxView) { state.taxView = v; renderTaxBody(); } }

function renderTaxBody() {
  const host = $('#tax-body'); if (!host) return;
  if (state.taxView === 'all') {
    host.innerHTML = taxAllHtml(state.taxReport);
    wireTaxAllFilters();
    renderTaxAllTable();
  } else {
    host.innerHTML = `<div class="type-label mb-2" style="color:var(--ink2)">${T('Category details')}</div>`
      + state.meta.tax_buckets.map(bucket => taxBucketGuide(bucket, state.taxReportBySlug.get(bucket.slug))).join('');
  }
  attachTooltips();
}

/* Flatten the per-bucket/per-owner report into one row per tagged transaction. */
function taxAllRows(report) {
  const rows = [];
  (report || []).forEach(b => Object.entries(b.owners).forEach(([owner, data]) =>
    data.items.forEach(item => rows.push({ ...item, bucket: b.bucket, bucketName: b.name, owner }))));
  return rows;
}
function taxMatchStatus(item, key) {
  if (key === 'candidate') return !item.confirmed;
  if (key === 'confirmed') return item.confirmed;
  if (key === 'ready') return item.ready;
  if (key === 'missing') return item.confirmed && !item.ready;
  return true;
}

/* "All entries": one filterable table over every tax-tagged transaction of the year. */
function taxAllHtml(report) {
  const rows = taxAllRows(report);
  const owners = [...new Set(rows.map(r => r.owner))].sort();
  const ownerOpts = [['', T('All people')], ...owners.map(o => [o, personLabelRaw(o)])];
  const bucketOpts = [['', T('All categories')], ...report.filter(b => Object.keys(b.owners).length).map(b => [b.bucket, b.name])];
  const statusOpts = [['', T('Any status')], ['candidate', T('candidate')], ['confirmed', T('confirmed')], ['ready', T('ready')], ['missing', T('missing evidence')]];
  const f = state.taxFilters;
  return `<div class="card p-4 mb-4">
    <div class="tax-filters flex gap-3 flex-wrap items-end">
      ${selectField({ id: 'tax-f-owner', label: T('Person'), value: f.owner, options: ownerOpts, className: 'w-40' })}
      ${selectField({ id: 'tax-f-bucket', label: T('Category'), value: f.bucket, options: bucketOpts, className: 'w-56' })}
      ${selectField({ id: 'tax-f-status', label: T('Status'), value: f.status, options: statusOpts, className: 'w-44' })}
      ${textField({ id: 'tax-f-from', label: T('From'), type: 'date', value: f.from, className: 'w-40' })}
      ${textField({ id: 'tax-f-to', label: T('To'), type: 'date', value: f.to, className: 'w-40' })}
      ${textField({ id: 'tax-f-q', label: T('Search'), value: f.q, className: 'w-56', placeholder: T('Merchant or note') })}
      <md-text-button onclick="clearTaxFilters()"><md-icon slot="icon">filter_alt_off</md-icon>${T('Clear filters')}</md-text-button>
    </div>
  </div>
  <div class="scroll-x"><table class="w-full type-body-small settle-table tax-all-table">
    <thead><tr style="color:var(--ink2)">
      <td class="py-1">${T('Date')}</td><td>${T('Category')}</td><td>${T('Claimed by')}</td><td>${T('Merchant')}</td><td>${T('Status')}</td><td class="text-right">${T('Amount')}</td><td></td>
    </tr></thead>
    <tbody id="tax-all-tbody"></tbody>
    <tfoot id="tax-all-foot"></tfoot>
  </table></div>`;
}
function readTaxFilters() {
  const g = id => ($('#' + id)?.value || '').trim();
  state.taxFilters = { owner: g('tax-f-owner'), bucket: g('tax-f-bucket'), status: g('tax-f-status'), from: g('tax-f-from'), to: g('tax-f-to'), q: g('tax-f-q') };
  return state.taxFilters;
}
function wireTaxAllFilters() {
  ['tax-f-owner', 'tax-f-bucket', 'tax-f-status'].forEach(id => $('#' + id)?.addEventListener('change', renderTaxAllTable));
  ['tax-f-from', 'tax-f-to', 'tax-f-q'].forEach(id => $('#' + id)?.addEventListener('input', renderTaxAllTable));
}
function clearTaxFilters() { state.taxFilters = { owner: '', bucket: '', status: '', from: '', to: '', q: '' }; renderTaxBody(); }
function renderTaxAllTable() {
  const tb = $('#tax-all-tbody'); if (!tb) return;
  const f = readTaxFilters();
  let rows = taxAllRows(state.taxReport);
  if (f.owner) rows = rows.filter(r => r.owner === f.owner);
  if (f.bucket) rows = rows.filter(r => r.bucket === f.bucket);
  if (f.status) rows = rows.filter(r => taxMatchStatus(r, f.status));
  if (f.from) rows = rows.filter(r => r.date >= f.from);
  if (f.to) rows = rows.filter(r => r.date <= f.to);
  if (f.q) { const q = f.q.toLowerCase(); rows = rows.filter(r => (r.counterparty || '').toLowerCase().includes(q) || (r.purpose || '').toLowerCase().includes(q)); }
  rows.sort((a, b) => b.date.localeCompare(a.date));
  tb.innerHTML = rows.map(r => `<tr>
    <td class="py-2 w-24" style="color:var(--ink2)">${fmtDate(r.date, true)}</td>
    <td>${esc(r.bucketName)}</td>
    <td>${esc(personLabelRaw(r.owner))}</td>
    <td class="truncate" style="max-width:280px" ${tooltip(r.counterparty || r.purpose || '')}>${esc(r.counterparty || r.purpose || '')}</td>
    <td>${taxItemStatus(r)}</td>
    <td class="text-right">${fmt(r.amount)}</td>
    <td><md-icon-button class="txn-edit" onclick="openTaxTransaction('${esc(r.id)}')" ${tooltip(T('Open transaction'))}><md-icon>open_in_new</md-icon></md-icon-button></td>
  </tr>`).join('') || `<tr><td colspan="7" class="py-4" style="color:var(--ink2)">${T('No entries match these filters.')}</td></tr>`;
  const total = rows.reduce((a, r) => a + r.amount, 0);
  $('#tax-all-foot').innerHTML = `<tr class="border-t"><td colspan="5" class="py-2 font-medium">${T('{n} entries', { n: rows.length })}</td><td class="text-right font-medium">${fmt(total)}</td><td></td></tr>`;
  attachTooltips();
}

/* ---------- add entry (cash) ---------- */
async function renderAdd() {
  const cashAccounts = state.meta.accounts.filter(a => a.type === 'cash');
  if (!cashAccounts.length) {
    $('#main').innerHTML = `<div class="card p-6 max-w-lg">
      <h2 class="font-medium mb-2">${T('Add cash transaction')}</h2>
      <div class="type-body-small mb-4" style="color:var(--ink2)">${T("Manual entries need a {cash} account — a wallet with no bank export, so it can't double-count. You don't have one yet.", { cash: `<b>${T('cash')}</b>` })}</div>
      <md-filled-button onclick="location.hash='accounts'"><md-icon slot="icon">account_balance</md-icon>${T('Add a cash account')}</md-filled-button>
    </div>`;
    return;
  }
  $('#main').innerHTML = `<div class="card p-6 max-w-lg">
    <h2 class="font-medium mb-4">${T('Add cash transaction')}</h2>
    <div class="space-y-3">
      ${textField({ id: 'c-date', label: T('Date'), type: 'date', value: localDateISO() })}
      ${selectField({ id: 'c-account', label: T('Who paid'), value: cashAccounts[0]?.id || '', options: cashAccounts.map(a => [a.id, personLabelRaw(a.owner), accountOwnLabel(a)]) })}
      <div class="flex gap-2 items-start">${textField({ id: 'c-amount', label: T('Amount'), supportingText: T('Use a negative value for an expense'), type: 'number', placeholder: '-12.50', attrs: 'step="0.01"' })}
        ${selectField({ id: 'c-currency', label: T('Currency'), value: 'EUR', options: currencyOptions() })}</div>
      ${textField({ id: 'c-desc', label: T('Description'), className: 'w-full', placeholder: T('Döner after gym') })}
      <div><label class="type-label" style="color:var(--ink2)">${T('Category (optional — else it goes to Review)')}</label><br>${catField('id="c-cat"', '')}</div>
      <md-filled-button onclick="saveCash()">${T('Save')}</md-filled-button>
    </div></div>
    <div class="card p-5 mt-4">
      <h2 class="font-medium mb-3">${T('Manual cash entries — {year}', { year: state.year })}</h2>
      <div id="cash-entries" class="type-body-small" style="color:var(--ink2)">${T('Loading…')}</div>
    </div>`;
  loadCashEntries();
}

async function loadCashEntries() {
  const host = $('#cash-entries'); if (!host) return;
  const { items } = await api(`/api/transactions?year=${state.year}`);
  const cash = items.filter(t => t.source && t.source.file === 'cash.csv').sort((a, b) => b.date.localeCompare(a.date));
  state._cashById = Object.fromEntries(cash.map(t => [t.id, t]));
  if (!cash.length) { host.textContent = T('No manual cash entries this year yet.'); return; }
  const total = cash.reduce((a, t) => a + t.amount_eur, 0);
  host.innerHTML = `<div style="overflow-x:auto"><table class="w-full type-body-small settle-table">
    <tr style="color:var(--ink2)"><td class="py-1">${T('Date')}</td><td>${T('Who paid')}</td><td>${T('Description')}</td><td>${T('Category')}</td><td class="text-right">${T('Amount')}</td><td></td></tr>
    ${cash.map(t => `<tr>
      <td class="py-1">${fmtDate(t.date, true)}</td>
      <td>${esc(accountLabel(t.account))}</td>
      <td class="truncate" style="max-width:300px">${esc(t.counterparty || '')}</td>
      <td>${t.category ? catBadge(t.category) : `<span class="type-caption" style="color:var(--ink2)">${T('in review')}</span>`}</td>
      <td class="text-right font-medium" style="color:${t.amount_eur < 0 ? 'var(--bad)' : 'var(--good)'}"><span class="flex items-center justify-end gap-1">${fxBadge(t)}${fmt(t.amount_eur)}</span></td>
      <td class="text-right" style="white-space:nowrap">
        <md-icon-button title="${T('Edit entry')}" onclick="openCashEdit('${t.id}')"><md-icon>edit</md-icon></md-icon-button>
        <md-icon-button title="${T('Delete entry')}" onclick="confirmDeleteCash('${t.id}', ${t.amount_eur})"><md-icon>delete</md-icon></md-icon-button></td>
    </tr>`).join('')}
    <tr class="font-medium"><td class="py-1">${T('Total')}</td><td></td><td></td><td></td><td class="text-right">${fmt(total)}</td><td></td></tr>
  </table></div>`;
  attachTooltips();
}

function confirmDeleteCash(id, amount) {
  confirmAction({
    title: T('Delete cash entry?'),
    body: T('Remove this manual entry of {amount}? It is deleted from the ledger and from cash.csv.', { amount: `<b>${fmt(amount)}</b>` }),
    confirmLabel: T('Delete'), danger: true,
    onConfirm: async () => { await api('/api/cash-delete', { year: state.year, id }); loadCashEntries(); },
  });
}

function openCashEdit(id) {
  const t = (state._cashById || {})[id];
  if (!t) return;
  const cashAccounts = state.meta.accounts.filter(a => a.type === 'cash');
  const opts = currencyOptions();
  if (!opts.some(([c]) => c === t.currency)) opts.push([t.currency, t.currency]);
  openModal({
    title: T('Edit cash entry'), width: '540px',
    body: `<div class="space-y-3">
      ${textField({ id: 'e-date', label: T('Date'), type: 'date', value: t.date })}
      ${selectField({ id: 'e-account', label: T('Who paid'), value: t.account, options: cashAccounts.map(a => [a.id, personLabelRaw(a.owner), accountOwnLabel(a)]) })}
      <div class="flex gap-2 items-start">${textField({ id: 'e-amount', label: T('Amount'), type: 'number', value: t.amount_original, attrs: 'step="0.01"' })}
        ${selectField({ id: 'e-currency', label: T('Currency'), value: t.currency, options: opts })}</div>
      ${textField({ id: 'e-desc', label: T('Description'), className: 'w-full', value: t.counterparty || '' })}
      <div><label class="type-label" style="color:var(--ink2)">${T('Category (optional — else it goes to Review)')}</label><br>${catField('id="e-ccat"', t.category || '')}</div>
    </div>`,
    actions: `<md-text-button class="ce-cancel">${T('Cancel')}</md-text-button><md-filled-button class="ce-save">${T('Save')}</md-filled-button>`,
    onMount: root => {
      root.querySelector('.ce-cancel').onclick = () => root._close();
      root.querySelector('.ce-save').onclick = async () => {
        const amount = +root.querySelector('#e-amount').value;
        const account = root.querySelector('#e-account').value;
        const description = root.querySelector('#e-desc').value.trim();
        if (!account || !amount || !description) { showError(T('Amount, who paid, and description are required.')); return; }
        await api('/api/cash-edit', {
          year: state.year, id, date: root.querySelector('#e-date').value, account, amount,
          currency: root.querySelector('#e-currency').value, description, category: root.querySelector('#e-ccat').value || '',
        });
        root._close();
        loadCashEntries();
      };
    },
  });
}

async function saveCash() {
  const amount = +$('#c-amount').value;
  const account = $('#c-account').value;
  if (!account) { showError(T('Pick who paid.')); return; }
  if (!amount || !$('#c-desc').value) { showError(T('Amount and description are required.')); return; }
  const r = await api('/api/cash', {
    date: $('#c-date').value, account, amount,
    currency: $('#c-currency').value, description: $('#c-desc').value, category: $('#c-cat').value,
  });
  showMessage(r.result && r.result.includes('new') ? T('Saved and ingested — {result}.', { result: r.result }) : T('Saved (already recorded — no duplicate added).'));
  render();
}

/* ---------- feedback (separate from accountability data) ---------- */
function feedbackEntryHtml(entry) {
  const created = new Date(entry.created_at).toLocaleString('en-DE', { dateStyle: 'medium', timeStyle: 'short' });
  const attachment = entry.attachment
    ? `<md-outlined-button href="${esc(entry.attachment.url)}"><md-icon slot="icon">attachment</md-icon>${esc(entry.attachment.name)}</md-outlined-button>`
    : '';
  return `<div class="border-t py-3">
    <div class="flex items-start justify-between gap-3 flex-wrap">
      <div class="min-w-0 flex-1">
        <div class="type-title">${esc(entry.title)}</div>
        <div class="type-caption mt-1" style="color:var(--ink2)">${esc(created)}</div>
      </div>
      <div class="flex items-center gap-2">
        ${attachment}
        <md-icon-button class="feedback-delete" data-id="${esc(entry.id)}" title="${T('Delete feedback')}"><md-icon>delete</md-icon></md-icon-button>
      </div>
    </div>
    <div class="feedback-description type-body-small mt-3">${esc(entry.description)}</div>
  </div>`;
}

/* Colour + icon picker for one partner (Settings). Selections go into state.settingsPStyle
   and are saved with the rest of the settings; segInfo/personColor/personIcon read them back. */
function partnerStyleEditor(p) {
  const curColor = personColor(p).toLowerCase(), curIcon = personIcon(p);
  return `<div class="partner-style">
    <div class="flex items-center gap-2 mb-1">
      <md-icon class="pstyle-preview" style="color:${curColor};font-size:22px">${esc(curIcon)}</md-icon>
      <span class="type-label" style="color:var(--ink2)">${T('Colour &amp; icon')}</span>
    </div>
    <div class="flex flex-wrap gap-1 mb-2">
      ${MATERIAL_PALETTE.map(([name, hex]) => `<button type="button" class="swatch-pick ${hex.toLowerCase() === curColor ? 'selected' : ''}" title="${esc(name)}" style="background:${hex}" onclick="pickPartnerStyle(this,'${esc(p)}','color','${hex}')"></button>`).join('')}
    </div>
    <div class="flex flex-wrap gap-1">
      ${PERSON_ICONS.map(ic => `<button type="button" class="icon-pick ${ic === curIcon ? 'selected' : ''}" title="${ic}" onclick="pickPartnerStyle(this,'${esc(p)}','icon','${ic}')"><md-icon>${ic}</md-icon></button>`).join('')}
    </div>
  </div>`;
}
function pickPartnerStyle(el, p, kind, value) {
  state.settingsPStyle = state.settingsPStyle || {};
  state.settingsPStyle[p] = { ...(state.settingsPStyle[p] || {}), [kind]: value };
  state.meta.person_styles = { ...(state.meta.person_styles || {}), [p]: { ...(state.settingsPStyle[p]) } };  // reflect in the preview
  el.parentElement.querySelectorAll('.selected').forEach(x => x.classList.remove('selected'));
  el.classList.add('selected');
  const preview = el.closest('.partner-style')?.querySelector('.pstyle-preview');
  if (preview) { if (kind === 'color') preview.style.color = value; else preview.textContent = value; }
  refreshSharePreview();
  scheduleSettingsSave();
}
/* Same picker for the shared / together option (a single style, not per-person). */
function sharedStyleEditor() {
  const curColor = sharedColor().toLowerCase(), curIcon = sharedIcon('group');
  return `<div class="partner-style">
    <div class="flex items-center gap-2 mb-1">
      <md-icon class="pstyle-preview seg-icon" style="color:${curColor}">${esc(curIcon)}</md-icon>
      <span class="type-label" style="color:var(--ink2)">${T('Shared / together — colour &amp; icon')}</span>
    </div>
    <div class="flex flex-wrap gap-1 mb-2">
      ${MATERIAL_PALETTE.map(([name, hex]) => `<button type="button" class="swatch-pick ${hex.toLowerCase() === curColor ? 'selected' : ''}" title="${esc(name)}" style="background:${hex}" onclick="pickSharedStyle(this,'color','${hex}')"></button>`).join('')}
    </div>
    <div class="flex flex-wrap gap-1">
      ${SHARED_ICONS.map(ic => `<button type="button" class="icon-pick ${ic === curIcon ? 'selected' : ''}" title="${ic}" onclick="pickSharedStyle(this,'icon','${ic}')"><md-icon>${ic}</md-icon></button>`).join('')}
    </div>
  </div>`;
}
function pickSharedStyle(el, kind, value) {
  state.settingsSharedStyle = { ...(state.settingsSharedStyle || {}), [kind]: value };
  state.meta.shared_style = { ...(state.settingsSharedStyle) };  // reflect in the preview
  el.parentElement.querySelectorAll('.selected').forEach(x => x.classList.remove('selected'));
  el.classList.add('selected');
  const preview = el.closest('.partner-style')?.querySelector('.pstyle-preview');
  if (preview) { if (kind === 'color') preview.style.color = value; else preview.textContent = value; }
  refreshSharePreview();
  scheduleSettingsSave();
}

/* App-bar (header) icon + colour. A curated set of Material Symbols that suit a household /
   money app; colour reuses the shared palette. Selection lives in state.settingsBrandStyle. */
const HEADER_ICONS = ['savings', 'account_balance', 'account_balance_wallet', 'wallet', 'payments',
  'paid', 'euro', 'currency_exchange', 'credit_card', 'price_check', 'monitoring', 'trending_up',
  'insights', 'query_stats', 'pie_chart', 'home', 'cottage', 'house', 'apartment', 'home_work',
  'real_estate_agent', 'family_restroom', 'diversity_1', 'diversity_3', 'favorite', 'volunteer_activism',
  'redeem', 'card_giftcard', 'celebration', 'spa', 'self_improvement', 'pets', 'eco', 'park',
  'rocket_launch', 'star', 'bolt', 'shopping_cart', 'receipt_long', 'calculate', 'work', 'school',
  'flight', 'sailing', 'restaurant', 'local_cafe', 'directions_car', 'pedal_bike'];
const brandColor = () => ((state.settingsBrandStyle || {}).color || '').toLowerCase();
const brandIcon = () => (state.settingsBrandStyle || {}).icon || DEFAULT_BRAND_ICON;
function brandStyleEditor() {
  const curColor = brandColor(), curIcon = brandIcon();
  return `<div class="partner-style">
    <div class="flex items-center gap-2 mb-1">
      <md-icon class="pstyle-preview" style="color:${curColor || 'var(--primary)'};font-size:22px">${esc(curIcon)}</md-icon>
      <span class="type-label" style="color:var(--ink2)">${T('App icon (header) — colour &amp; icon')}</span>
    </div>
    <div class="flex flex-wrap gap-1 mb-2">
      ${MATERIAL_PALETTE.map(([name, hex]) => `<button type="button" class="swatch-pick ${hex.toLowerCase() === curColor ? 'selected' : ''}" title="${esc(name)}" style="background:${hex}" onclick="pickBrandStyle(this,'color','${hex}')"></button>`).join('')}
    </div>
    <div class="flex flex-wrap gap-1">
      ${HEADER_ICONS.map(ic => `<button type="button" class="icon-pick ${ic === curIcon ? 'selected' : ''}" title="${ic}" onclick="pickBrandStyle(this,'icon','${ic}')"><md-icon>${ic}</md-icon></button>`).join('')}
    </div>
  </div>`;
}
function pickBrandStyle(el, kind, value) {
  state.settingsBrandStyle = { ...(state.settingsBrandStyle || {}), [kind]: value };
  state.meta.brand_style = { ...(state.settingsBrandStyle) };
  el.parentElement.querySelectorAll('.selected').forEach(x => x.classList.remove('selected'));
  el.classList.add('selected');
  const preview = el.closest('.partner-style')?.querySelector('.pstyle-preview');
  if (preview) { if (kind === 'color') preview.style.color = value; else preview.textContent = value; }
  applyBrand(state.meta.brand_style);   // live-update the header
  scheduleSettingsSave();
}

/* Live preview of the exact sharing selector used across the app (Shared / each partner /
   Out of scope), so the colour, icon and name edits above are visible immediately. Picks
   mirror into state.meta, then this re-renders from the same component every screen uses. */
function sharePreviewBlock() {
  return `<div class="share-preview flex flex-col gap-1">
    <span class="type-label" style="color:var(--ink2)">${T('Preview — how these appear across the app')}</span>
    <div id="hh-share-preview">${sharingOptions('hh-share', 'shared')}</div>
  </div>`;
}
function refreshSharePreview() {
  const host = $('#hh-share-preview');
  if (host) host.innerHTML = sharingOptions('hh-share', 'shared');
}

/* Settings tab: edits the config.json knobs. Person SLUGS are immutable ids; only their
   display labels are editable here (same slug/label split the app uses for categories). */
/* Settings is a left-rail of sub-pages. Every field autosaves (debounced, optimistic,
   latest-wins); the save-status flag shows Saving / Saved / an error. state.settingsCfg is
   the single source of truth the payload is built from, so switching areas never loses a change. */
const SETTINGS_AREAS = [
  ['household', 'home', 'Household'],
  ['accounts', 'account_balance', 'Accounts'],
  ['balances', 'grid_on', 'Balances'],
  ['preferences', 'tune', 'Preferences'],
  ['accounting', 'calculate', 'Accounting'],
  ['data', 'inventory_2', 'Data'],
  ['security', 'lock', 'Security'],
];

/* `className` is for sections that must escape the readable-measure cap — a data grid needs
   the panel's full width, a form field does not. */
function settingsSection(title, desc, body, className = '') {
  return `<section class="settings-section flex flex-col gap-4 ${className}">
      <div><div class="type-title">${title}</div>${desc ? `<div class="type-body-small mt-1" style="color:var(--ink2)">${desc}</div>` : ''}</div>
      ${body}
    </section>`;
}

async function renderSettings() {
  if (!SETTINGS_AREAS.some(a => a[0] === state.settingsArea)) state.settingsArea = 'household';
  state.settingsSaveSeq = state.settingsSaveSeq || 0;
  const cfg = await api('/api/settings');
  const [p1, p2] = state.meta.people;
  state.settingsCfg = {
    person_labels: { [p1]: (cfg.person_labels || {})[p1] || personLabelRaw(p1), [p2]: (cfg.person_labels || {})[p2] || personLabelRaw(p2) },
    reference_ratio: cfg.reference_ratio || { [p1]: 0.5, [p2]: 0.5 },
    items_threshold_eur: cfg.items_threshold_eur ?? 50,
    transfer_match_window_days: cfg.transfer_match_window_days ?? 4,
    transfer_match_tolerance_cents: cfg.transfer_match_tolerance_cents ?? 200,
    currencies: (cfg.currencies || ['EUR']).map(c => String(c).toUpperCase()),
    household_name: cfg.household_name || '',
    language: cfg.language || 'en',
  };
  state.settingsPStyle = JSON.parse(JSON.stringify(cfg.person_styles || {}));   // edited by the pickers
  state.settingsSharedStyle = JSON.parse(JSON.stringify(cfg.shared_style || {}));
  state.settingsBrandStyle = JSON.parse(JSON.stringify(cfg.brand_style || {}));

  const rail = SETTINGS_AREAS.map(([id, icon, label]) =>
    `<button type="button" class="settings-rail-item ${id === state.settingsArea ? 'active' : ''}" onclick="setSettingsArea('${id}')"><md-icon>${icon}</md-icon>${T(label)}</button>`).join('');
  const areaTitle = T(SETTINGS_AREAS.find(a => a[0] === state.settingsArea)[2]);
  $('#main').innerHTML = `<div class="settings-shell">
    <nav class="settings-rail" aria-label="${T('Settings sections')}">${rail}</nav>
    <div class="settings-panel card p-8">
      <div class="settings-panel-head">
        <h2 class="type-headline">${areaTitle}</h2>
        <span id="save-status" class="save-status hidden"></span>
      </div>
      <div id="settings-area"></div>
    </div>
  </div>`;
  await fillSettingsArea();
}

function setSettingsArea(area) { state.settingsArea = area; renderSettings(); }

/* On-input/change hookup helper (Material fields dispatch 'input'; selects use 'change'). */
function onSettingsField(id, event, fn) {
  const el = $('#' + id); if (el) el.addEventListener(event, fn);
}

async function fillSettingsArea() {
  const host = $('#settings-area'); if (!host) return;
  const area = state.settingsArea;
  const [p1, p2] = state.meta.people;
  const c = state.settingsCfg;

  if (area === 'household') {
    host.innerHTML = settingsSection(
      T('Household &amp; partners'),
      T('The household name shows in the header and footer; partner names, colours and icons appear across the app.'), `
        ${textField({ id: 'set-household', label: T('Household name'), className: 'w-full', value: c.household_name, placeholder: 'Family Accountability', supportingText: T('Leave blank to use the default “Family Accountability”.') })}
        ${brandStyleEditor()}
        <div class="grid2 gap-6">
          <div class="flex flex-col gap-3">
            ${textField({ id: `set-label-${p1}`, label: T('Partner 1 name'), value: c.person_labels[p1] })}
            ${partnerStyleEditor(p1)}
          </div>
          <div class="flex flex-col gap-3">
            ${textField({ id: `set-label-${p2}`, label: T('Partner 2 name'), value: c.person_labels[p2] })}
            ${partnerStyleEditor(p2)}
          </div>
        </div>
        ${sharedStyleEditor()}
        ${sharePreviewBlock()}`);
    onSettingsField('set-household', 'input', e => {
      c.household_name = e.target.value;
      applyHouseholdName(c.household_name);   // optimistic: header + footer + title
      scheduleSettingsSave();
    });
    for (const p of [p1, p2]) onSettingsField(`set-label-${p}`, 'input', e => {
      c.person_labels[p] = e.target.value;
      state.meta.person_labels = { ...(state.meta.person_labels || {}), [p]: e.target.value };  // reflect in the preview
      refreshSharePreview();
      scheduleSettingsSave();
    });

  } else if (area === 'accounts') {
    host.innerHTML = `<div class="type-body-small py-6" style="color:var(--ink2)">${T('Loading…')}</div>`;
    host.innerHTML = await accountsAreaHtml();
    attachTooltips();   // wire the [data-tip] tooltips in the account rows (help, delete, in-use)

  } else if (area === 'balances') {
    host.innerHTML = `<div class="type-body-small py-6" style="color:var(--ink2)">${T('Loading…')}</div>`;
    await fillBalancesArea();

  } else if (area === 'preferences') {
    const langOptions = I18N.codes().map(code =>
      `<md-select-option value="${esc(code)}" ${code === c.language ? 'selected' : ''}><div slot="headline">${esc(I18N.names[code])}</div></md-select-option>`).join('');
    host.innerHTML = `<div class="flex flex-col gap-6">
      ${settingsSection(T('Appearance'), T('Light, dark, or follow your system setting. Saved on this device only.'), themeSeg())}
      ${settingsSection(T('Language'), T('The language of the interface. Names you typed (categories, accounts) stay as you wrote them.'),
        `<md-outlined-select id="set-lang" class="w-full" label="${T('Interface language')}" aria-label="${T('Interface language')}">${langOptions}</md-outlined-select>`)}
    </div>`;
    customElements.whenDefined('md-outlined-select').then(() => { const l = $('#set-lang'); if (l) l.value = c.language; });
    onSettingsField('set-lang', 'change', e => { c.language = e.target.value || 'en'; scheduleSettingsSave({ relayout: true }); });

  } else if (area === 'accounting') {
    const ratioPct = Math.round((c.reference_ratio[p1] ?? 0.5) * 100);
    host.innerHTML = `<div class="flex flex-col gap-6">
      ${settingsSection(T('Income split default'), T('Monthly estimate only — the binding yearly settlement is computed from actual salaries. The other partner gets the remainder.'),
        `<div class="pt-1">${ratioSlider('set-ratio', ratioPct)}</div>`)}
      ${settingsSection(T('Categorization'), '',
        textField({ id: 'set-items', label: T('Items threshold (€)'), type: 'number', className: 'w-full', value: c.items_threshold_eur, attrs: 'min="0" step="0.01"', supportingText: T('Purchases at or under this amount are auto-filed as small items, above it as large items. Changing it re-categorizes these in all open months.') }))}
      ${settingsSection(T('Transfer matching'), T('How close in time and amount two opposite movements must be to be treated as one internal transfer.'), `
        <div class="grid2 gap-6">
          ${textField({ id: 'set-tw', label: T('Match window (days)'), type: 'number', value: c.transfer_match_window_days, attrs: 'min="0"' })}
          ${textField({ id: 'set-tt', label: T('Match tolerance (cents)'), type: 'number', value: c.transfer_match_tolerance_cents, attrs: 'min="0"' })}
        </div>`)}
      ${settingsSection(T('Currencies'), T('EUR is always the base currency and is included automatically.'),
        textField({ id: 'set-cur', label: T('Foreign currencies (comma-separated ECB codes)'), className: 'w-full', value: c.currencies.filter(x => x !== 'EUR').join(', '), placeholder: T('e.g. USD, CHF') }))}
    </div>`;
    wireRatioSlider('set-ratio', v => { c.reference_ratio = { [p1]: v / 100, [p2]: (100 - v) / 100 }; scheduleSettingsSave(); });
    onSettingsField('set-items', 'input', e => { c.items_threshold_eur = +e.target.value; scheduleSettingsSave(); });
    onSettingsField('set-tw', 'input', e => { c.transfer_match_window_days = Math.trunc(+e.target.value); scheduleSettingsSave(); });
    onSettingsField('set-tt', 'input', e => { c.transfer_match_tolerance_cents = Math.trunc(+e.target.value); scheduleSettingsSave(); });
    onSettingsField('set-cur', 'input', e => {
      const foreign = e.target.value.split(',').map(s => s.trim().toUpperCase()).filter(Boolean);
      c.currencies = ['EUR', ...foreign];
      scheduleSettingsSave();
    });

  } else if (area === 'data') {
    // part id → [label, description]; order matches the folder diagram. All checked by default.
    const parts = [
      ['data', T('Transactions & decisions'), T('The ledger, categorizations, accounts, balances, findings.')],
      ['rules', T('Rules & categories'), T('Categories, merchant rules, tax buckets, budgets.')],
      ['receipts', T('Receipts & attachments'), T('The PDFs and files you attach to transactions.')],
      ['inbox', T('Raw imported files'), T('The original bank exports you dropped in.')],
      ['feedback', T('My Notes'), T('Your notes and their attachments.')],
      ['config', T('Settings'), T('Household, split, currencies, language.')],
    ];
    const exportYears = (state.meta.years || [state.year]).slice().sort((a, b) => b - a);
    host.innerHTML = `<div class="flex flex-col gap-6">
      ${settingsSection(T('Where your data lives'),
        T('Everything is plain files on your machine — fully offline. You can copy any of these folders yourself, or download a backup below.'),
        folderTreeHtml())}
      ${settingsSection(T('Export transactions to a spreadsheet'),
        T('Every transaction of one year as a single table, plus a Summary tab with the dashboard figures — money in and out, savings, savings rate, month by month. Each figure sits beside a live formula that rebuilds it from the rows, so an auditor can check rather than trust.'), `
        <div class="flex items-center gap-2 flex-wrap">
          ${selectField({ id: 'export-year', label: T('Year'), value: state.year,
            options: exportYears.map(y => [y, String(y)]) })}
          <md-outlined-button id="export-txns"><md-icon slot="icon">table_view</md-icon>${T('Download spreadsheet')}</md-outlined-button>
        </div>
        <div class="type-caption mt-2" style="color:var(--ink2)">${T('Contains counterparty IBANs and your notes — worth a look before you forward it.')}</div>`)}
      ${settingsSection(T('Export a backup'), T('A zip of your data. Everything is included by default — untick anything you want to leave out.'), `
        <div class="export-parts">
          ${parts.map(([id, label, desc]) => `
            <label class="export-part">
              <md-checkbox touch-target="wrapper" class="exp-part" data-part="${id}" checked></md-checkbox>
              <span><span class="type-body font-medium">${label}</span><span class="type-caption" style="color:var(--ink2)">${desc}</span></span>
            </label>`).join('')}
        </div>
        <div class="mt-5"><md-filled-button id="do-backup"><md-icon slot="icon">cloud_download</md-icon>${T('Download backup')}</md-filled-button></div>`)}
      ${settingsSection(T('Import / restore a backup'), T('Load a backup zip. A backup of your current data is saved first, so you can always undo.'), `
        ${fileField({ id: 'imp-file', label: T('Choose backup zip'), accept: '.zip', className: 'mb-4' })}
        <div class="type-label mb-1" style="color:var(--ink2)">${T('How to apply it')}</div>
        <div class="mb-2">${segControl('imp-mode', [['replace', T('Replace')], ['merge', T('Merge')]], 'replace')}</div>
        <div class="type-caption mb-4" style="color:var(--ink2)">${T('Replace: wipe each restored folder, then load the backup (an exact mirror). Merge: add the backup on top, keeping files it does not contain.')}</div>
        <div class="type-label mb-1" style="color:var(--ink2)">${T('Restore which parts')}</div>
        <div class="export-parts">
          ${parts.map(([id, label]) => `
            <label class="export-part">
              <md-checkbox touch-target="wrapper" class="imp-part" data-part="${id}" checked></md-checkbox>
              <span><span class="type-body font-medium">${label}</span></span>
            </label>`).join('')}
        </div>
        <div class="mt-5"><md-filled-button id="do-restore" style="--md-filled-button-container-color:var(--bad)"><md-icon slot="icon">restore</md-icon>${T('Restore from backup')}</md-filled-button></div>`)}
      ${settingsSection(T('Maintenance'), T('Scans all years for data problems. Read-only until you act on a finding.'),
        `<div><md-outlined-button onclick="openDoctor()"><md-icon slot="icon">monitor_heart</md-icon>${T('Data health check')}</md-outlined-button></div>`)}
      ${settingsSection(T('Danger zone'), T('Permanently delete every transaction, decision and receipt for one year. A safety backup is saved to backups/ first, but there is no undo inside the app.'),
        `<div><md-outlined-button class="danger-btn" onclick="openDeleteYear()"><md-icon slot="icon">delete_forever</md-icon>${T('Delete a year…')}</md-outlined-button></div>`)}
    </div>`;
    wireFileField(host);
    // Content-Disposition download, so the page stays where it is.
    $('#export-txns').onclick = () => {
      const year = $('#export-year').value || state.year;
      window.location.href = `/api/transactions-export?year=${encodeURIComponent(year)}`;
    };
    $('#do-restore').onclick = () => {
      const fileEl = $('#imp-file');
      if (!fileEl || !fileEl.files.length) { showError(T('Choose a backup zip first.')); return; }
      const mode = readSeg($('#imp-mode')) || 'replace';
      const sel = [...document.querySelectorAll('.imp-part')].filter(cb => cb.checked).map(cb => cb.dataset.part);
      if (!sel.length) { showError(T('Pick at least one thing to restore.')); return; }
      confirmAction({
        title: T('Restore from backup?'), danger: true, confirmLabel: T('Restore'),
        body: `${mode === 'replace'
          ? T('This wipes the selected folders and replaces them with the backup.')
          : T('This adds the backup on top of your current data, overwriting matching files.')} ${T('A backup of your current data is saved to backups/ first, so you can undo. The page reloads when it is done.')}`,
        onConfirm: async () => {
          const fd = new FormData();
          fd.append('file', fileEl.files[0]);
          fd.append('mode', mode);
          fd.append('parts', sel.join(','));
          const res = await fetch('/api/restore', { method: 'POST', body: fd });
          if (!res.ok) { let d = await res.text(); try { d = JSON.parse(d).detail; } catch (_) { /* text */ } showError(T('Restore failed: ') + d); return; }
          const r = await res.json();
          showMessage(T('Restored {n} files ({parts}). Safety backup saved as {name}. Reloading…', { n: r.restored, parts: r.parts.join(', '), name: r.safety_backup }));
          setTimeout(() => location.reload(), 1600);
        },
      });
    };
    $('#do-backup').onclick = () => {
      const sel = [...document.querySelectorAll('.exp-part')].filter(cb => cb.checked).map(cb => cb.dataset.part);
      if (!sel.length) { showError(T('Pick at least one thing to export.')); return; }
      window.location.href = '/api/backup?parts=' + sel.join(',');   // Content-Disposition download; page stays
    };

  } else if (area === 'security') {
    fillSecurityArea(host);
  }
}

/* GitHub-style year deletion: pick a year, type the exact phrase "delete <year>" (paste is
   blocked so it must be typed), then the Delete button enables. The server re-checks the
   phrase and writes a safety backup before removing the year. */
function openDeleteYear() {
  const years = (state.meta.years || []).slice();
  if (!years.length) { showError(T('There is no year to delete.')); return; }
  let year = years[years.length - 1];               // default to the newest year
  const phraseFor = y => 'delete ' + y;
  const yearOpts = years.map(y => `<md-select-option value="${y}" ${y === year ? 'selected' : ''}><div slot="headline">${y}</div></md-select-option>`).join('');
  openModal({
    title: T('Delete a year'),
    body: `
      <div class="type-body-small" style="color:var(--bad); font-weight:var(--font-medium)">${T('This permanently deletes every transaction, decision and receipt for the chosen year. It cannot be undone from the app.')}</div>
      <div class="type-body-small mt-2" style="color:var(--ink2)">${T('A safety backup is written to backups/ first, so a mistake is recoverable from disk.')}</div>
      <div class="mt-4"><md-outlined-select id="dy-year" class="w-full" label="${T('Year to delete')}">${yearOpts}</md-outlined-select></div>
      <div class="type-body-small mt-4 mb-1" style="color:var(--ink2)">${T('To confirm, type')} <span id="dy-phrase" class="dy-phrase">${phraseFor(year)}</span></div>
      ${textField({ id: 'dy-confirm', label: T('Confirmation phrase'), className: 'w-full' })}`,
    actions: `<md-text-button class="dy-cancel">${T('Cancel')}</md-text-button><md-filled-button class="dy-go danger-fill" disabled><md-icon slot="icon">delete_forever</md-icon>${T('Delete year')}</md-filled-button>`,
    onMount: root => {
      const sel = root.querySelector('#dy-year'), phraseEl = root.querySelector('#dy-phrase');
      const input = root.querySelector('#dy-confirm'), go = root.querySelector('.dy-go');
      const revalidate = () => { go.disabled = (input.value || '').trim() !== phraseFor(year); };
      root.querySelector('.dy-cancel').onclick = () => root._close();
      customElements.whenDefined('md-outlined-select').then(() => { sel.value = String(year); });
      sel.addEventListener('change', () => { year = +sel.value; phraseEl.textContent = phraseFor(year); revalidate(); });
      input.addEventListener('input', revalidate);
      input.addEventListener('paste', e => e.preventDefault(), true);   // no copy-paste: must be typed
      input.addEventListener('drop', e => e.preventDefault(), true);
      go.onclick = async () => {
        go.disabled = true;
        const res = await fetch('/api/delete-year', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ year, confirm: (input.value || '').trim() }) });
        if (!res.ok) { let d = await res.text(); try { d = JSON.parse(d).detail; } catch (_) { /* text */ } showError(T('Delete failed: ') + d); revalidate(); return; }
        const r = await res.json();
        root._close();
        showMessage(T('Deleted {year}. Safety backup saved as {name}. Reloading…', { year: r.year, name: r.safety_backup || '—' }));
        setTimeout(() => location.reload(), 1500);
      };
    },
  });
}

/* Direct fetch (not api()) for the lock endpoints: a 401 here means the session lapsed
   mid-edit — surface the lock screen rather than a generic error. Returns Response or null. */
async function securityFetch(path, body) {
  const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (res.status === 401) { showLockScreen(); return null; }
  return res;
}

function fillSecurityArea(host) {
  const m = state.meta;
  const caveat = T('Casual privacy only — the data files on disk are not encrypted and stay readable to anyone with this computer.');
  if (!m.lock_enabled) {
    host.innerHTML = settingsSection(T('App lock'),
      T('Off by default. Set a password to lock the app; it asks for the password whenever the server restarts.') + ' ' + caveat, `
      ${textField({ id: 'sec-new', label: T('New password'), type: 'password', className: 'w-full mb-3' })}
      ${textField({ id: 'sec-new2', label: T('Confirm password'), type: 'password', className: 'w-full' })}
      <div id="sec-err" class="type-body-small mt-2" style="color:var(--bad); min-height:20px"></div>
      <div class="mt-1"><md-filled-button id="sec-enable"><md-icon slot="icon">lock</md-icon>${T('Enable lock')}</md-filled-button></div>`);
    $('#sec-enable').onclick = async () => {
      const a = $('#sec-new').value, b = $('#sec-new2').value, err = $('#sec-err');
      if (a.length < 4) { err.textContent = T('Password must be at least 4 characters.'); return; }
      if (a !== b) { err.textContent = T('Passwords do not match.'); return; }
      const res = await securityFetch('/api/security/set-password', { new_password: a });
      if (!res) return;
      if (!res.ok) { err.textContent = T('Could not set the password.'); return; }
      state.meta = await api('/api/meta'); setupInactivityTimer(); renderSettings(); showMessage(T('App lock enabled.'));
    };
    return;
  }
  host.innerHTML = `<div class="flex flex-col gap-6">
    ${settingsSection(T('App lock'), T('The app is locked with a password; it re-locks whenever the server restarts.') + ' ' + caveat,
      `<div><md-outlined-button onclick="lockNow()"><md-icon slot="icon">lock</md-icon>${T('Lock now')}</md-outlined-button></div>`)}
    ${settingsSection(T('Auto-lock on inactivity'), T('Also lock automatically after a period of no activity, like an online bank.'), `
      ${switchField({ id: 'sec-auto', label: T('Lock after inactivity'), on: !!m.auto_lock, className: 'sec-auto-field' })}
      <div class="mt-3" style="max-width:220px">${textField({ id: 'sec-timeout', label: T('Minutes'), type: 'number', value: m.lock_timeout || 5, attrs: 'min="1" max="1440"' })}</div>
      <div class="mt-3"><md-filled-button id="sec-auto-save">${T('Save')}</md-filled-button></div>`)}
    ${settingsSection(T('Change password'), '', `
      ${textField({ id: 'cp-cur', label: T('Current password'), type: 'password', className: 'w-full mb-3' })}
      ${textField({ id: 'cp-new', label: T('New password'), type: 'password', className: 'w-full mb-3' })}
      ${textField({ id: 'cp-new2', label: T('Confirm new password'), type: 'password', className: 'w-full' })}
      <div id="cp-err" class="type-body-small mt-2" style="color:var(--bad); min-height:20px"></div>
      <div class="mt-1"><md-filled-button id="cp-save">${T('Change password')}</md-filled-button></div>`)}
    ${settingsSection(T('Turn off the lock'), T('Remove the password. The app will no longer ask to unlock.'), `
      ${textField({ id: 'rm-cur', label: T('Current password'), type: 'password', className: 'w-full' })}
      <div class="mt-1"><md-outlined-button id="rm-go" style="--md-outlined-button-label-text-color:var(--bad)"><md-icon slot="icon">lock_open</md-icon>${T('Remove password')}</md-outlined-button></div>`)}
  </div>`;
  $('#sec-auto-save').onclick = async () => {
    const on = readSwitch(host, 'sec-auto-field');
    const mins = Math.max(1, Math.min(1440, Math.trunc(+$('#sec-timeout').value || 5)));
    const res = await securityFetch('/api/security/auto-lock', { auto_lock: on, timeout_minutes: mins });
    if (!res) return;
    if (!res.ok) { showError(T('Could not save.')); return; }
    state.meta = await api('/api/meta'); setupInactivityTimer(); showMessage(T('Saved'));
  };
  $('#cp-save').onclick = async () => {
    const cur = $('#cp-cur').value, a = $('#cp-new').value, b = $('#cp-new2').value, err = $('#cp-err');
    if (a.length < 4) { err.textContent = T('Password must be at least 4 characters.'); return; }
    if (a !== b) { err.textContent = T('Passwords do not match.'); return; }
    const res = await securityFetch('/api/security/set-password', { new_password: a, current_password: cur });
    if (!res) return;
    if (!res.ok) { err.textContent = res.status === 403 ? T('Current password is wrong.') : T('Could not change the password.'); return; }
    renderSettings(); showMessage(T('Password changed.'));
  };
  $('#rm-go').onclick = () => confirmAction({
    title: T('Turn off the lock?'), danger: true, confirmLabel: T('Remove password'),
    body: T('The app will stop asking for a password.'),
    onConfirm: async () => {
      const res = await securityFetch('/api/security/remove-password', { current_password: $('#rm-cur').value });
      if (!res) return;
      if (!res.ok) { showError(res.status === 403 ? T('Current password is wrong.') : T('Could not remove the password.')); return; }
      state.meta = await api('/api/meta'); clearInactivityTimer(); renderSettings(); showMessage(T('App lock disabled.'));
    },
  });
}

/* Visual map of the on-disk project folder so a user can copy files themselves (offline-first). */
function folderTreeHtml() {
  const root = state.meta.root || 'FamilyAccountability';
  const name = root.split('/').filter(Boolean).pop() || 'FamilyAccountability';
  const row = (depth, icon, label, note, last) => `<div class="folder-row" style="--depth:${depth}">
    <span class="folder-branch">${depth ? (last ? '└─' : '├─') : ''}</span>
    <md-icon class="folder-icon">${icon}</md-icon><span class="folder-name">${esc(label)}</span>
    ${note ? `<span class="folder-note">${note}</span>` : ''}</div>`;
  return `<div class="folder-tree">
    <div class="folder-path type-caption" ${tooltip(esc(root))}>${esc(root)}</div>
    ${row(0, 'folder_open', name + '/', '')}
    ${row(1, 'description', 'config.json', T('household, split, currencies, language'))}
    ${row(1, 'folder', 'data/', T('transactions, decisions, accounts, balances, findings'))}
    ${row(1, 'folder', 'rules/', T('categories, merchant rules, tax buckets, budgets'))}
    ${row(1, 'folder', 'receipts/', T('attachments & receipt PDFs'))}
    ${row(1, 'folder', 'inbox/', T('raw imported bank files'))}
    ${row(1, 'folder', 'feedback/', T('your notes and their files'))}
    ${row(1, 'folder', 'backups/', T('the backup zips you download'), true)}
  </div>`;
}

function settingsPayload() {
  const c = state.settingsCfg, [p1, p2] = state.meta.people;
  const foreign = (c.currencies || []).filter(x => x !== 'EUR');
  return {
    person_labels: { [p1]: String(c.person_labels[p1]).trim(), [p2]: String(c.person_labels[p2]).trim() },
    reference_ratio: c.reference_ratio,
    items_threshold_eur: c.items_threshold_eur,
    transfer_match_window_days: c.transfer_match_window_days,
    transfer_match_tolerance_cents: c.transfer_match_tolerance_cents,
    currencies: ['EUR', ...foreign],
    household_name: c.household_name || '',
    language: c.language || 'en',
    person_styles: state.settingsPStyle,
    shared_style: state.settingsSharedStyle,
    brand_style: state.settingsBrandStyle,
  };
}

/* Client-side pre-check so a half-typed field does not fire a doomed request. The server
   remains the final authority — its 400 detail also surfaces in the status flag. */
function validateSettings() {
  const c = state.settingsCfg, [p1, p2] = state.meta.people;
  if (!String(c.person_labels[p1] || '').trim() || !String(c.person_labels[p2] || '').trim())
    return { ok: false, message: T('Not saved — partner names cannot be empty') };
  if (!(c.items_threshold_eur > 0)) return { ok: false, message: T('Not saved — items threshold must be positive') };
  if (!(c.transfer_match_window_days >= 0) || !(c.transfer_match_tolerance_cents >= 0))
    return { ok: false, message: T('Not saved — transfer values must be ≥ 0') };
  return { ok: true };
}

function setSaveStatus(kind, msg) {
  const el = $('#save-status'); if (!el) return;
  const icon = kind === 'saving' ? 'sync' : kind === 'error' ? 'error' : 'check_circle';
  el.className = 'save-status ' + kind;
  el.innerHTML = `<md-icon>${icon}</md-icon>${esc(msg)}`;
}

function scheduleSettingsSave(opts = {}) {
  clearTimeout(state.settingsSaveTimer);
  const v = validateSettings();
  if (!v.ok) { setSaveStatus('error', v.message); return; }
  setSaveStatus('saving', T('Saving…'));
  state.settingsSaveTimer = setTimeout(() => commitSettingsSave(opts), 400);
}

async function commitSettingsSave(opts = {}) {
  const seq = ++state.settingsSaveSeq;   // latest-wins: a newer change invalidates this response
  let res;
  try {
    res = await fetch('/api/settings-update', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(settingsPayload()) });
  } catch (_) { res = null; }
  if (seq !== state.settingsSaveSeq) return;       // superseded — ignore
  if (!res || !res.ok) {
    let msg = T('Not saved');
    if (res) { try { const d = JSON.parse(await res.text()).detail; if (d) msg = T('Not saved') + ' — ' + d; } catch (_) { /* opaque */ } }
    setSaveStatus('error', msg);
    return;
  }
  invalidateYearCache();                            // threshold/ratio/currencies affect derived views
  state.meta = await api('/api/meta');
  applyHouseholdName(state.meta.household_name);
  setSaveStatus('saved', T('Saved'));
  if (opts.relayout) { applyLanguage(state.meta.language || 'en'); renderSettings(); }  // language: retranslate + rebuild
}

async function renderFeedback(renderId = state.renderId) {
  const data = await api('/api/feedback');
  if (!renderIsCurrent(renderId, 'feedback', state.year)) return;
  state.feedbackEntries = Object.fromEntries(data.items.map(entry => [entry.id, entry]));
  $('#main').innerHTML = `<div class="card p-6 max-w-lg">
    <h2 class="mb-2">${T('My Notes')}</h2>
    <div class="type-body-small mb-4" style="color:var(--ink2)">${T('Record an idea, issue, or improvement for the website.')}</div>
    <div class="space-y-3">
      ${textField({ id: 'feedback-title', label: T('Title'), className: 'w-full', attrs: 'maxlength="200" required' })}
      ${textField({ id: 'feedback-description', label: T('Description'), type: 'textarea', className: 'w-full', attrs: 'rows="6" maxlength="10000" required' })}
      ${fileField({ id: 'feedback-file', label: T('Add file') })}
      <div><md-filled-button id="feedback-save"><md-icon slot="icon">send</md-icon>${T('Save feedback')}</md-filled-button></div>
    </div>
  </div>
  <div class="card p-6 mt-4">
    <h2 class="mb-3">${T('My Notes ({n})', { n: data.items.length })}</h2>
    <div id="feedback-entries">${data.items.length ? data.items.map(feedbackEntryHtml).join('') : `<div class="type-body-small" style="color:var(--ink2)">${T('No feedback entries yet.')}</div>`}</div>
  </div>`;
  wireFileField($('#main'));
  $('#feedback-save').onclick = saveFeedback;
  $('#feedback-entries').onclick = event => {
    const button = event.target.closest('.feedback-delete');
    if (button) confirmDeleteFeedback(button.dataset.id);
  };
}

function confirmDeleteFeedback(id) {
  const entry = (state.feedbackEntries || {})[id];
  if (!entry) return;
  confirmAction({
    title: T('Delete feedback?'),
    body: entry.attachment
      ? T('Delete {title} and its attached file? This cannot be undone.', { title: `<b>${esc(entry.title)}</b>` })
      : T('Delete {title}? This cannot be undone.', { title: `<b>${esc(entry.title)}</b>` }),
    confirmLabel: T('Delete'), danger: true,
    onConfirm: async () => { await api('/api/feedback-delete', { id }); await renderFeedback(); },
  });
}

async function saveFeedback() {
  const title = $('#feedback-title').value.trim();
  const description = $('#feedback-description').value.trim();
  if (!title || !description) { showError(T('Title and description are required.')); return; }
  const button = $('#feedback-save');
  button.disabled = true;
  const form = new FormData();
  form.append('title', title);
  form.append('description', description);
  const file = $('#feedback-file').files[0];
  if (file) form.append('file', file);
  try {
    const response = await fetch('/api/feedback', { method: 'POST', body: form });
    if (!response.ok) throw new Error(await response.text());
    await renderFeedback();
    showMessage(T('Feedback saved.'));
  } catch (error) {
    showError(T('Could not save feedback: ') + error.message);
    button.disabled = false;
  }
}

boot();
