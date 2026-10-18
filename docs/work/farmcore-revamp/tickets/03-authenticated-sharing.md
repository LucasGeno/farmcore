---
type: Work Ticket
title: "Recipient-bound grants preserve farmer control"
description: "Milestones 1–4: recipient-bound grants preserve farmer control."
status: approved
blocked_by: ["02"]
---

# 03 — Recipient-bound grants preserve farmer control

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Recipient-bound grants preserve farmer control. Touchpoints: share services/routes/models/migration; core identity integration; grantee views and exports. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain; rebase migration ancestry during coordinator integration rather than creating parallel heads.

## Blocked by

- [02](./02-member-sessions.md)

## Acceptance criteria

- [ ] Recipient identity is bound to a grant; numeric URL alone is insufficient.
- [ ] Recipient sees only allowed farm/resources; expiry/revoke blocks next read.
- [ ] No audit/raw transcript access through ordinary grants; own/grantee views keep audited reads.
- [ ] Owner creates/revokes grants in the browser; foreign member cannot administer them.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [ ] `uv run pytest tests/apps/share tests/core -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [ ] `uv run alembic heads` — one head after integration.
- [ ] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Pending implementation.
