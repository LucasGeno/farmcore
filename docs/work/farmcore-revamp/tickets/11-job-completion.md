---
type: Work Ticket
title: "Complete planned work into one confirmed operation"
description: "Milestones 1–4: complete planned work into one confirmed operation."
status: approved
blocked_by: ["08", "10"]
---

# 11 — Complete planned work into one confirmed operation

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Complete planned work into one confirmed operation. Touchpoints: planning completion; operations confirmation; inventory; advisor recalculation; evidence links. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain; rebase migration ancestry during coordinator integration rather than creating parallel heads.

## Blocked by

- [08](./08-resource-scheduling.md)
- [10](./10-broader-capture.md)

## Acceptance criteria

- [ ] Preview actual work and differences from saved plan; confirmed completion atomically links/create canonical operation.
- [ ] Double click/retry/concurrent completion cannot duplicate operation or stock movement.
- [ ] Keep original assessment/version, human review, plan revision and immutable available weather separately.
- [ ] Re-evaluate only after successful commit; failed transactions leave plan/log/stock unchanged.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [ ] `uv run pytest tests/apps/planning tests/apps/operations tests/apps/inventory tests/apps/advisor -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [ ] `uv run alembic heads` — one head after integration.
- [ ] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Pending implementation.
