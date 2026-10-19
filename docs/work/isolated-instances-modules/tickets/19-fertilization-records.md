---
type: Work Ticket
title: "Confirm and correct fertilization through an optional app"
description: "Deliver the first broader operation app: parcel/season/product/quantity preview, confirm/cancel/correct and stock-bearing application."
status: blocked
blocked_by: ["11","13","15","17"]
---

# 19 — Confirm and correct fertilization through an optional app

**Source:** [Approved Change Spec](../spec.md), requirements I7. **Tier:** 3.

## Outcome and scope

Deliver the first broader operation app: parcel/season/product/quantity preview, confirm/cancel/correct and stock-bearing application. Reuse confirmed record/audit/resource contracts. No nutrient-rate advice, soil model, new regulatory filing or inferred product composition.

## Touchpoints

New fertilization addon; shared records/catalog/quantity/confirmation/inventory interfaces; generic diary/map/assistant contributions; external bundle profile.

## Blocked by

- [11 — Install a separately packaged compatible FarmCore app](11-addon-contract.md)
- [13 — Retain distinct crop seasons on a stable parcel](13-crop-seasons.md)
- [15 — Preserve stock accounting through neutral quantities](15-inventory-consumer-migration.md)
- [17 — Remove mandatory crop-protection assumptions from shared core](17-neutral-core-contract.md)

## Acceptance criteria

- [ ] Authorized owner previews/confirms/cancels/corrects entered fertilizer facts with original unit/composition provenance and preserved original values/reason/actor.
- [ ] Missing area/composition/density prompts an explicit unknown/clarification rather than invented conversion or nutrient recommendation.
- [ ] Planned work never decrements stock; confirmed applications and correction deltas act once under retry/concurrency/rollback. Farm/instance/module authority is enforced on every entry point.

## Execution discipline

One implementation worker owns the bounded slice; other agents are not alone and must preserve peers' edits. Work only after blockers pass; inherited draft code is not a completed dependency. Use TDD through the spec-approved public CLI/HTTP/job/package seams: observed RED, minimum GREEN, review/refactor, one vertical behavior at a time. Mock only external infrastructure/providers, not internal collaborators. The integration coordinator alone authors serialized revisions; keep one linear supported release chain. No live customer data, provisioning, payments, production migrations or deployment. Independent QA reviews spec conformance and standards separately before merge. Current PRs retain their own gates.

## Verification

The named new test files are implementation deliverables, not existing test claims.

- [ ] `uv run pytest tests/addons/test_fertilization.py tests/instances/test_fertilization_profile.py -q` — meaningful public contracts and semantic golden comparisons pass; retain relevant existing regression assertions.
- [ ] Tier-3 `verify` with real merge base: write failure model first; property/adversarial cases plus mutation or restored isolated manual mutants; required smoke through actual CLI/HTTP/worker/containers. Record failures before fixes and skips honestly.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass; update paths for a separately packaged hosting/addon layout and explicitly record scope.
- [ ] `uv run alembic heads` — one supported head; test upgrade of populated legacy data for schema changes. Release/profile compatibility and clean restored source hashes verified.
- [ ] Fresh independent reviewer checks every criterion on frozen source and records exact SHA/profile, commands, counts, baseline and remaining limits. Final integrated full/data/browser/Linux qualification belongs to20; focused checks do not claim it.

## Evidence

Not started. No product implementation or verification claimed by publishing this ticket.
