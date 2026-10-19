---
type: Work Ticket
title: "Remove crop-protection assumptions from shared core"
description: "The contract step: shared core no longer requires fungicide attributes or spray decisions."
status: blocked
blocked_by: ["10", "11", "12"]
---

# 14 — Remove crop-protection assumptions from shared core

**Source:** [Change Spec](../spec.md) R5, D6. **Track:** C — Neutral farm model. **Tier:** 3.

## Outcome

The contract step: shared core no longer requires fungicide attributes or spray decisions. A bundle without crop protection starts and is useful.

## Scope

In: drop shared restrictions from core models; decouple `forecast`, `chat`, `kpi`, `notifications` and `assistant` from `advisor` (manifest `depends` and direct imports) so each degrades to unavailable without it; make crop protection a non-required app; versioned adapters for public representations, deprecation notes. Out: framework rewrite.

## Touchpoints

core/models/canonical.py; apps/forecast, chat, kpi, notifications, assistant (advisor imports); app `required` flags; migrations; docs/architecture.md; API compatibility notes.

## Blocked by

- [10 — Move records and capture onto the neutral catalog](10-record-consumer-migration.md)
- [11 — Keep stock accounting exact through neutral quantities](11-inventory-consumer-migration.md)
- [12 — Keep crop-protection assessments typed and reproducible](12-assessment-consumer-migration.md)

## Acceptance criteria

- [ ] Static search and public tests show no shared-core requirement for contact/penetrant/downy/powdery or spray decisions.
- [ ] A registry test loads every bundle that omits `advisor` and its dependents fail only where documented; forecast receipts remain readable without advisor.
- [ ] A bundle without advisor starts, navigates parcels/records/diary and shows unavailable capabilities honestly.
- [ ] Populated legacy databases migrate with matching evidence, exports and stock.

## Verification

- [ ] `uv run pytest tests/core/test_schema.py tests/core/test_registry.py -q`
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests`.
- [ ] `uv run alembic heads` shows the supported heads only; populated-legacy upgrade tested for schema changes.
- [ ] Tier-3 `verify`: written failure model first, adversarial/property cases, mutants killed and restored, real CLI/HTTP/worker/container smoke.
- [ ] Independent review against this ticket on a frozen SHA. See [execution rules](../tickets.md#execution-rules).

## Evidence

Not started.
