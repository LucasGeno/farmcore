---
type: Work Ticket
title: "Run the live forecast fetch as a durable, fenced task"
description: "The live forecast fetch is the tracer bullet for durable work: a `farmcore worker` process claims per-farm tasks, a crash or deadline leads to a retry, and a stale attempt cannot publish a receipt."
status: blocked
blocked_by: ["01", "04", "../../farmcore-revamp/tickets/12-integration-qa.md"]
---

# 05 — Run the live forecast fetch as a durable, fenced task

**Source:** [Change Spec](../spec.md) R3, D3. **Track:** B — Durable background work. **Tier:** 3.

## Outcome

The live forecast fetch is the tracer bullet for durable work: a `farmcore worker` process claims per-farm tasks, a crash or deadline leads to a retry, and a stale attempt cannot publish a receipt. Replay keeps using in-process `step_to`.

## Scope

In: `core/tasks.py` seam wrapping the chosen library; `farmcore worker` CLI and Compose service; a FarmCore-owned attempt row holding the publication fence (attempt current, farm eligible, clock-mode generation unchanged), checked in the publishing transaction, independent of the library's own job-status bookkeeping; restore (ticket 02) bumps the generation so a restored copy never runs A's queued work; removal of the global advisory lock from the live forecast path. Out: other jobs (06), outbox (07), module generation (15).

## Touchpoints

core/jobs.py; core/manifest.py (JobSpec stays the public seam); core/clock.py (mode generation); apps/forecast job/store; docker-compose.yml; coordinator-authored revision.

## Blocked by

- [01 — Run two isolated installations from one release](01-portable-installations.md)
- [04 — Spike: choose the Postgres task library](04-task-queue-spike.md)
- [Revamp 12 — integration QA](../../farmcore-revamp/tickets/12-integration-qa.md)

## Acceptance criteria

- [ ] Killing a worker mid-fetch leads to a retry; the killed attempt, if it resumes, is rejected at commit.
- [ ] A provider stalled for farm A (loopback stub) does not delay farm B's task beyond its normal latency; A's failure is visible, not silent.
- [ ] Switching live → replay invalidates in-flight attempts before publication; replay output is byte-identical to the baseline.
- [ ] No request handler performs a live fetch; it enqueues.
- [ ] In a copy restored from a backup taken with queued and in-flight tasks, none of those attempts publish; due work is re-planned once.

## Verification

- [ ] `uv run pytest tests/core/test_tasks.py tests/apps/forecast/test_task_worker.py -q`
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests`.
- [ ] `uv run alembic heads` shows the supported heads only; populated-legacy upgrade tested for schema changes.
- [ ] Tier-3 `verify`: written failure model first, adversarial/property cases, mutants killed and restored, real CLI/HTTP/worker/container smoke.
- [ ] Independent review against this ticket on a frozen SHA. See [execution rules](../tickets.md#execution-rules).

## Evidence

Not started.
