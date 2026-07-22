'use strict';

const fs = require('fs');
const path = require('path');

const source = fs.readFileSync(path.join(__dirname, '..', 'app', 'static', 'app.js'), 'utf8');
const start = source.indexOf('function savedYear(');
const end = source.indexOf('async function boot()', start);
if (start < 0 || end < 0) throw new Error('year-selection helpers not found');

const values = new Map();
const localStorage = {
  getItem: key => values.has(key) ? values.get(key) : null,
  setItem: (key, value) => values.set(key, String(value)),
  removeItem: key => values.delete(key),
};
const YEAR_SELECTION_KEY = 'fa-year-selection';
const YEAR_SELECTION_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const helpers = new Function(
  'localStorage', 'YEAR_SELECTION_KEY', 'YEAR_SELECTION_TTL_MS',
  `${source.slice(start, end)}; return {savedYear, rememberYear};`
)(localStorage, YEAR_SELECTION_KEY, YEAR_SELECTION_TTL_MS);
const {savedYear, rememberYear} = helpers;

const now = Date.UTC(2026, 6, 13);
const years = [2024, 2025, 2026];

localStorage.setItem(YEAR_SELECTION_KEY, JSON.stringify({year: 2025, savedAt: now - 6 * 24 * 60 * 60 * 1000}));
if (savedYear(years, now) !== 2025) throw new Error('recent year was not restored');

localStorage.setItem(YEAR_SELECTION_KEY, JSON.stringify({year: 2025, savedAt: now - 8 * 24 * 60 * 60 * 1000}));
if (savedYear(years, now) !== 2026) throw new Error('expired year did not fall back to newest');

localStorage.setItem(YEAR_SELECTION_KEY, JSON.stringify({year: 2023, savedAt: now}));
if (savedYear(years, now) !== 2026) throw new Error('unavailable year did not fall back to newest');

rememberYear(2024);
const remembered = JSON.parse(localStorage.getItem(YEAR_SELECTION_KEY));
if (remembered.year !== 2024 || !Number.isFinite(remembered.savedAt)) throw new Error('year was not saved');

console.log('Year selection passed: restore for 7 days -> expire to newest');
