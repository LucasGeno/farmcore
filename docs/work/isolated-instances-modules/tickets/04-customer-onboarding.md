---
type: Work Ticket
title: "Let a customer claim and configure their hosted installation"
description: "Deliver a minimal authenticated customer portal showing owned installations and one-use owner setup."
status: blocked
blocked_by: ["03","../../farmcore-revamp/tickets/03-authenticated-sharing.md"]
---

# 04 — Let a customer claim and configure their hosted installation

**Source:** [Approved Change Spec](../spec.md), requirements I2. **Tier:** 3.

## Outcome and scope

Deliver a minimal authenticated customer portal showing owned installations and one-use owner setup. Reuse the runtime's available farm/member/timezone and connector setup within instance-administrator permissions. Module-profile selection/configuration is delivered in10 after its lifecycle contract exists. Manual operator-approved customer allocation only; no checkout, subscriptions or arbitrary code uploads.

## Touchpoints

Hosting portal HTTP/session routes and control data; runtime public sign-in/bootstrap; already-supported farm/member/timezone and connector setup entry points. Customer/control identity and runtime identities are distinct; module-profile configuration is not a04 completion claim.

## Blocked by

- [03 — Provision one customer installation safely through a management command](03-hosted-provisioning.md)
- [Current revamp authenticated sharing](../../farmcore-revamp/tickets/03-authenticated-sharing.md)

## Acceptance criteria

- [ ] Expired/replayed/bootstrap tokens fail, are not logged, and cannot claim another installation; owner claim is atomic.
- [ ] Customer A cannot enumerate or configure B; host/cookie replay and CSRF tests deny access. Instance administration does not implicitly read unassigned farm records.
- [ ] A customer can complete setup, sign in to their installation and configure timezone/farms/members through authorized runtime flows; the same setup is available locally to self-hosters.

## Execution discipline

One implementation worker owns the bounded slice; other agents are not alone and must preserve peers' edits. Work only after blockers pass; inherited draft code is not a completed dependency. Use TDD through the spec-approved public CLI/HTTP/job/package seams: observed RED, minimum GREEN, review/refactor, one vertical behavior at a time. Mock only external infrastructure/providers, not internal collaborators. The integration coordinator alone authors serialized revisions; keep one linear supported release chain. No live customer data, provisioning, payments, production migrations or deployment. Independent QA reviews spec conformance and standards separately before merge. Current PRs retain their own gates.

## Verification

The named new test files are implementation deliverables, not existing test claims.

- [ ] `uv run pytest tests/hosting/test_customer_portal.py tests/instances/test_owner_setup.py -q` — meaningful public contracts and semantic golden comparisons pass; retain relevant existing regression assertions.
- [ ] Tier-3 `verify` with real merge base: write failure model first; property/adversarial cases plus mutation or restored isolated manual mutants; required smoke through actual CLI/HTTP/worker/containers. Record failures before fixes and skips honestly.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass; update paths for a separately packaged hosting/addon layout and explicitly record scope.
- [ ] `uv run alembic heads` — one supported head; test upgrade of populated legacy data for schema changes. Release/profile compatibility and clean restored source hashes verified.
- [ ] Fresh independent reviewer checks every criterion on frozen source and records exact SHA/profile, commands, counts, baseline and remaining limits. Final integrated full/data/browser/Linux qualification belongs to20; focused checks do not claim it.

## Evidence

Not started. No product implementation or verification claimed by publishing this ticket.
