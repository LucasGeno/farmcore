---
type: Work Ticket
title: "Confirmed spray records can be corrected safely"
description: "Milestones 1–4: confirmed spray records can be corrected safely."
status: approved
blocked_by: ["02"]
---

# 09 — Confirmed spray records can be corrected safely

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Confirmed spray records can be corrected safely. Touchpoints: operations correction service/routes; chat confirmation; stock transactions and source receipts. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain. Implementation workers propose schema changes; only the designated coordinator authors/serializes revision files. During access tickets 01–03, the access chat coordinator owns revisions exclusively; other chats request schema changes from the integration coordinator.

## Blocked by

- [02](./02-member-sessions.md)

## Acceptance criteria

- [ ] Preview/cancel/confirm correction scoped to farm/member; retain original, correction reason and actor/time.
- [ ] Retries and concurrent confirmation apply one correction and stock delta; failure rolls back all writes.
- [ ] Existing weather receipts and source provenance remain immutable; correction does not overwrite original evidence.
- [ ] Original pending/done spray confirmation paths remain supported with explicit authority.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [ ] `uv run pytest tests/apps/operations tests/apps/chat tests/apps/inventory -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [ ] `uv run alembic heads` — one head after integration.
- [ ] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Pending implementation.
