---
type: Work Ticket
title: "Local identities, farm memberships and role enforcement"
description: "Milestones 1–4: local identities, farm memberships and role enforcement."
status: approved
blocked_by: ["01"]
---

# 02 — Local identities, farm memberships and role enforcement

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Local identities, farm memberships and role enforcement. Touchpoints: core identity models/session/login/CLI; scoped repositories and farm switcher; web route guards; single Alembic migration. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain; rebase migration ancestry during coordinator integration rather than creating parallel heads.

## Blocked by

- [01](./01-private-access-default.md)

## Acceptance criteria

- [ ] CLI bootstrap creates local owner securely; no public signup, credentials or session tokens in URLs/logs.
- [ ] Owner/advisor memberships, opaque hashed sessions, expiry/logout/revocation, rate limits and CSRF enforced.
- [ ] Farm selectors/switcher/listing and read/write routes enforce membership; advisor cannot write operations or change installation settings.
- [ ] Role checks hold across browser and programmatic routes; installation admin break-glass remains documented.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [ ] `uv run pytest tests/core tests/apps/share tests/apps/chat tests/apps/assistant -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [ ] `uv run alembic heads` — one head after integration.
- [ ] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Pending implementation.
