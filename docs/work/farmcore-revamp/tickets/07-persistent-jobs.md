---
type: Work Ticket
title: "Advice becomes a persistent planned spray job"
description: "Milestones 1–4: advice becomes a persistent planned spray job."
status: approved
blocked_by: ["04", "05"]
---

# 07 — Advice becomes a persistent planned spray job

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Advice becomes a persistent planned spray job. Touchpoints: new planning app/models/routes/templates; forecast and advisor entry points; migration. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain. Implementation workers propose schema changes; only the designated coordinator authors/serializes revision files. During access tickets 01–03, the access chat coordinator owns revisions exclusively; other chats request schema changes from the integration coordinator.

## Blocked by

- [04](./04-live-weather.md)
- [05](./05-reviewed-parameters.md)

## Acceptance criteria

- [ ] Create/edit/cancel planned job with parcels, assessment/version, product, duration and farmer availability.
- [ ] State survives restart/device changes; stale revision returns conflict.
- [ ] Planned jobs do not enter completed register or consume stock.
- [ ] Read/write ownership uses central identity/scopes and explicit job lifecycle.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [ ] `uv run pytest tests/apps/planning tests/apps/forecast tests/apps/operations -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [ ] `uv run alembic heads` — one head after integration.
- [ ] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Pending implementation.
