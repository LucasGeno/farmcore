---
type: Work Ticket
title: "Live weather arrives with provenance and freshness"
description: "Milestones 1–4: live weather arrives with provenance and freshness."
status: approved
blocked_by: []
---

# 04 — Live weather arrives with provenance and freshness

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Live weather arrives with provenance and freshness. Touchpoints: forecast sources/jobs/manifest/store; stations ingestion; data-source views; focused tests. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain. Implementation workers propose schema changes; only the designated coordinator authors/serializes revision files. During access tickets 01–03, the access chat coordinator owns revisions exclusively; other chats request schema changes from the integration coordinator.

## Blocked by

None — can start immediately.

## Acceptance criteria

- [ ] Scheduled bounded fetch ingests real forecast responses through the existing run store, preserving issued/fetched/available/location/unit semantics.
- [ ] Retries/duplicates/provider failure preserve prior valid data and report freshness/coverage.
- [ ] Replay never consumes runs not available as of its clock; no reanalysis masquerading as issued forecast.
- [ ] Missing required observations/forecast inputs are explicitly unavailable; demo saved runs remain usable.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [ ] `uv run pytest tests/apps/forecast tests/apps/stations tests/apps/advisor -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [ ] `uv run alembic heads` — one head after integration.
- [ ] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Pending implementation.
