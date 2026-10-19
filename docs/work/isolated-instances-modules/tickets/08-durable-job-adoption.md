---
type: Work Ticket
title: "Move station and assessment execution onto durable tasks"
description: "Adopt durable task execution for existing station ingest and advisor evaluation, removing dependence on in-process live scheduler ownership while retaining deterministic replay."
status: blocked
blocked_by: ["07"]
---

# 08 — Move station and assessment execution onto durable tasks

**Source:** [Approved Change Spec](../spec.md), requirements I4. **Tier:** 3.

## Outcome and scope

Adopt durable task execution for existing station ingest and advisor evaluation, removing dependence on in-process live scheduler ownership while retaining deterministic replay. No new agronomic logic, modules or duplicate assessment model.

## Touchpoints

core/jobs.py and clock; stations/advisor manifests/jobs; worker entry point; existing replay CLI/tests. Keep old execution adapter only during verified migration.

## Blocked by

- [07 — Recover a leased forecast task after worker failure](07-durable-forecast-task.md)

## Acceptance criteria

- [ ] Restarting web/worker or beginning in replay then entering live does not lose future due work or create duplicate evaluations.
- [ ] Task failure/expired claim/mode generation cannot publish stale observations/assessment evidence; another farm's tasks progress.
- [ ] Existing replay order, farm-scoped reset/catch-up and semantic golden outputs remain unchanged; ordered lifecycle fixtures keep their documented exception.

## Execution discipline

One implementation worker owns the bounded slice; other agents are not alone and must preserve peers' edits. Work only after blockers pass; inherited draft code is not a completed dependency. Use TDD through the spec-approved public CLI/HTTP/job/package seams: observed RED, minimum GREEN, review/refactor, one vertical behavior at a time. Mock only external infrastructure/providers, not internal collaborators. The integration coordinator alone authors serialized revisions; keep one linear supported release chain. No live customer data, provisioning, payments, production migrations or deployment. Independent QA reviews spec conformance and standards separately before merge. Current PRs retain their own gates.

## Verification

The named new test files are implementation deliverables, not existing test claims.

- [ ] `uv run pytest tests/core/test_jobs.py tests/core/test_clock.py tests/core/test_task_adoption.py tests/apps/stations/test_jobs.py -q` — meaningful public contracts and semantic golden comparisons pass; retain relevant existing regression assertions.
- [ ] Tier-3 `verify` with real merge base: write failure model first; property/adversarial cases plus mutation or restored isolated manual mutants; required smoke through actual CLI/HTTP/worker/containers. Record failures before fixes and skips honestly.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass; update paths for a separately packaged hosting/addon layout and explicitly record scope.
- [ ] `uv run alembic heads` — one supported head; test upgrade of populated legacy data for schema changes. Release/profile compatibility and clean restored source hashes verified.
- [ ] Fresh independent reviewer checks every criterion on frozen source and records exact SHA/profile, commands, counts, baseline and remaining limits. Final integrated full/data/browser/Linux qualification belongs to20; focused checks do not claim it.

## Evidence

Not started. No product implementation or verification claimed by publishing this ticket.
