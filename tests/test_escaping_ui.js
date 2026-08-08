#!/usr/bin/env node
// Guard the two ways user-written text reaches the page as markup.
//
// Category names are free text and icons are free text, and both are interpolated into
// innerHTML all over app.js. That made renaming a category to `<img onerror=...>` a way to
// run script in the app on every screen that shows it — for anyone on the network in LAN
// mode, or through an imported config or restored backup. The fixes are structural, so the
// guard is structural: names are escaped at every HTML sink, and every icon goes through
// one validator instead of being escaped at each of a dozen sinks.
const fs = require('fs');
const assert = require('assert');

const app = fs.readFileSync('app/static/app.js', 'utf8');

// --- icons: one validator, used by every source a person can write to ---
assert.match(app, /const ICON_NAME = \/\^\[a-z0-9_\]/,
  'icon names must be validated against a Material Symbol pattern');
assert.match(app, /function safeIcon\(/, 'the one icon validator is missing');
assert.match(app, /function defaultIcon\([\s\S]{0,220}?safeIcon\(cat\.icon/,
  'a category icon must pass through safeIcon()');
assert.match(app, /function personIcon\([\s\S]{0,160}?safeIcon\(/,
  'a partner icon must pass through safeIcon()');
assert.match(app, /const sharedIcon = [^\n]*safeIcon\(/,
  'the shared/together icon must pass through safeIcon()');
assert.match(app, /const brandIcon = [^\n]*safeIcon\(/,
  'the app-bar icon must pass through safeIcon()');

// --- names: escaped wherever they are interpolated into markup ---
// catName/groupName/subName deliberately return plain text, because chart labels are drawn
// to a canvas and must not carry entities. So the escaping belongs at the HTML sinks, and
// what this checks is that no raw `.name` interpolation is left in a template.
// Only markup lines are scanned: `catName()` itself returns `${grp.name} · ${sub.name}`
// unescaped on purpose, and a chart label that arrived pre-escaped would render `&amp;`.
const RAW_NAME = /\$\{(?:c|s|g|grp|sub)\.name(?:\.toLowerCase\(\))?\}/;
const offenders = app.split('\n')
  .map((line, i) => [i + 1, line])
  .filter(([, line]) => /<[a-z]|data-[a-z]+=/.test(line) && RAW_NAME.test(line));
assert.strictEqual(offenders.length, 0,
  `category names must be esc()-d before reaching innerHTML — app.js:${offenders.map(([n]) => n).join(', ')}`);

for (const [what, pattern] of [
  ['the category picker heading', /<h4><md-icon>\$\{defaultIcon\(c\)\}<\/md-icon>\$\{esc\(c\.name\)\}/],
  ['the category picker sub buttons', /data-name="\$\{esc\(s\.name\.toLowerCase\(\)\)\}"[^]{0,80}\$\{esc\(s\.name\)\}/],
  ['the category picker box filter', /data-name="\$\{esc\(c\.name\.toLowerCase\(\)\)\}"/],
  ['the Categories page group row', /<span class="font-medium">\$\{esc\(g\.name\)\}<\/span>/],
  ['the Categories page sub row', /padding-left:12px">\$\{esc\(s\.name\)\}<\/span>/],
  ['the category field trigger', /catTriggerHtml[^]{0,240}?esc\(catLabel\(slug\)\)/],
  ['the rename button payload', /renameCategory\('\$\{slug\}', \$\{esc\(JSON\.stringify\(name\)\)\}\)/],
  ['the year-over-year category rows', /row\(esc\(catName\(c\)\)/],
  ['the split child label', /esc\(s\.purpose\) \|\| esc\(catName\(s\.category\)\)/],
]) {
  assert.match(app, pattern, `${what} must escape the category name`);
}

console.log('Escaping guard passed: category names escaped at every sink, icons validated once');
