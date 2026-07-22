<!-- Thanks for contributing! Please keep PRs focused and read CONTRIBUTING.md. -->

## What & why

Describe the change and the motivation. Link any related issue (`Fixes #123`).

## Checklist

- [ ] `./run-tests.sh` passes locally (full suite, or `--fast` + CI for non-UI changes).
- [ ] No test assertion was weakened or removed to make things pass.
- [ ] User-facing strings go through `T(...)` and exist in `app/static/i18n/de.js` (UI changes).
- [ ] User text is escaped (`esc()`); money uses `cents()` (UI/backend).
- [ ] No personal data, real IBANs, or real statement rows added anywhere (fixtures are synthetic).
- [ ] For a new bank format: a JSON file in `pipeline/formats/` (+ a fixture if possible).
