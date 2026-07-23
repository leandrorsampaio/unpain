# Changelog

This file documents all notable changes to UnPain.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). UnPain aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-07-22

### Added

- Initial public release. UnPain is a self-hosted, offline-first expense tracker for two-person
  households:
  - Bank CSV ingestion with format auto-detection (`pipeline/formats/*.json`), content-hash dedupe,
    and ECB foreign-currency conversion. Deterministic PDF extraction for PDF-only sources, gated by
    balance reconciliation.
  - Learned merchant-categorization rules (family- and per-person-scoped), applied on read. UnPain
    recomputes everything derived (dashboards, settlement, tax) and never stores it.
  - Income-proportional settlement between partners, with a monthly estimate and a binding annual
    true-up. Recurring-payment detection. A year-end German tax evidence pack.
  - FastAPI and vanilla-JS Material Design 3 web UI (English and German), fully offline. Optional
    LLM skills at the edges (categorization proposals, statement extraction) — no API keys, no
    vendor lock-in.
  - Try it with `./start.sh --demo` (synthetic data in an isolated `./demo/`).
