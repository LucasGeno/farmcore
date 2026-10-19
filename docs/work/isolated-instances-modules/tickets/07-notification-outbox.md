---
type: Work Ticket
title: "Dispatch notifications through a transactional outbox"
description: "The Telegram advice push (`chat.push_advice`, today cursor-based and at-most-once) and future external deliveries are written as intents in the same transaction as their cause and dispatched by the worker with deduplication and receipts."
status: blocked
blocked_by: ["02", "06"]
---

# 07 — Dispatch notifications through a transactional outbox

**Source:** [Change Spec](../spec.md) R3, D4. **Track:** B — Durable background work. **Tier:** 3.

## Outcome

The Telegram advice push (`chat.push_advice`, today cursor-based and at-most-once) and future external deliveries are written as intents in the same transaction as their cause and dispatched by the worker with deduplication and receipts. Nothing is sent while `external_effects` is paused.

## Scope

In: `outbox` table and dispatcher task; Telegram provider adapter (in-app notifications are presentation-only and need none); reconciliation view for ambiguous deliveries; admission check interface `may_dispatch(intent)` that ticket 15 extends with module generation. Out: event sourcing, exactly-once claims.

## Touchpoints

apps/chat/push.py; bot send path; apps/notifications (reads only); core transaction seams; coordinator revision.

## Blocked by

- [02 — Back up and restore an installation into an independent copy](02-backup-restore.md)
- [06 — Move station ingest and advisor evaluation onto durable tasks](06-durable-job-adoption.md)

## Acceptance criteria

- [ ] Rollback leaves no intent; commit then worker crash leaves a dispatchable intent.
- [ ] Retried claims never duplicate a delivery the provider acknowledged; ambiguous outcomes are listed for reconciliation.
- [ ] Paused effects (restored copy) hold intents without sending; enabling resumes them without blind replay of expired ones.

## Verification

- [ ] `uv run pytest tests/apps/notifications/test_outbox.py -q`
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests`.
- [ ] `uv run alembic heads` shows the supported heads only; populated-legacy upgrade tested for schema changes.
- [ ] Tier-3 `verify`: written failure model first, adversarial/property cases, mutants killed and restored, real CLI/HTTP/worker/container smoke.
- [ ] Independent review against this ticket on a frozen SHA. See [execution rules](../tickets.md#execution-rules).

## Evidence

Not started.
