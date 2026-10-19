---
type: Work Ticket
title: "Suspend, resume and archive an installation without data loss"
description: "Implement bounded reversible suspension/resume and archive/export retirement with explicit customer/operator authority."
status: blocked
blocked_by: ["02","03","04"]
---

# 06 — Suspend, resume and archive an installation without data loss

**Source:** [Approved Change Spec](../spec.md), requirements I3. **Tier:** 3.

## Outcome and scope

Implement bounded reversible suspension/resume and archive/export retirement with explicit customer/operator authority. Retain protected recovery data. Permanent deletion and nonpayment automation are excluded.

## Touchpoints

Hosting lifecycle state/agent; runtime task admission/routing health; customer portal status/recovery action; later worker-fencing contract must be adopted before final QA.

## Blocked by

- [02 — Restore an installation into an independent self-hosted copy](02-portable-recovery.md)
- [03 — Provision one customer installation safely through a management command](03-hosted-provisioning.md)
- [04 — Let a customer claim and configure their hosted installation](04-customer-onboarding.md)

## Acceptance criteria

- [ ] Repeated suspend/resume/retire calls converge without deleting records or another customer's resources.
- [ ] Suspended/archived instances cannot perform new customer mutations or external deliveries; authorized recovery/export remains possible.
- [ ] Resume respects actual release/profile and current clock/module state; queued stale effects are not blindly replayed. Audit actor/reason/state and expose retention ownership.

## Execution discipline

One implementation worker owns the bounded slice; other agents are not alone and must preserve peers' edits. Work only after blockers pass; inherited draft code is not a completed dependency. Use TDD through the spec-approved public CLI/HTTP/job/package seams: observed RED, minimum GREEN, review/refactor, one vertical behavior at a time. Mock only external infrastructure/providers, not internal collaborators. The integration coordinator alone authors serialized revisions; keep one linear supported release chain. No live customer data, provisioning, payments, production migrations or deployment. Independent QA reviews spec conformance and standards separately before merge. Current PRs retain their own gates.

## Verification

The named new test files are implementation deliverables, not existing test claims.

- [ ] `uv run pytest tests/hosting/test_lifecycle.py -q` — meaningful public contracts and semantic golden comparisons pass; retain relevant existing regression assertions.
- [ ] Tier-3 `verify` with real merge base: write failure model first; property/adversarial cases plus mutation or restored isolated manual mutants; required smoke through actual CLI/HTTP/worker/containers. Record failures before fixes and skips honestly.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass; update paths for a separately packaged hosting/addon layout and explicitly record scope.
- [ ] `uv run alembic heads` — one supported head; test upgrade of populated legacy data for schema changes. Release/profile compatibility and clean restored source hashes verified.
- [ ] Fresh independent reviewer checks every criterion on frozen source and records exact SHA/profile, commands, counts, baseline and remaining limits. Final integrated full/data/browser/Linux qualification belongs to20; focused checks do not claim it.

## Evidence

Not started. No product implementation or verification claimed by publishing this ticket.
