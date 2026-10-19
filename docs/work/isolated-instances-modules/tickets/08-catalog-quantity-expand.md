---
type: Work Ticket
title: "Add neutral catalog items and typed quantities beside spray data"
description: "Additive schema: a neutral catalog item and typed quantities (mass, volume, area, count, rates) with entered value/unit and provenance, plus a crop-protection extension holding contact/penetrant and downy/powdery."
status: blocked
blocked_by: ["../../farmcore-revamp/tickets/12-integration-qa.md"]
---

# 08 — Add neutral catalog items and typed quantities beside spray data

**Source:** [Change Spec](../spec.md) R5, D6. **Track:** C — Neutral farm model. **Tier:** 3.

## Outcome

Additive schema: a neutral catalog item and typed quantities (mass, volume, area, count, rates) with entered value/unit and provenance, plus a crop-protection extension holding contact/penetrant and downy/powdery. Existing products and quantities are mirrored; every current consumer keeps working through adapters.

## Scope

In: models, `pint` unit registry (`non_int_type=Decimal`) with FarmCore's allowed units, adapters, semantic golden fixtures frozen before any consumer moves. Include the EU record-content fields (Reg. 2023/564, from 2026-01-01): per-country authorisation number on catalog items (FR AMM, ES registration number, IT registration number), EPPO crop code and BBCH stage on applications, dose in standard units. Product-register import modules (France's E-Phy first) attach to this catalog. Out: consumer migration (10–12), contraction (14), per-country export formats.

## Touchpoints

core/models/canonical.py Product/Quantity; new core/units.py; apps/advisor (extension model); coordinator revision.

## Blocked by

- [Revamp 12 — integration QA](../../farmcore-revamp/tickets/12-integration-qa.md)

## Acceptance criteria

- [ ] Golden fixtures for register export, stock, spray evidence and source dedup are frozen and pass unchanged.
- [ ] No float enters the quantity or stock path (property test). Typed quantities keep entered unit and value; incompatible dimensions and missing density/area/composition raise, never silently convert.
- [ ] A catalog item can exist with no crop-protection extension; the extension still validates targets and type.
- [ ] Migration is additive and upgrades a populated legacy database.
- [ ] A catalog item can carry several country authorisation numbers; an application can carry EPPO crop code, BBCH stage and a standard-unit dose, all optional for legacy rows and never invented.

## Verification

- [ ] `uv run pytest tests/core/test_units.py tests/core/test_catalog.py tests/core/test_schema.py -q`
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests`.
- [ ] `uv run alembic heads` shows the supported heads only; populated-legacy upgrade tested for schema changes.
- [ ] Tier-3 `verify`: written failure model first, adversarial/property cases, mutants killed and restored, real CLI/HTTP/worker/container smoke.
- [ ] Independent review against this ticket on a frozen SHA. See [execution rules](../tickets.md#execution-rules).

## Evidence

Not started.
