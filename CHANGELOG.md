# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
aims to adhere to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-07-22

### Added

- Initial public release. A self-hosted, offline-first expense tracker for two-person households:
  - Bank CSV ingestion with format auto-detection (`pipeline/formats/*.json`), content-hash
    dedupe, and ECB foreign-currency conversion; deterministic PDF extraction for PDF-only
    sources, gated by balance reconciliation.
  - Learned merchant-categorization rules (family- and per-person-scoped) applied on read;
    everything derived (dashboards, settlement, tax) is recomputed, never stored.
  - Income-proportional settlement between partners, with a monthly estimate and a binding
    annual true-up; recurring-payment detection; a year-end German tax evidence pack.
  - FastAPI + vanilla-JS Material Design 3 web UI (English and German), fully offline. Optional
    LLM skills at the edges (categorization proposals, statement extraction) — no API keys, no
    vendor lock-in.
  - Try it with `./start.sh --demo` (synthetic data in an isolated `./demo/`).
