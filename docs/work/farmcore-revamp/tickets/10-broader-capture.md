---
type: Work Ticket
title: "Confirm field observations and irrigation facts"
description: "Milestones 1–4: confirm field observations and irrigation facts."
status: approved
blocked_by: ["09"]
---

# 10 — Confirm field observations and irrigation facts

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Confirm field observations and irrigation facts. Touchpoints: chat extractor DTO/handlers; operations models/routes; assistant entry points. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain; rebase migration ancestry during coordinator integration rather than creating parallel heads.

## Blocked by

- [09](./09-audited-corrections.md)

## Acceptance criteria

- [ ] Resolve missing/ambiguous parcel/date/unit before preview; confirm/cancel supported for observation/irrigation facts.
- [ ] Idempotent farm/member-bound write; no unconfirmed record exposed as done.
- [ ] Preserve original irrigation amount/unit and gross/net basis when provided; no silent efficiency assumption.
- [ ] No irrigation recommendations or spray stock consumption for these record types.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [ ] `uv run pytest tests/apps/chat tests/apps/operations tests/apps/assistant -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [ ] `uv run alembic heads` — one head after integration.
- [ ] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Pending implementation.
