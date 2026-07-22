/* Family Accountability — tiny offline i18n engine.
 *
 * Natural-key translation: the English source string IS the key. English needs
 * no dictionary (T() returns the key unchanged). Other languages register a
 * { "English source": "translation" } map. A missing key degrades to English.
 *
 * Loaded as a classic <script> BEFORE app.js. Language dictionaries (i18n/de.js …)
 * load after this file and self-register via I18N.register(code, dict).
 *
 * Interpolation: T('{n} to review', { n: 5 }) -> '5 to review' (or the German
 * equivalent with the same {n} placeholder). Placeholder values are substituted
 * verbatim — never pass unescaped user text; escape it before it reaches innerHTML.
 */
'use strict';

const I18N = {
  lang: 'en',
  dict: {},                     // active non-English map (English -> translation)
  dicts: { en: {} },            // all registered maps, keyed by language code
  /* Human-readable language names for the picker. Add a code here + ship an
     i18n/<code>.js dictionary to support a new language. */
  names: { en: 'English', de: 'Deutsch' },

  register(code, dict) {
    this.dicts[code] = dict || {};
    if (code === this.lang) this.dict = this.dicts[code];   // late-loaded active dict
  },

  set(code) {
    this.lang = this.names[code] ? code : 'en';
    this.dict = this.dicts[this.lang] || {};
  },

  codes() { return Object.keys(this.names); },
};

/* Translate. `key` is the English source string. `vars` fills {placeholders}. */
function T(key, vars) {
  let s = (I18N.lang !== 'en' && I18N.dict[key]) || key;
  if (vars) s = s.replace(/\{(\w+)\}/g, (m, name) => (name in vars ? String(vars[name]) : m));
  return s;
}

/* Retranslate the static HTML chrome (index.html) in place. Elements opt in with:
     data-i18n="English text"        -> sets textContent
     data-i18n-title="English text"  -> sets the title attribute
     data-i18n-label="English text"  -> sets the label attribute (md-* components)
     data-i18n-aria="English text"   -> sets aria-label
   The attribute value stays the English source (the key), so this is idempotent. */
function translateChrome(root = document) {
  root.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = T(el.getAttribute('data-i18n')); });
  const attr = (sel, name) => root.querySelectorAll(`[data-i18n-${sel}]`).forEach(el =>
    el.setAttribute(name, T(el.getAttribute(`data-i18n-${sel}`))));
  attr('title', 'title');
  attr('label', 'label');
  attr('aria', 'aria-label');
}

/* Apply a language everywhere: active dict, <html lang>, chrome, persisted mirror. */
function applyLanguage(code) {
  I18N.set(code);
  document.documentElement.lang = I18N.lang;
  try { localStorage.setItem('fa-language', I18N.lang); } catch (_) { /* private mode */ }
  translateChrome();
}

/* Early paint: adopt the last-used language from localStorage before /api/meta
   returns, so the chrome does not flash English then swap. */
try {
  const early = localStorage.getItem('fa-language');
  if (early) I18N.set(early);
} catch (_) { /* ignore */ }
