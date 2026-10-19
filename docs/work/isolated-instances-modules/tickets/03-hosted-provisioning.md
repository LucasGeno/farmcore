---
type: Work Ticket
title: "Provision one customer installation safely through a management command"
description: "Introduce the separate optional hosting-manager/lifecycle-agent tracer bullet: authenticated operator command, durable allocation state, server-generated identity and narrow Compose provisioning."
status: blocked
blocked_by: ["01","02","../../farmcore-revamp/tickets/02-member-sessions.md"]
---

# 03 — Provision one customer installation safely through a management command

**Source:** [Approved Change Spec](../spec.md), requirements I1,I2. **Tier:** 3.

## Outcome and scope

Introduce the separate optional hosting-manager/lifecycle-agent tracer bullet: authenticated operator command, durable allocation state, server-generated identity and narrow Compose provisioning. Customer portal and payments are excluded.

## Touchpoints

New separately deployed hosting package/CLI and control database; deployment adapter; existing identity bootstrap CLI after revamp02. The portal must never receive the Docker socket or database-provisioning credential.

## Blocked by

- [01 — Run two isolated installations from one release](01-portable-installations.md)
- [02 — Restore an installation into an independent self-hosted copy](02-portable-recovery.md)
- [Current revamp member identities](../../farmcore-revamp/tickets/02-member-sessions.md)

## Acceptance criteria

- [ ] Provisioning retries/concurrent calls/crash recovery yield one installation or a recoverable failed state, with only owned allocations eligible for cleanup.
- [ ] Hostile customer names, domains, paths and resource IDs cannot inject commands, escape directories or select another customer's resources.
- [ ] Customer runtime credentials cannot provision databases/host resources; allocated identity/profile/release/limits and operator actions are audited. Custom domains activate only after ownership verification and allowlisted mapping.

## Execution discipline

One implementation worker owns the bounded slice; other agents are not alone and must preserve peers' edits. Work only after blockers pass; inherited draft code is not a completed dependency. Use TDD through the spec-approved public CLI/HTTP/job/package seams: observed RED, minimum GREEN, review/refactor, one vertical behavior at a time. Mock only external infrastructure/providers, not internal collaborators. The integration coordinator alone authors serialized revisions; keep one linear supported release chain. No live customer data, provisioning, payments, production migrations or deployment. Independent QA reviews spec conformance and standards separately before merge. Current PRs retain their own gates.

## Verification

The named new test files are implementation deliverables, not existing test claims.

- [ ] `uv run pytest tests/hosting/test_provisioning.py tests/hosting/test_resource_authority.py -q` — meaningful public contracts and semantic golden comparisons pass; retain relevant existing regression assertions.
- [ ] Tier-3 `verify` with real merge base: write failure model first; property/adversarial cases plus mutation or restored isolated manual mutants; required smoke through actual CLI/HTTP/worker/containers. Record failures before fixes and skips honestly.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass; update paths for a separately packaged hosting/addon layout and explicitly record scope.
- [ ] `uv run alembic heads` — one supported head; test upgrade of populated legacy data for schema changes. Release/profile compatibility and clean restored source hashes verified.
- [ ] Fresh independent reviewer checks every criterion on frozen source and records exact SHA/profile, commands, counts, baseline and remaining limits. Final integrated full/data/browser/Linux qualification belongs to20; focused checks do not claim it.

## Evidence

Not started. No product implementation or verification claimed by publishing this ticket.
