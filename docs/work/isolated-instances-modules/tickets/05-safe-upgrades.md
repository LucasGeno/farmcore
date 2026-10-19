---
type: Work Ticket
title: "Upgrade one installation with a verified recovery path"
description: "Deliver release/profile compatibility checks, pre-upgrade backup, one designated migrator, maintenance/failure state and tested restore recovery."
status: blocked
blocked_by: ["02","03"]
---

# 05 — Upgrade one installation with a verified recovery path

**Source:** [Approved Change Spec](../spec.md), requirements I3. **Tier:** 3.

## Outcome and scope

Deliver release/profile compatibility checks, pre-upgrade backup, one designated migrator, maintenance/failure state and tested restore recovery. No production upgrades or claim that image rollback reverses schema.

## Touchpoints

Hosting lifecycle command; pinned release/profile metadata; Alembic invocation; app/worker startup ordering; operational runbook.

## Blocked by

- [02 — Restore an installation into an independent self-hosted copy](02-portable-recovery.md)
- [03 — Provision one customer installation safely through a management command](03-hosted-provisioning.md)

## Acceptance criteria

- [ ] Concurrent upgrade requests execute one migration; workers and web do not independently race schema upgrades.
- [ ] Inject preflight, migration and post-start failures; A remains recoverable through a verified restore while B keeps serving.
- [ ] Unsupported module/core versions fail before migration; status distinguishes failed upgrade from healthy rollback and states the actual schema/release in use.

## Execution discipline

One implementation worker owns the bounded slice; other agents are not alone and must preserve peers' edits. Work only after blockers pass; inherited draft code is not a completed dependency. Use TDD through the spec-approved public CLI/HTTP/job/package seams: observed RED, minimum GREEN, review/refactor, one vertical behavior at a time. Mock only external infrastructure/providers, not internal collaborators. The integration coordinator alone authors serialized revisions; keep one linear supported release chain. No live customer data, provisioning, payments, production migrations or deployment. Independent QA reviews spec conformance and standards separately before merge. Current PRs retain their own gates.

## Verification

The named new test files are implementation deliverables, not existing test claims.

- [ ] `uv run pytest tests/hosting/test_upgrades.py tests/instances/test_upgrade_recovery.py -q` — meaningful public contracts and semantic golden comparisons pass; retain relevant existing regression assertions.
- [ ] Tier-3 `verify` with real merge base: write failure model first; property/adversarial cases plus mutation or restored isolated manual mutants; required smoke through actual CLI/HTTP/worker/containers. Record failures before fixes and skips honestly.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass; update paths for a separately packaged hosting/addon layout and explicitly record scope.
- [ ] `uv run alembic heads` — one supported head; test upgrade of populated legacy data for schema changes. Release/profile compatibility and clean restored source hashes verified.
- [ ] Fresh independent reviewer checks every criterion on frozen source and records exact SHA/profile, commands, counts, baseline and remaining limits. Final integrated full/data/browser/Linux qualification belongs to20; focused checks do not claim it.

## Evidence

Not started. No product implementation or verification claimed by publishing this ticket.
