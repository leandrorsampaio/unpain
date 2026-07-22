#!/usr/bin/env node
// Guard the performance architecture of the two data-heavy tabs.
const fs = require('fs');
const assert = require('assert');

const app = fs.readFileSync('app/static/app.js', 'utf8');

const tabHandler = app.match(/tabs\.addEventListener\('change',[\s\S]*?\n  \}\);/)?.[0] || '';
assert.ok(tabHandler, 'tab change handler not found');
assert.doesNotMatch(tabHandler, /\brender\s*\(/, 'tab click must not render before hashchange');

const renderShell = app.match(/async function render\(\)[\s\S]*?\n\}/)?.[0] || '';
assert.doesNotMatch(renderShell, /\/api\/(?:summary|review)\?/, 'global render must not fetch heavy view data');
assert.match(app, /function cachedYearData\b/, 'year data cache missing');
assert.match(app, /function invalidateYearCache\b/, 'cache invalidation missing');
assert.match(app, /api\('\/api\/decisions-bulk'/, 'review groups must use one atomic bulk decision write');
assert.match(app, /function coverageCard\b/, 'shared statement coverage renderer missing');
assert.match(app, /function coverageGapsFor\b/, 'shared statement coverage gap helper missing');
assert.match(app, /if \(view === 0\) \{[\s\S]*?fillRecurring\(token\);\s*fillCoverage\(token\);/,
  'Dashboard coverage must load lazily on the year view only');

assert.match(app, /const REVIEW_BATCH_SIZE = 30/, 'Review groups must render incrementally');
assert.match(app, /function appendReviewGroups\b/, 'Review load-more renderer missing');
assert.match(app, /function toggleReviewGroup\b/, 'Review details must render on expansion');
assert.match(app, /bodyHtml: open \? reviewDetailsHtml\(txns, gi\) : ''/, 'collapsed Review details rendered eagerly');

assert.match(app, /function renderTxnMonth\b/, 'transaction month lazy renderer missing');
assert.match(app, /bodyHtml: open && idxs\.length \?/, 'collapsed transaction months rendered eagerly');

console.log('Heavy-tab guard passed: single navigation render, scoped fetches and lazy DOM');
