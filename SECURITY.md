# Security policy

UnPAIN is a **self-hosted, offline-first** application. It has no cloud backend, no accounts, and no
telemetry. Your financial data never leaves the machine that you run it on. That design also shapes
its threat model. Understand the threat model before you expose the app beyond `localhost`.

## Threat model — read this before you run it on a network

**The app has no authentication or authorization by design.** Anyone who can reach the HTTP port can
read and modify all data. This is a deliberate trade-off for a two-person household tool, not an
oversight.

- **Default (safe).** `./start.sh` binds to `127.0.0.1` (localhost) only. Nothing outside your
  machine can reach it.
- **LAN mode (opt-in).** `./start.sh --lan` binds all interfaces, so another device on your home
  network can reach it. Do this only on a network you trust. Anyone on that network can use the app
  with no login.
- **Never expose the port to the public internet.** No auth stands between a stranger and your data.
  If you need remote access, put it behind a VPN or an authenticating reverse proxy that you operate.
- An **optional server-enforced app lock** exists (off by default) as a light convenience gate. It
  is **not** a substitute for network isolation. Do not rely on it as a security boundary.

## What the app does protect

- **Deterministic data boundary.** LLM and agent output never writes to the canonical store
  directly. It goes through `inbox/` CSVs and a reconciliation gate. UnPAIN recomputes everything
  derived and never trusts it from an external source.
- **Path-traversal hardening.** UnPAIN rejects zip-slip entries on archive import and hardens the
  file-serving endpoints.
- **Pinned, hashed dependencies.** `requirements.txt` is fully pinned. GitHub Actions are pinned to
  commit SHAs. Dependabot proposes reviewed bumps rather than letting anything float.

## Your data is your responsibility

`data/`, `rules/`, `config.json`, `inbox/`, `receipts/`, and `backups/` are gitignored and live only
on your machine. Git does **not** carry them. Use the in-app Backup button, or make your own backup
of the folder. There is no server-side copy to recover from.

## Report a vulnerability

Report security issues **privately**, not in a public issue:

- Preferred: open a private advisory through the repository's **Security → Report a vulnerability**
  tab (GitHub private vulnerability reporting).

This is a personal project shared as-is, so there is no formal SLA. Reports are genuinely
appreciated, and the maintainer will look at them. Give enough detail to reproduce the issue. Allow
reasonable time for a fix before any public disclosure.
