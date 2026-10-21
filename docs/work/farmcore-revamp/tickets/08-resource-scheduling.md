---
type: Work Ticket
title: "Resource-aware scheduling offers feasible alternatives"
description: "Milestones 1–4: resource-aware scheduling offers feasible alternatives."
status: approved
blocked_by: ["07"]
---

# 08 — Resource-aware scheduling offers feasible alternatives

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Resource-aware scheduling offers feasible alternatives. Touchpoints: planning engine/resource assignment; forecast ranking; machine/operator forms. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain. Implementation workers propose schema changes; only the designated coordinator authors/serializes revision files. During access tickets 01–03, the access chat coordinator owns revisions exclusively; other chats request schema changes from the integration coordinator.

## Blocked by

- [07](./07-persistent-jobs.md)

## Acceptance criteria

- [ ] Eligible hours combine existing product/weather constraints, estimated duration and configured operator/machine availability.
- [ ] Overlapping assignments reject atomically; unknown capacity is shown rather than assumed safe.
- [ ] Offer eligible alternate time/job/parcel or configured machine; preserve infeasible job with reason.
- [ ] No soil suitability, reduced dose, unsupported price or calibrated probability claims; alternatives are side-effect free.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [ ] `uv run pytest tests/apps/planning tests/apps/forecast -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [ ] `uv run alembic heads` — one head after integration.
- [ ] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Pending implementation.
