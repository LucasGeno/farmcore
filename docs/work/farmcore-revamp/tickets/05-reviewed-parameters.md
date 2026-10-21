---
type: Work Ticket
title: "Versioned agronomic parameters are reviewable"
description: "Milestones 1–4: versioned agronomic parameters are reviewable."
status: approved
blocked_by: ["02"]
---

# 05 — Versioned agronomic parameters are reviewable

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Versioned agronomic parameters are reviewable. Touchpoints: advisor config/evaluation and new review app; app manifest; migration; evaluation snapshots. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain. Implementation workers propose schema changes; only the designated coordinator authors/serializes revision files. During access tickets 01–03, the access chat coordinator owns revisions exclusively; other chats request schema changes from the integration coordinator.

## Blocked by

- [02](./02-member-sessions.md)

## Acceptance criteria

- [ ] Seed current parameters unchanged with source/assumed status and bounds; no generic executable CSV rule engine.
- [ ] Expert proposes/reviews/activates version with farm/crop/region scope and immutable history.
- [ ] New advice stores effective version and input provenance; old advice remains unchanged and legacy is labelled.
- [ ] Missing inputs and rejected/not-evaluable states are explicit; reviewer approval never claims field accuracy.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [ ] `uv run pytest tests/apps/advisor tests/apps/kpi tests/core -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [ ] `uv run alembic heads` — one head after integration.
- [ ] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Pending implementation.
