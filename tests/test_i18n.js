#!/usr/bin/env node
/* i18n completeness guard: every T('key') used in the UI (app.js + index.html
 * data-i18n attributes) must have a German entry in app/static/i18n/de.js.
 * Natural-key fallback means a miss degrades to English silently — this test is
 * what turns that silent gap into a failing build. */
'use strict';
const fs = require('fs');
const path = require('path');

const STATIC = path.resolve(__dirname, '..', 'app', 'static');
const app = fs.readFileSync(path.join(STATIC, 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
const deSrc = fs.readFileSync(path.join(STATIC, 'i18n', 'de.js'), 'utf8');

// Collect the first string-literal argument of every T(...) call.
const keys = new Set();
const re = /\bT\(\s*('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`(?:[^`\\]|\\.)*`)/g;
for (let m; (m = re.exec(app)); ) {
  const raw = m[1], q = raw[0];
  let body = raw.slice(1, -1);
  body = body.replace(new RegExp('\\\\' + q, 'g'), q).replace(/\\\\/g, '\\');
  keys.add(body);
}
for (const m of html.matchAll(/data-i18n(?:-\w+)?="([^"]*)"/g)) keys.add(m[1]);

// Load de.js by faking the global I18N.register the file calls.
const dict = {};
global.I18N = { register: (_code, d) => Object.assign(dict, d) };
// eslint-disable-next-line no-eval
eval(deSrc.replace(/^'use strict';/, ''));
const have = new Set(Object.keys(dict));

const missing = [...keys].filter(k => !have.has(k)).sort();
if (missing.length) {
  console.error(`i18n: ${missing.length} key(s) used in the UI have no German translation in de.js:`);
  missing.forEach(k => console.error('  ' + JSON.stringify(k)));
  process.exit(1);
}
// A repeated key is not a duplicate — in an object literal the last one silently wins.
// Adding 'Close': 'Abschluss' for a checkpoint kind quietly relabelled every dialog's
// Close button, and nothing failed: the key existed, so the coverage check above was
// perfectly happy. Only the file itself can show it.
const declared = [...deSrc.matchAll(/^\s*('(?:[^'\\]|\\.)*')\s*:/gm)].map(m => m[1]);
const seen = new Set();
const repeated = [...new Set(declared.filter(k => seen.has(k) || (seen.add(k), false)))].sort();
if (repeated.length) {
  console.error(`i18n: ${repeated.length} key(s) declared more than once in de.js — the last`);
  console.error('      declaration silently overrides the earlier ones:');
  repeated.forEach(k => console.error('  ' + k));
  process.exit(1);
}
console.log(`i18n OK: all ${keys.size} translation keys present in de.js, none declared twice`);
