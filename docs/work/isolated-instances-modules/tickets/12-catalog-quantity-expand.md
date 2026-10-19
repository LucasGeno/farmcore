---
type: Work Ticket
title: "Add a neutral catalog and typed quantities beside spray data"
description: "Expand the model with generic product/quantity contracts and a typed crop-protection extension while keeping current consumers valid through adapters."
status: blocked
blocked_by: ["../../farmcore-revamp/tickets/12-integration-qa.md"]
---

# 12 — Add a neutral catalog and typed quantities beside spray data

**Source:** [Approved Change Spec](../spec.md), requirements I6. **Tier:** 3.

## Outcome and scope

Expand the model with generic product/quantity contracts and a typed crop-protection extension while keeping current consumers valid through adapters. This is additive; consumer migration and removal are later tickets.

## Touchpoints

core/models/canonical.py Product/Quantity; app-owned crop-protection attributes; unit validation/catalog interface; serialized adapters; coordinator revision. Establish semantic golden fixtures before migration.

## Blocked by

- [Current revamp final integration QA](../../farmcore-revamp/tickets/12-integration-qa.md)

## Acceptance criteria

- [ ] Legacy products/quantities retain identity and serialized/export meaning; current spray workflows still pass unchanged.
- [ ] Typed mass/volume/area/compatible rates retain entered units and provenance; invalid dimensions and absent density/area/composition cannot be silently converted.
- [ ] Crop-protection targets/contact behavior remain validated in their extension; generic products can exist without false mildew attributes. Migration is additive and supports current installed data.

## Execution discipline

One implementation worker owns the bounded slice; other agents are not alone and must preserve peers' edits. Work only after blockers pass; inherited draft code is not a completed dependency. Use TDD through the spec-approved public CLI/HTTP/job/package seams: observed RED, minimum GREEN, review/refactor, one vertical behavior at a time. Mock only external infrastructure/providers, not internal collaborators. The integration coordinator alone authors serialized revisions; keep one linear supported release chain. No live customer data, provisioning, payments, production migrations or deployment. Independent QA reviews spec conformance and standards separately before merge. Current PRs retain their own gates.

## Verification

The named new test files are implementation deliverables, not existing test claims.

- [ ] `uv run pytest tests/core/test_catalog_compatibility.py tests/core/test_quantity_dimensions.py tests/core/test_schema.py -q` — meaningful public contracts and semantic golden comparisons pass; retain relevant existing regression assertions.
- [ ] Tier-3 `verify` with real merge base: write failure model first; property/adversarial cases plus mutation or restored isolated manual mutants; required smoke through actual CLI/HTTP/worker/containers. Record failures before fixes and skips honestly.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass; update paths for a separately packaged hosting/addon layout and explicitly record scope.
- [ ] `uv run alembic heads` — one supported head; test upgrade of populated legacy data for schema changes. Release/profile compatibility and clean restored source hashes verified.
- [ ] Fresh independent reviewer checks every criterion on frozen source and records exact SHA/profile, commands, counts, baseline and remaining limits. Final integrated full/data/browser/Linux qualification belongs to20; focused checks do not claim it.

## Evidence

Not started. No product implementation or verification claimed by publishing this ticket.
