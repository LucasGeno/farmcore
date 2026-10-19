---
type: Work Ticket
title: "Record fertilization through an optional in-tree app"
description: "A farmer can record what fertilizer went on which parcel this season, confirm or correct it, and see stock move exactly once."
status: blocked
blocked_by: ["09", "11"]
---

# 13 — Record fertilization through an optional in-tree app

**Source:** [Change Spec](../spec.md) R6. **Track:** C — Neutral farm model. **Tier:** 3.

## Outcome

A farmer can record what fertilizer went on which parcel this season, confirm or correct it, and see stock move exactly once. This is the first farmer-visible payoff of the neutral model.

## Scope

In: optional `fertilization` app (in-tree), preview/confirm/cancel/correct, nutrient composition with provenance, diary/map/assistant contributions. Out: rate recommendations, soil models, regulatory filings, inferred composition.

## Touchpoints

apps/fertilization (new); shared records/catalog/quantity/inventory interfaces; coordinator revision.

## Blocked by

- [09 — Retain distinct crop seasons on a stable parcel](09-crop-seasons.md)
- [11 — Keep stock accounting exact through neutral quantities](11-inventory-consumer-migration.md)

## Acceptance criteria

- [ ] Owner previews, confirms, cancels and corrects; original values, reason and actor are preserved.
- [ ] Missing area/composition/density asks for clarification rather than inventing a conversion.
- [ ] Planned work never decrements stock; confirmed applications and correction deltas apply once under retry, concurrency and rollback; farm and module authority enforced on every entry point.

## Verification

- [ ] `uv run pytest tests/apps/fertilization -q`
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests`.
- [ ] `uv run alembic heads` shows the supported heads only; populated-legacy upgrade tested for schema changes.
- [ ] Tier-3 `verify`: written failure model first, adversarial/property cases, mutants killed and restored, real CLI/HTTP/worker/container smoke.
- [ ] Independent review against this ticket on a frozen SHA. See [execution rules](../tickets.md#execution-rules).

## Evidence

Not started.
