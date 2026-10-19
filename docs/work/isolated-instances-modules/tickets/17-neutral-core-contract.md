---
type: Work Ticket
title: "Remove mandatory crop-protection assumptions from shared core"
description: "Contract old shared fungicide restrictions only after all consumers migrated."
status: blocked
blocked_by: ["14","15","16"]
---

# 17 — Remove mandatory crop-protection assumptions from shared core

**Source:** [Approved Change Spec](../spec.md), requirements I6. **Tier:** 3.

## Outcome and scope

Contract old shared fungicide restrictions only after all consumers migrated. Preserve explicit versioned legacy adapters and authorized historical reads. This is the final bounded expand–migrate–contract step, not a whole-framework rewrite.

## Touchpoints

core/models/canonical.py shared restrictions; module-owned product/assessment types; required app/registry dependencies; migrations; external API compatibility documentation.

## Blocked by

- [14 — Move records and capture onto neutral catalog contracts](14-record-consumer-migration.md)
- [15 — Preserve stock accounting through neutral quantities](15-inventory-consumer-migration.md)
- [16 — Keep crop-protection assessments typed and reproducible](16-assessment-consumer-migration.md)

## Acceptance criteria

- [ ] Static consumer inventory and public tests show no active shared-core requirement for contact/penetrant/downy/powdery or crop-protection decisions.
- [ ] A release/profile without crop protection starts, navigates parcels/records and retains typed generic catalog/quantity behavior.
- [ ] Legacy datasets migrate through the supported linear chain with matching evidence/exports/stock and no loss; published compatibility/deprecation rules are explicit and exercised.

## Execution discipline

One implementation worker owns the bounded slice; other agents are not alone and must preserve peers' edits. Work only after blockers pass; inherited draft code is not a completed dependency. Use TDD through the spec-approved public CLI/HTTP/job/package seams: observed RED, minimum GREEN, review/refactor, one vertical behavior at a time. Mock only external infrastructure/providers, not internal collaborators. The integration coordinator alone authors serialized revisions; keep one linear supported release chain. No live customer data, provisioning, payments, production migrations or deployment. Independent QA reviews spec conformance and standards separately before merge. Current PRs retain their own gates.

## Verification

The named new test files are implementation deliverables, not existing test claims.

- [ ] `uv run pytest tests/core/test_neutral_core.py tests/core/test_schema.py tests/core/test_catalog_compatibility.py tests/core/test_assessment_compatibility.py -q` — meaningful public contracts and semantic golden comparisons pass; retain relevant existing regression assertions.
- [ ] Tier-3 `verify` with real merge base: write failure model first; property/adversarial cases plus mutation or restored isolated manual mutants; required smoke through actual CLI/HTTP/worker/containers. Record failures before fixes and skips honestly.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass; update paths for a separately packaged hosting/addon layout and explicitly record scope.
- [ ] `uv run alembic heads` — one supported head; test upgrade of populated legacy data for schema changes. Release/profile compatibility and clean restored source hashes verified.
- [ ] Fresh independent reviewer checks every criterion on frozen source and records exact SHA/profile, commands, counts, baseline and remaining limits. Final integrated full/data/browser/Linux qualification belongs to20; focused checks do not claim it.

## Evidence

Not started. No product implementation or verification claimed by publishing this ticket.
