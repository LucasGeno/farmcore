---
type: Work Ticket
title: "Recover a leased forecast task after worker failure"
description: "Add the smallest durable worker tracer through the live-forecast job: farm/job task identity, claim lease, attempt token, bounded external work and fenced receipt publication."
status: blocked
blocked_by: ["01","../../farmcore-revamp/tickets/12-integration-qa.md"]
---

# 07 — Recover a leased forecast task after worker failure

**Source:** [Approved Change Spec](../spec.md), requirements I4. **Tier:** 3.

## Outcome and scope

Add the smallest durable worker tracer through the live-forecast job: farm/job task identity, claim lease, attempt token, bounded external work and fenced receipt publication. Preserve the public manifest/job seam. Migrate other job classes in08; outbox is09.

## Touchpoints

core/jobs.py and JobSpec/JobContext; forecast job/store; new durable task model/worker CLI; coordinator-authored revision. Use current live-weather contracts as baseline.

## Blocked by

- [01 — Run two isolated installations from one release](01-portable-installations.md)
- [Current revamp final integration QA](../../farmcore-revamp/tickets/12-integration-qa.md)

## Acceptance criteria

- [ ] Separate worker processes cannot publish the same attempt twice; killing/expiring a lease permits recovery and rejects the stale worker at commit.
- [ ] A slow provider/DNS for A does not hold a global transaction or block unrelated eligible B work; retries preserve last valid receipts and remain bounded.
- [ ] Live/replay generation is checked at publication; mode switch, farm restriction and future-run filters preserve existing semantics. Runtime DB role remains installation-scoped.

## Execution discipline

One implementation worker owns the bounded slice; other agents are not alone and must preserve peers' edits. Work only after blockers pass; inherited draft code is not a completed dependency. Use TDD through the spec-approved public CLI/HTTP/job/package seams: observed RED, minimum GREEN, review/refactor, one vertical behavior at a time. Mock only external infrastructure/providers, not internal collaborators. The integration coordinator alone authors serialized revisions; keep one linear supported release chain. No live customer data, provisioning, payments, production migrations or deployment. Independent QA reviews spec conformance and standards separately before merge. Current PRs retain their own gates.

## Verification

The named new test files are implementation deliverables, not existing test claims.

- [ ] `uv run pytest tests/core/test_task_leases.py tests/apps/forecast/test_task_worker.py -q` — meaningful public contracts and semantic golden comparisons pass; retain relevant existing regression assertions.
- [ ] Tier-3 `verify` with real merge base: write failure model first; property/adversarial cases plus mutation or restored isolated manual mutants; required smoke through actual CLI/HTTP/worker/containers. Record failures before fixes and skips honestly.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass; update paths for a separately packaged hosting/addon layout and explicitly record scope.
- [ ] `uv run alembic heads` — one supported head; test upgrade of populated legacy data for schema changes. Release/profile compatibility and clean restored source hashes verified.
- [ ] Fresh independent reviewer checks every criterion on frozen source and records exact SHA/profile, commands, counts, baseline and remaining limits. Final integrated full/data/browser/Linux qualification belongs to20; focused checks do not claim it.

## Evidence

Not started. No product implementation or verification claimed by publishing this ticket.
