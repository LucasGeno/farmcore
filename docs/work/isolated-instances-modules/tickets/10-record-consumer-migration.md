---
type: Work Ticket
title: "Move records and capture onto the neutral catalog"
description: "Operations, the register, ISOXML import and confirmed capture read and write through neutral catalog/quantity interfaces."
status: blocked
blocked_by: ["08"]
---

# 10 — Move records and capture onto the neutral catalog

**Source:** [Change Spec](../spec.md) R5. **Track:** C — Neutral farm model. **Tier:** 3.

## Outcome

Operations, the register, ISOXML import and confirmed capture read and write through neutral catalog/quantity interfaces. Each record type keeps its own merge policy.

## Scope

In: apps/operations, chat/assistant confirmation, core/logs.py, import/export serializers. Out: inventory (11), assessments (12).

## Touchpoints

apps/operations; apps/chat; apps/assistant; core/logs.py.

## Blocked by

- [08 — Add neutral catalog items and typed quantities beside spray data](08-catalog-quantity-expand.md)

## Acceptance criteria

- [ ] Spray, observation and irrigation preview/confirm/cancel/correct journeys keep semantic outputs and source identity.
- [ ] The 12-hour spray merge rule is not applied to other record types.
- [ ] Non-spray facts are recorded without fabricated plant-protection fields; register export unchanged.

## Verification

- [ ] `uv run pytest tests/apps/operations tests/apps/chat -q`
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests`.
- [ ] `uv run alembic heads` shows the supported heads only; populated-legacy upgrade tested for schema changes.
- [ ] Tier-3 `verify`: written failure model first, adversarial/property cases, mutants killed and restored, real CLI/HTTP/worker/container smoke.
- [ ] Independent review against this ticket on a frozen SHA. See [execution rules](../tickets.md#execution-rules).

## Evidence

Not started.
