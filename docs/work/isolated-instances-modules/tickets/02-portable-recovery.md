---
type: Work Ticket
title: "Restore an installation into an independent self-hosted copy"
description: "Produce and restore a private integrity-checked recovery bundle containing data/files/release-module evidence."
status: blocked
blocked_by: ["01","../../farmcore-revamp/tickets/02-member-sessions.md"]
---

# 02 — Restore an installation into an independent self-hosted copy

**Source:** [Approved Change Spec](../spec.md), requirements I3. **Tier:** 3.

## Outcome and scope

Produce and restore a private integrity-checked recovery bundle containing data/files/release-module evidence. Rotate destination authority, invalidate sessions and prevent automatic duplicate side effects. No live customer restores, general cloud backup product or deletion.

## Touchpoints

Existing Compose backup service; management CLI; docs/self-hosting.md; new recovery manifest and disposable restore fixture. Use audited standard encryption tooling; do not invent cryptography.

## Blocked by

- [01 — Run two isolated installations from one release](01-portable-installations.md)
- [Current revamp member sessions](../../farmcore-revamp/tickets/02-member-sessions.md), so restored credentials/session invalidation are tested against the actual identity model.

## Acceptance criteria

- [ ] Backup A and restore to new C with equal semantic record/source/module evidence while B continues serving.
- [ ] Quiesce writes or use an equivalent consistent checkpoint across database and private files. A concurrent file/record update during backup restores as one coherent recovery point with every evidence reference resolving to its captured file version; independent valid hashes do not substitute for this test.
- [ ] Wrong key, modified manifest/file, truncated dump and incompatible release fail before activation; retain original A and prior recovery artifacts.
- [ ] C has new installation/agent/operator secrets and invalid prior sessions; restored jobs/deliveries stay paused until explicit activation. Document customer connector-secret recovery and exclude platform-wide credentials.

## Execution discipline

One implementation worker owns the bounded slice; other agents are not alone and must preserve peers' edits. Work only after blockers pass; inherited draft code is not a completed dependency. Use TDD through the spec-approved public CLI/HTTP/job/package seams: observed RED, minimum GREEN, review/refactor, one vertical behavior at a time. Mock only external infrastructure/providers, not internal collaborators. The integration coordinator alone authors serialized revisions; keep one linear supported release chain. No live customer data, provisioning, payments, production migrations or deployment. Independent QA reviews spec conformance and standards separately before merge. Current PRs retain their own gates.

## Verification

The named new test files are implementation deliverables, not existing test claims.

- [ ] `uv run pytest tests/instances/test_recovery.py -q` — meaningful public contracts and semantic golden comparisons pass; retain relevant existing regression assertions.
- [ ] Tier-3 `verify` with real merge base: write failure model first; property/adversarial cases plus mutation or restored isolated manual mutants; required smoke through actual CLI/HTTP/worker/containers. Record failures before fixes and skips honestly.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass; update paths for a separately packaged hosting/addon layout and explicitly record scope.
- [ ] `uv run alembic heads` — one supported head; test upgrade of populated legacy data for schema changes. Release/profile compatibility and clean restored source hashes verified.
- [ ] Fresh independent reviewer checks every criterion on frozen source and records exact SHA/profile, commands, counts, baseline and remaining limits. Final integrated full/data/browser/Linux qualification belongs to20; focused checks do not claim it.

## Evidence

Not started. No product implementation or verification claimed by publishing this ticket.
