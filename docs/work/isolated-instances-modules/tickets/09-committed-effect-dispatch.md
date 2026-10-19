---
type: Work Ticket
title: "Dispatch committed notifications safely after crashes"
description: "Persist notification/delivery intent with its source transaction and dispatch through durable attempts."
status: blocked
blocked_by: ["06","08","../../farmcore-revamp/tickets/11-job-completion.md"]
---

# 09 — Dispatch committed notifications safely after crashes

**Source:** [Approved Change Spec](../spec.md), requirements I4. **Tier:** 3.

## Outcome and scope

Persist notification/delivery intent with its source transaction and dispatch through durable attempts. Establish the shared admission/fencing interface using the existing installation suspend/clone/clock generations. Module10 extends this interface for module-disable races; that later behavior is not a09 completion criterion. Internal record/stock identity remains in domain writes. No universal event-sourcing rewrite or exactly-once external transport claim.

## Touchpoints

core timeline/transaction seams; notifications/service.py; provider transport adapters; durable worker; new outbox/delivery models and coordinator revision.

## Blocked by

- [06 — Suspend, resume and archive an installation without data loss](06-suspend-retire.md), providing the installation lifecycle state used by admission checks.
- [08 — Move station and assessment execution onto durable tasks](08-durable-job-adoption.md)
- [Current revamp job completion](../../farmcore-revamp/tickets/11-job-completion.md)

## Acceptance criteria

- [ ] Rollback creates neither delivery intent nor delivered effect; commit then worker crash leaves recoverable intent.
- [ ] Duplicate/retried claims do not duplicate internal operations or stock; supported provider idempotency/receipts are honored.
- [ ] Lost acknowledgement/ambiguous provider outcome is visible for reconciliation; suspension/restored-clone/clock fences prevent stale dispatch through the shared admission interface. Module-disable dispatch races are acceptance work in10.

## Execution discipline

One implementation worker owns the bounded slice; other agents are not alone and must preserve peers' edits. Work only after blockers pass; inherited draft code is not a completed dependency. Use TDD through the spec-approved public CLI/HTTP/job/package seams: observed RED, minimum GREEN, review/refactor, one vertical behavior at a time. Mock only external infrastructure/providers, not internal collaborators. The integration coordinator alone authors serialized revisions; keep one linear supported release chain. No live customer data, provisioning, payments, production migrations or deployment. Independent QA reviews spec conformance and standards separately before merge. Current PRs retain their own gates.

## Verification

The named new test files are implementation deliverables, not existing test claims.

- [ ] `uv run pytest tests/apps/notifications/test_outbox.py tests/core/test_effect_recovery.py -q` — meaningful public contracts and semantic golden comparisons pass; retain relevant existing regression assertions.
- [ ] Tier-3 `verify` with real merge base: write failure model first; property/adversarial cases plus mutation or restored isolated manual mutants; required smoke through actual CLI/HTTP/worker/containers. Record failures before fixes and skips honestly.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass; update paths for a separately packaged hosting/addon layout and explicitly record scope.
- [ ] `uv run alembic heads` — one supported head; test upgrade of populated legacy data for schema changes. Release/profile compatibility and clean restored source hashes verified.
- [ ] Fresh independent reviewer checks every criterion on frozen source and records exact SHA/profile, commands, counts, baseline and remaining limits. Final integrated full/data/browser/Linux qualification belongs to20; focused checks do not claim it.

## Evidence

Not started. No product implementation or verification claimed by publishing this ticket.
