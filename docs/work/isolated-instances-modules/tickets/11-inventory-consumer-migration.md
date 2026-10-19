---
type: Work Ticket
title: "Keep stock accounting exact through neutral quantities"
description: "Inventory reads and confirmed/corrected application effects use neutral quantities with identical stock semantics."
status: blocked
blocked_by: ["10"]
---

# 11 — Keep stock accounting exact through neutral quantities

**Source:** [Change Spec](../spec.md) R5. **Track:** C — Neutral farm model. **Tier:** 3.

## Outcome

Inventory reads and confirmed/corrected application effects use neutral quantities with identical stock semantics.

## Scope

In: apps/inventory, quantity adapter, correction/completion contracts. Out: new application types.

## Touchpoints

apps/inventory; core/units.py; revamp correction/completion seams.

## Blocked by

- [10 — Move records and capture onto the neutral catalog](10-record-consumer-migration.md)

## Acceptance criteria

- [ ] Historical stock and correction deltas match the golden baseline before and after.
- [ ] Planned, pending, cancelled work and observations never consume stock; concurrent confirmations (real competing sessions) apply one delta.
- [ ] Unknown conversion factors cannot produce a plausible-looking balance; foreign-farm requests reveal nothing.

## Verification

- [ ] `uv run pytest tests/apps/inventory -q`
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests`.
- [ ] `uv run alembic heads` shows the supported heads only; populated-legacy upgrade tested for schema changes.
- [ ] Tier-3 `verify`: written failure model first, adversarial/property cases, mutants killed and restored, real CLI/HTTP/worker/container smoke.
- [ ] Independent review against this ticket on a frozen SHA. See [execution rules](../tickets.md#execution-rules).

## Evidence

Not started.
