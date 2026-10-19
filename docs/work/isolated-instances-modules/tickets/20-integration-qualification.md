---
type: Work Ticket
title: "Qualify hosted and self-hosted modular installations"
description: "Independent final spec and standards review plus real release qualification."
status: blocked
blocked_by: ["04","05","06","09","18","19","../../farmcore-revamp/tickets/12-integration-qa.md"]
---

# 20 — Qualify hosted and self-hosted modular installations

**Source:** [Approved Change Spec](../spec.md), requirements I1-I7. **Tier:** 3.

## Outcome and scope

Independent final spec and standards review plus real release qualification. Recheck the actual integrated tip; preserve earlier honest failures/skips. No real customer allocation, payment or production cutover.

## Touchpoints

All initiative acceptance criteria; CLI/HTTP/browser/container/recovery test harness; release/module manifest; setup/upgrade/support runbooks; source semantic golden comparisons.

## Blocked by

- [04 — Let a customer claim and configure their hosted installation](04-customer-onboarding.md)
- [05 — Upgrade one installation with a verified recovery path](05-safe-upgrades.md)
- [06 — Suspend, resume and archive an installation without data loss](06-suspend-retire.md)
- [09 — Dispatch committed notifications safely after crashes](09-committed-effect-dispatch.md)
- [18 — Run a weather-risk app without crop protection](18-independent-weather-risk.md)
- [19 — Confirm and correct fertilization through an optional app](19-fertilization-records.md)
- [Current revamp final integration QA](../../farmcore-revamp/tickets/12-integration-qa.md)

## Acceptance criteria

- [ ] Two independently configured installations plus hosted-to-selfhosted restored C pass identity/role/route/cookie/file/database/worker separation, with A/B availability independently observable.
- [ ] Crash/lease/mode-switch/provider/DNS/outbox/disable/upgrade/restore/suspend tests prove fencing, recovery and bounded work; final migration graph has one supported head.
- [ ] Full Python/data replay, Node, normal/reduced-motion desktop/mobile browser and Linux container/build/audit checks run on the actual release; historical exports/stock/assessment evidence and independent apps pass.
- [ ] Publish exact SHA/profile/commands/counts/baseline, manual fallbacks and remaining limits. Required failed or skipped acceptance blocks release qualification; merchant and scientific models are not advertised.

## Execution discipline

One implementation worker owns the bounded slice; other agents are not alone and must preserve peers' edits. Work only after blockers pass; inherited draft code is not a completed dependency. Use TDD through the spec-approved public CLI/HTTP/job/package seams: observed RED, minimum GREEN, review/refactor, one vertical behavior at a time. Mock only external infrastructure/providers, not internal collaborators. The integration coordinator alone authors serialized revisions; keep one linear supported release chain. No live customer data, provisioning, payments, production migrations or deployment. Independent QA reviews spec conformance and standards separately before merge. Current PRs retain their own gates.

## Verification

The named new test files are implementation deliverables, not existing test claims.

- [ ] `uv run pytest tests/instances tests/hosting tests/addons tests/core tests/apps -q` — meaningful public contracts and semantic golden comparisons pass; retain relevant existing regression assertions.
- [ ] Tier-3 `verify` with real merge base: write failure model first; property/adversarial cases plus mutation or restored isolated manual mutants; required smoke through actual CLI/HTTP/worker/containers. Record failures before fixes and skips honestly.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass; update paths for a separately packaged hosting/addon layout and explicitly record scope.
- [ ] `uv run alembic heads` — one supported head; test upgrade of populated legacy data for schema changes. Release/profile compatibility and clean restored source hashes verified.
- [ ] Fresh independent reviewer checks every criterion on frozen source and records exact SHA/profile, commands, counts, baseline and remaining limits. Final integrated full/data/browser/Linux qualification belongs to20; focused checks do not claim it.

## Evidence

Not started. No product implementation or verification claimed by publishing this ticket.
