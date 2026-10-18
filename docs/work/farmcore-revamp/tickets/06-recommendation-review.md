---
type: Work Ticket
title: "Farmer requests and receives an advisor check"
description: "Milestones 1–4: farmer requests and receives an advisor check."
status: approved
blocked_by: ["05"]
---

# 06 — Farmer requests and receives an advisor check

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Farmer requests and receives an advisor check. Touchpoints: review app routes/templates/manifest; advisor cards; notification and assistant read tools. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain; rebase migration ancestry during coordinator integration rather than creating parallel heads.

## Blocked by

- [05](./05-reviewed-parameters.md)

## Acceptance criteria

- [ ] Owner requests review of an exact stored assessment; assigned advisor responds with note and reviewer/time.
- [ ] Review does not mutate model output or automatically endorse newer assessments.
- [ ] Golden questions test facts, missing inputs and unauthorized farm requests; deterministic fallback retained.
- [ ] No automatic training/few-shot ingestion of private feedback; no model-authorized write.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [ ] `uv run pytest tests/apps/advisor tests/apps/assistant tests/apps/notifications -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [ ] `uv run alembic heads` — one head after integration.
- [ ] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Pending implementation.
