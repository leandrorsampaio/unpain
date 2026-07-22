# Security policy

Family Accountability is a **self-hosted, offline-first** application. It has no cloud
backend, no accounts, and no telemetry — your financial data never leaves the machine you
run it on. That design also shapes its threat model, which you should understand before
exposing it to anything beyond `localhost`.

## Threat model — read this before running it on a network

**The app has no authentication or authorization by design.** Anyone who can reach the HTTP
port can read and modify all data. This is a deliberate trade-off for a two-person household
tool, not an oversight.

- **Default (safe):** `./start.sh` binds to `127.0.0.1` (localhost) only. Nothing outside your
  machine can reach it.
- **LAN mode (opt-in):** `./start.sh --lan` binds all interfaces so another device on your home
  network can reach it. Only do this on a network you trust. Anyone on that network can use the
  app with no login.
- **Never expose the port to the public internet.** There is no auth to stand between a stranger
  and your data. If you need remote access, put it behind a VPN or an authenticating reverse
  proxy that you operate.
- An **optional server-enforced app lock** exists (off by default) as a light convenience gate;
  it is **not** a substitute for network isolation and should not be relied on as a security
  boundary.

## What the app does protect

- **Deterministic data boundary.** LLM/agent output never writes to the canonical store
  directly — it goes through `inbox/` CSVs and a reconciliation gate. Everything derived is
  recomputed, never trusted from an external source.
- **Path-traversal hardening** on archive import (zip-slip entries are rejected) and on
  file-serving endpoints.
- **Pinned, hashed dependencies.** `requirements.txt` is fully pinned; GitHub Actions are pinned
  to commit SHAs; Dependabot proposes reviewed bumps rather than letting anything float.

## Your data is your responsibility

`data/`, `rules/`, `config.json`, `inbox/`, `receipts/`, and `backups/` are gitignored and live
only on your machine. Git does **not** carry them. Use the in-app Backup button (or your own
backup of the folder) — there is no server-side copy to recover from.

## Reporting a vulnerability

Please report security issues **privately**, not in a public issue:

- Preferred: open a private advisory via the repository's **Security → Report a vulnerability**
  tab (GitHub private vulnerability reporting).

Because this is a personal project shared as-is, there is no formal SLA, but reports are
genuinely appreciated and will be looked at. Please give enough detail to reproduce, and allow
reasonable time for a fix before any public disclosure.
