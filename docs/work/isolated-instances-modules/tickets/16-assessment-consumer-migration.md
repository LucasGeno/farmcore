---
type: Work Ticket
title: "Keep crop-protection assessments typed and reproducible"
description: "Migrate crop-protection assessment/catalog consumers to the module-owned typed result and shared evidence envelope."
status: blocked
blocked_by: ["11","12","13"]
---

# 16 — Keep crop-protection assessments typed and reproducible

**Source:** [Approved Change Spec](../spec.md), requirements I6. **Tier:** 3.

## Outcome and scope

Migrate crop-protection assessment/catalog consumers to the module-owned typed result and shared evidence envelope. Preserve the current reviewed parameters/recommendation review contracts and every legacy decision meaning. No irrigation/nutrient result mapped to spray enums.

## Touchpoints

apps/advisor engine/service/models/routes; reviewed config/review/evidence from revamp05/06; forecast/planning/assistant read adapters; coordinator revision.

## Blocked by

- [11 — Install a separately packaged compatible FarmCore app](11-addon-contract.md)
- [12 — Add a neutral catalog and typed quantities beside spray data](12-catalog-quantity-expand.md)
- [13 — Retain distinct crop seasons on a stable parcel](13-crop-seasons.md)

## Acceptance criteria

- [ ] Frozen numerical/replay examples and legacy spray/wait/protected/none outputs retain their meaning, sources and public representation.
- [ ] Stored parameter version, input snapshot and human review references remain historical and reproducible across activation/migration.
- [ ] Missing/unsupported inputs stay limited/unavailable; new generic envelope supports a different typed module result without relaxing crop-protection validation.

## Execution discipline

One implementation worker owns the bounded slice; other agents are not alone and must preserve peers' edits. Work only after blockers pass; inherited draft code is not a completed dependency. Use TDD through the spec-approved public CLI/HTTP/job/package seams: observed RED, minimum GREEN, review/refactor, one vertical behavior at a time. Mock only external infrastructure/providers, not internal collaborators. The integration coordinator alone authors serialized revisions; keep one linear supported release chain. No live customer data, provisioning, payments, production migrations or deployment. Independent QA reviews spec conformance and standards separately before merge. Current PRs retain their own gates.

## Verification

The named new test files are implementation deliverables, not existing test claims.

- [ ] `uv run pytest tests/apps/advisor tests/core/test_assessment_compatibility.py -q` — meaningful public contracts and semantic golden comparisons pass; retain relevant existing regression assertions.
- [ ] Tier-3 `verify` with real merge base: write failure model first; property/adversarial cases plus mutation or restored isolated manual mutants; required smoke through actual CLI/HTTP/worker/containers. Record failures before fixes and skips honestly.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass; update paths for a separately packaged hosting/addon layout and explicitly record scope.
- [ ] `uv run alembic heads` — one supported head; test upgrade of populated legacy data for schema changes. Release/profile compatibility and clean restored source hashes verified.
- [ ] Fresh independent reviewer checks every criterion on frozen source and records exact SHA/profile, commands, counts, baseline and remaining limits. Final integrated full/data/browser/Linux qualification belongs to20; focused checks do not claim it.

## Evidence

Not started. No product implementation or verification claimed by publishing this ticket.
