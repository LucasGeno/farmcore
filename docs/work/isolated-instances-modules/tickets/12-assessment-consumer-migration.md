---
type: Work Ticket
title: "Keep crop-protection assessments typed and reproducible"
description: "The advisor stores its typed result inside a shared assessment evidence envelope that other modules can use with their own types."
status: blocked
blocked_by: ["08", "09"]
---

# 12 — Keep crop-protection assessments typed and reproducible

**Source:** [Change Spec](../spec.md) R5. **Track:** C — Neutral farm model. **Tier:** 3.

## Outcome

The advisor stores its typed result inside a shared assessment evidence envelope that other modules can use with their own types. Every legacy decision keeps its meaning.

## Scope

In: advisor engine/service/models/routes, reviewed parameters and recommendation reviews from revamp 05/06, forecast/planning/assistant readers. Out: mapping any non-spray result onto spray enums.

## Touchpoints

apps/advisor; apps/forecast readers; apps/assistant tools; coordinator revision.

## Blocked by

- [08 — Add neutral catalog items and typed quantities beside spray data](08-catalog-quantity-expand.md)
- [09 — Retain distinct crop seasons on a stable parcel](09-crop-seasons.md)

## Acceptance criteria

- [ ] Frozen numeric/replay examples and spray/wait/protected/none outputs are unchanged in meaning, sources and public representation.
- [ ] Stored parameter versions, input snapshots and human reviews stay reproducible.
- [ ] The envelope accepts a second, differently typed result in tests without relaxing advisor validation.

## Verification

- [ ] `uv run pytest tests/apps/advisor -q`
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests`.
- [ ] `uv run alembic heads` shows the supported heads only; populated-legacy upgrade tested for schema changes.
- [ ] Tier-3 `verify`: written failure model first, adversarial/property cases, mutants killed and restored, real CLI/HTTP/worker/container smoke.
- [ ] Independent review against this ticket on a frozen SHA. See [execution rules](../tickets.md#execution-rules).

## Evidence

Not started.
