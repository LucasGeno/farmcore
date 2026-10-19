---
type: Work Ticket
title: "Make module activation and disablement effective across processes"
description: "Version installation module profiles and validate dependencies/settings/authority."
status: blocked
blocked_by: ["08","09"]
---

# 10 — Make module activation and disablement effective across processes

**Source:** [Approved Change Spec](../spec.md), requirements I5. **Tier:** 3.

## Outcome and scope

Version installation module profiles and validate dependencies/settings/authority. Deliver the instance administrator's reviewed profile/settings configuration flow, completing the module configuration deferred from04. Make acknowledged disablement gate routes, mutations, tools and task/effect publication/claims across web/worker processes while retaining authorized historical data. Code/schema installation remains a controlled release/restart.

## Touchpoints

core/manifest.py; core/registry.py; AppModule; routes_apps.py; access/mutation and worker admission; app widgets/tools; settings schemas.

## Blocked by

- [08 — Move station and assessment execution onto durable tasks](08-durable-job-adoption.md)
- [09 — Dispatch committed notifications safely after crashes](09-committed-effect-dispatch.md)

## Acceptance criteria

- [ ] UI, direct HTTP, assistant tools and a second process agree on a disabled module; an in-flight old attempt cannot publish after acknowledged disable.
- [ ] Extend09's admission interface with module-profile generation: a concurrently disabling module fences queued and in-flight notification/outbox dispatch. The customer profile/settings flow rejects foreign installations, unapproved builds and unauthorized farm access.
- [ ] Missing/incompatible/cyclic dependencies and invalid settings fail without partial activation or domain writes.
- [ ] Disable/re-enable retains records/history/exports and does not reseed duplicates; a permitted retention path remains authorized while active functionality is unavailable.

## Execution discipline

One implementation worker owns the bounded slice; other agents are not alone and must preserve peers' edits. Work only after blockers pass; inherited draft code is not a completed dependency. Use TDD through the spec-approved public CLI/HTTP/job/package seams: observed RED, minimum GREEN, review/refactor, one vertical behavior at a time. Mock only external infrastructure/providers, not internal collaborators. The integration coordinator alone authors serialized revisions; keep one linear supported release chain. No live customer data, provisioning, payments, production migrations or deployment. Independent QA reviews spec conformance and standards separately before merge. Current PRs retain their own gates.

## Verification

The named new test files are implementation deliverables, not existing test claims.

- [ ] `uv run pytest tests/core/test_module_lifecycle.py tests/core/test_apps_routes.py tests/core/test_registry.py -q` — meaningful public contracts and semantic golden comparisons pass; retain relevant existing regression assertions.
- [ ] Tier-3 `verify` with real merge base: write failure model first; property/adversarial cases plus mutation or restored isolated manual mutants; required smoke through actual CLI/HTTP/worker/containers. Record failures before fixes and skips honestly.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass; update paths for a separately packaged hosting/addon layout and explicitly record scope.
- [ ] `uv run alembic heads` — one supported head; test upgrade of populated legacy data for schema changes. Release/profile compatibility and clean restored source hashes verified.
- [ ] Fresh independent reviewer checks every criterion on frozen source and records exact SHA/profile, commands, counts, baseline and remaining limits. Final integrated full/data/browser/Linux qualification belongs to20; focused checks do not claim it.

## Evidence

Not started. No product implementation or verification claimed by publishing this ticket.
