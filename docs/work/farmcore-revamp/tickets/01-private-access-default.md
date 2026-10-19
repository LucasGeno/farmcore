---
type: Work Ticket
title: "Private farm reads are closed by default"
description: "Milestones 1–4: private farm reads are closed by default."
status: in_progress
blocked_by: []
---

# 01 — Private farm reads are closed by default

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

This first slice closes private farm reads using the existing installation operator token; it does not by itself finish member authentication or all of milestone 1. Touchpoints: core access/startup/settings; web administrator checks; share routes; security/setup docs; HTTP tests. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain. Implementation workers propose schema changes; only the designated coordinator authors/serializes revision files. During access tickets 01–03, the access chat coordinator owns revisions exclusively; other chats request schema changes from the integration coordinator.

## Blocked by

None — can start immediately.

## Acceptance criteria

- [ ] Private mode rejects anonymous farm pages, exports, fragments and assistant reads; health/static/login remain usable.
- [ ] Existing installation admin token can access farms; forged tour/farm selectors and numeric grant IDs do not bypass privacy.
- [ ] Demo mode is explicit and limited to designated demo farms; existing demo tests use deliberate fixtures. Keep separate anonymous-private, authenticated and explicit-demo clients; do not globally set test settings/client to demo.
- [ ] Global model-call audit remains installation-admin-only; no private model payload metadata leaks.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [ ] `uv run pytest tests/core tests/apps/share tests/apps/assistant tests/apps/chat -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [ ] `uv run alembic heads` — one head after integration.
- [ ] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Pending implementation.
