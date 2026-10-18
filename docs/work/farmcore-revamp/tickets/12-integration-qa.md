---
type: Work Ticket
title: "Independent QA verifies milestones 1–4"
description: "Milestones 1–4: independent qa verifies milestones 1–4."
status: approved
blocked_by: ["03", "06", "11"]
---

# 12 — Independent QA verifies milestones 1–4

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Independent QA verifies milestones 1–4. Touchpoints: whole branch review; migration chain; regression/browser/security tests; README/SECURITY. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain. Implementation workers propose schema changes; only the designated coordinator authors/serializes revision files. During access tickets 01–03, the access chat coordinator owns revisions exclusively; other chats request schema changes from the integration coordinator.

## Blocked by

- [03](./03-authenticated-sharing.md)
- [06](./06-recommendation-review.md)
- [11](./11-job-completion.md)

## Acceptance criteria

- [ ] Fresh QA agent reports spec gaps separately from standards findings; all blocking findings fixed.
- [ ] Real PostGIS role/expiry/revoke/CSRF/retry/concurrency/rollback regressions and browser journeys pass.
- [ ] One Alembic head, fresh-install upgrade and relevant existing-database upgrade pass.
- [ ] verify tier3 evidence includes failure model, test/lint/frontend/build/security results and manual fallbacks for missing tooling; PRs integrated only after QA.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [ ] `bash ~/.claude/scripts/gauntlet.sh --tier=3 --base=origin/main; node --test tests/core/*.mjs tests/apps/fields/*.mjs` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [ ] `uv run alembic heads` — one head after integration.
- [ ] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Pending implementation.
