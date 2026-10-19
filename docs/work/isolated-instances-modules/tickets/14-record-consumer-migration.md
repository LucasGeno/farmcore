---
type: Work Ticket
title: "Move records and capture onto neutral catalog contracts"
description: "Migrate operations/register/import and confirmed capture consumers to neutral product/quantity interfaces, preserving their public behavior and per-type source merge policy."
status: blocked
blocked_by: ["11","12"]
---

# 14 — Move records and capture onto neutral catalog contracts

**Source:** [Approved Change Spec](../spec.md), requirements I6. **Tier:** 3.

## Outcome and scope

Migrate operations/register/import and confirmed capture consumers to neutral product/quantity interfaces, preserving their public behavior and per-type source merge policy. Inventory and assessment consumers remain compatible through expand adapters.

## Touchpoints

apps/operations; apps/chat/assistant confirmed workflow; core/logs.py; import/export serializers; current revamp correction/completion interfaces. Do not make all record types share the spray 12-hour merge rule.

## Blocked by

- [11 — Install a separately packaged compatible FarmCore app](11-addon-contract.md)
- [12 — Add a neutral catalog and typed quantities beside spray data](12-catalog-quantity-expand.md)

## Acceptance criteria

- [ ] Spray, existing observation and irrigation preview/confirm/cancel/correct/complete golden journeys retain semantic outputs and source identity.
- [ ] Retry/cross-farm/rollback and canonical source merge contracts remain intact; register/export compatibility is preserved.
- [ ] Records/diary can represent non-spray facts without fabricated plant-protection fields; remaining old consumers continue working through the adapter.

## Execution discipline

One implementation worker owns the bounded slice; other agents are not alone and must preserve peers' edits. Work only after blockers pass; inherited draft code is not a completed dependency. Use TDD through the spec-approved public CLI/HTTP/job/package seams: observed RED, minimum GREEN, review/refactor, one vertical behavior at a time. Mock only external infrastructure/providers, not internal collaborators. The integration coordinator alone authors serialized revisions; keep one linear supported release chain. No live customer data, provisioning, payments, production migrations or deployment. Independent QA reviews spec conformance and standards separately before merge. Current PRs retain their own gates.

## Verification

The named new test files are implementation deliverables, not existing test claims.

- [ ] `uv run pytest tests/apps/operations tests/apps/chat tests/core/test_record_catalog_migration.py -q` — meaningful public contracts and semantic golden comparisons pass; retain relevant existing regression assertions.
- [ ] Tier-3 `verify` with real merge base: write failure model first; property/adversarial cases plus mutation or restored isolated manual mutants; required smoke through actual CLI/HTTP/worker/containers. Record failures before fixes and skips honestly.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass; update paths for a separately packaged hosting/addon layout and explicitly record scope.
- [ ] `uv run alembic heads` — one supported head; test upgrade of populated legacy data for schema changes. Release/profile compatibility and clean restored source hashes verified.
- [ ] Fresh independent reviewer checks every criterion on frozen source and records exact SHA/profile, commands, counts, baseline and remaining limits. Final integrated full/data/browser/Linux qualification belongs to20; focused checks do not claim it.

## Evidence

Not started. No product implementation or verification claimed by publishing this ticket.
