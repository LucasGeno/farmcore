---
type: Work Ticket
title: "Retain distinct crop seasons on a stable parcel"
description: "Add explicit dated crop-season identity and owner-authorized configuration without rewriting parcel geometry/history."
status: blocked
blocked_by: ["../../farmcore-revamp/tickets/12-integration-qa.md"]
---

# 13 — Retain distinct crop seasons on a stable parcel

**Source:** [Approved Change Spec](../spec.md), requirements I6. **Tier:** 3.

## Outcome and scope

Add explicit dated crop-season identity and owner-authorized configuration without rewriting parcel geometry/history. Preserve legacy unknown context. No phenology prediction, new disease model or forced single annual crop.

## Touchpoints

Canonical parcel relationships; new season context model/configuration HTTP; field metadata/views; existing assessment evidence adapters; coordinator revision.

## Blocked by

- [Current revamp final integration QA](../../farmcore-revamp/tickets/12-integration-qa.md)

## Acceptance criteria

- [ ] The same parcel can retain distinct season/crop contexts; editing current context leaves prior records/assessments unchanged.
- [ ] Unknown crop/date/phenology is explicit; conflicting/stale edits and cross-farm selectors are rejected. Explicit mixed/overlapping contexts are not silently collapsed.
- [ ] Legacy parcels/records remain readable and exportable without invented season facts; current vineyard behavior is preserved.

## Execution discipline

One implementation worker owns the bounded slice; other agents are not alone and must preserve peers' edits. Work only after blockers pass; inherited draft code is not a completed dependency. Use TDD through the spec-approved public CLI/HTTP/job/package seams: observed RED, minimum GREEN, review/refactor, one vertical behavior at a time. Mock only external infrastructure/providers, not internal collaborators. The integration coordinator alone authors serialized revisions; keep one linear supported release chain. No live customer data, provisioning, payments, production migrations or deployment. Independent QA reviews spec conformance and standards separately before merge. Current PRs retain their own gates.

## Verification

The named new test files are implementation deliverables, not existing test claims.

- [ ] `uv run pytest tests/apps/fields/test_crop_seasons.py tests/core/test_schema.py -q` — meaningful public contracts and semantic golden comparisons pass; retain relevant existing regression assertions.
- [ ] Tier-3 `verify` with real merge base: write failure model first; property/adversarial cases plus mutation or restored isolated manual mutants; required smoke through actual CLI/HTTP/worker/containers. Record failures before fixes and skips honestly.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass; update paths for a separately packaged hosting/addon layout and explicitly record scope.
- [ ] `uv run alembic heads` — one supported head; test upgrade of populated legacy data for schema changes. Release/profile compatibility and clean restored source hashes verified.
- [ ] Fresh independent reviewer checks every criterion on frozen source and records exact SHA/profile, commands, counts, baseline and remaining limits. Final integrated full/data/browser/Linux qualification belongs to20; focused checks do not claim it.

## Evidence

Not started. No product implementation or verification claimed by publishing this ticket.
