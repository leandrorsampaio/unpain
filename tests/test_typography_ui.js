#!/usr/bin/env node
// Guard the six-role typography system against one-off utility sizes returning.
const fs = require('fs');
const assert = require('assert');

const css = fs.readFileSync('app/static/app.css', 'utf8');
const app = fs.readFileSync('app/static/app.js', 'utf8');

const roles = ['caption', 'label', 'body-small', 'body', 'title', 'headline'];
for (const role of roles) {
  assert.match(css, new RegExp(`\\.type-${role}\\s*\\{`), `missing type-${role} role`);
}
for (const modifier of ['regular', 'medium', 'bold', 'italic']) {
  assert.match(css, new RegExp(`\\.font-${modifier}\\s*\\{`), `missing font-${modifier} modifier`);
}

assert.doesNotMatch(app + css, /\btext-(?:xs|sm|base|lg|xl|2xl)\b/, 'legacy size utilities returned');
assert.match(css, /--md-ref-typeface-brand:\s*var\(--typeface\)/, 'Material brand typeface is not shared');
assert.match(css, /--md-ref-typeface-plain:\s*var\(--typeface\)/, 'Material plain typeface is not shared');

for (const line of app.split('\n').filter(line => /font-size\s*:/.test(line))) {
  assert.match(line, /<md-icon\b/, `arbitrary inline text size: ${line.trim()}`);
}

console.log('Typography guard passed: six semantic roles, shared family and controlled modifiers');
