---
type: Work Ticket
title: "Retain distinct crop seasons on a stable parcel"
description: "A parcel carries dated crop seasons."
status: blocked
blocked_by: ["../../farmcore-revamp/tickets/12-integration-qa.md"]
---

# 09 — Retain distinct crop seasons on a stable parcel

**Source:** [Change Spec](../spec.md) R5. **Track:** C — Neutral farm model. **Tier:** 3.

## Outcome

A parcel carries dated crop seasons. The owner configures the current one; earlier records and assessments keep their season context. Unknown stays unknown.

## Scope

In: season model, owner-authorized configuration over HTTP, parcel views, assessment evidence references. Out: phenology prediction, new disease models.

## Touchpoints

core/models/canonical.py (Parcel relations); apps/fields; advisor evidence adapter; coordinator revision.

## Blocked by

- [Revamp 12 — integration QA](../../farmcore-revamp/tickets/12-integration-qa.md)

## Acceptance criteria

- [ ] One parcel holds distinct seasons; editing the current one leaves prior records/assessments unchanged.
- [ ] Unknown crop/date/phenology is explicit; overlapping seasons are rejected or explicit, never collapsed; stale edits and cross-farm selectors are rejected.
- [ ] Legacy parcels and records stay readable and exportable without invented season facts; vineyard behaviour unchanged.

## Verification

- [ ] `uv run pytest tests/apps/fields/test_seasons.py -q`
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests`.
- [ ] `uv run alembic heads` shows the supported heads only; populated-legacy upgrade tested for schema changes.
- [ ] Tier-3 `verify`: written failure model first, adversarial/property cases, mutants killed and restored, real CLI/HTTP/worker/container smoke.
- [ ] Independent review against this ticket on a frozen SHA. See [execution rules](../tickets.md#execution-rules).

## Evidence

Not started.
