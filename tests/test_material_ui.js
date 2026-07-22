#!/usr/bin/env node
// Guard the UI architecture: standard controls stay on the shared M3 helpers.
const fs = require('fs');
const assert = require('assert');

const app = fs.readFileSync('app/static/app.js', 'utf8');
const index = fs.readFileSync('app/static/index.html', 'utf8');
const entry = fs.readFileSync('app/vendor-build/scripts/material-entry.js', 'utf8');

for (const helper of ['textField', 'selectField', 'checkboxField', 'fileField', 'openModal']) {
  assert.match(app, new RegExp(`function ${helper}\\b`), `missing shared ${helper} helper`);
}
assert.match(app, /function switchField\b[\s\S]*?<md-switch/, 'labeled switches must use the shared M3 helper');
assert.match(app, /function lowActivitySwitch\b[\s\S]*?switchField\(/,
  'account low-activity control must reuse the shared switch helper');
assert.match(app, /function anchorStatusChip\b/, 'shared balance-anchor status chip missing');
assert.match(app, /function openRecordBalance\b[\s\S]*?openModal\([\s\S]*?textField\(/,
  'manual balance entry must reuse the shared modal and Material text fields');
assert.match(app, /api\('\/api\/anchor'/, 'manual balance entry endpoint is not wired');

assert.doesNotMatch(app, /\b(?:alert|confirm|prompt)\s*\(/, 'native browser dialogs are not M3');
assert.doesNotMatch(app, /<select\b|<textarea\b/i, 'native visible fields must use Material helpers');
assert.doesNotMatch(index, /<select\b|<textarea\b|<input\b|<button\b/i, 'app shell controls must be Material');
for (const asset of ['theme.css', 'app.css', 'material.js', 'app.js']) {
  assert.match(index, new RegExp(`${asset.replace('.', '\\.') }\\?v=`), `${asset} must be cache-versioned`);
}
assert.strictEqual((app.match(/document\.createElement\(['"]md-dialog['"]\)/g) || []).length, 1,
  'all dialogs must use the one shared Material modal');
assert.doesNotMatch(app, /cat-backdrop|generic-modal-head/, 'legacy hand-built modal returned');
assert.match(index, /<footer class="app-footer">[\s\S]*id="footer-household"[\s\S]*<md-outlined-button href="#feedback">/,
  'shared footer (config-driven household name) and Material feedback action missing');
// Backup + data health check moved out of the app bar into Settings › Data.
assert.match(app, /\/api\/backup/, 'backup download must be reachable (Settings › Data)');
assert.match(app, /onclick="openDoctor\(\)"/, 'data health check must be reachable from Settings › Data');
assert.match(app, /function openDoctor\b[\s\S]*?openModal\(/,
  'data doctor must use the shared modal');
assert.match(app, /function runDoctor\b[\s\S]*?api\('\/api\/doctor'/,
  'data doctor must fetch findings from the API');
assert.match(app, /function doctorRowHtml\b[\s\S]*?esc\(detail\.id\)/,
  'doctor result ids must be escaped');
assert.match(app, /doctorAction\b[\s\S]*?'\/api\/decision-clear-orphan'/,
  'doctor must clear orphan decisions through the guarded endpoint');
assert.match(app, /feedback-description[\s\S]*type: 'textarea'/,
  'feedback description must use the shared Material text field');
assert.match(app, /confirmDeleteFeedback[\s\S]*confirmAction\(/,
  'feedback deletion must use the shared confirmation modal');

for (const component of [
  'dialog/dialog.js', 'select/outlined-select.js', 'list/list.js',
  'chips/filter-chip.js',
]) {
  assert.ok(entry.includes(component), `Material bundle missing ${component}`);
}

assert.doesNotMatch(app, /<md-radio\b|readRadio\b|md-radio-option/, 'segmented controls must not show radio glyphs');
assert.match(app, /function selectSegment\b/, 'shared segmented selection handler missing');
assert.doesNotMatch(app, /\['',\s*'From account'\]|function oosToggle\b|class="oos-field"/, 'income owner must be one segmented control without a separate out-of-scope switch');
assert.match(app, /return personSegment\(id, options, selected, 'owner-field'\)/, 'income owner must reuse the shared personSegment control');
assert.match(app, /function personSegment\b/, 'single person/sharing segmented component missing');
assert.match(app, /current \|\| accountOwner\(accountId\)/, 'income owner must default to the bank account owner');

console.log('Material UI guard passed: shared fields/dialogs and M3 controls only');
