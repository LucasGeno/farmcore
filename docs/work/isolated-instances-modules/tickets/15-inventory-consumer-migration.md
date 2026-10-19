---
type: Work Ticket
title: "Preserve stock accounting through neutral quantities"
description: "Migrate inventory reads and confirmed/corrected application effects to neutral catalog/quantity contracts."
status: blocked
blocked_by: ["12","14"]
---

# 15 — Preserve stock accounting through neutral quantities

**Source:** [Approved Change Spec](../spec.md), requirements I6. **Tier:** 3.

## Outcome and scope

Migrate inventory reads and confirmed/corrected application effects to neutral catalog/quantity contracts. Keep existing computed/transactional accounting semantics and introduce no new domain applications.

## Touchpoints

apps/inventory; quantity/catalog adapter; confirmed correction/completion contracts; record/import idempotency. Use real competing sessions for concurrency.

## Blocked by

- [12 — Add a neutral catalog and typed quantities beside spray data](12-catalog-quantity-expand.md)
- [14 — Move records and capture onto neutral catalog contracts](14-record-consumer-migration.md)

## Acceptance criteria

- [ ] Historical stock and correction deltas match semantic baseline before/after migration, including existing spray source merges.
- [ ] Planned/pending/canceled work and observations do not consume stock; retries and concurrent confirmations apply one confirmed delta.
- [ ] Incompatible dimensions or unknown conversion factors cannot produce apparently valid stock; foreign-farm requests cannot reveal or mutate balances.

## Execution discipline

One implementation worker owns the bounded slice; other agents are not alone and must preserve peers' edits. Work only after blockers pass; inherited draft code is not a completed dependency. Use TDD through the spec-approved public CLI/HTTP/job/package seams: observed RED, minimum GREEN, review/refactor, one vertical behavior at a time. Mock only external infrastructure/providers, not internal collaborators. The integration coordinator alone authors serialized revisions; keep one linear supported release chain. No live customer data, provisioning, payments, production migrations or deployment. Independent QA reviews spec conformance and standards separately before merge. Current PRs retain their own gates.

## Verification

The named new test files are implementation deliverables, not existing test claims.

- [ ] `uv run pytest tests/apps/inventory tests/core/test_inventory_catalog_migration.py -q` — meaningful public contracts and semantic golden comparisons pass; retain relevant existing regression assertions.
- [ ] Tier-3 `verify` with real merge base: write failure model first; property/adversarial cases plus mutation or restored isolated manual mutants; required smoke through actual CLI/HTTP/worker/containers. Record failures before fixes and skips honestly.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass; update paths for a separately packaged hosting/addon layout and explicitly record scope.
- [ ] `uv run alembic heads` — one supported head; test upgrade of populated legacy data for schema changes. Release/profile compatibility and clean restored source hashes verified.
- [ ] Fresh independent reviewer checks every criterion on frozen source and records exact SHA/profile, commands, counts, baseline and remaining limits. Final integrated full/data/browser/Linux qualification belongs to20; focused checks do not claim it.

## Evidence

Not started. No product implementation or verification claimed by publishing this ticket.
