---
type: Work Ticket
title: "Move station ingest and advisor evaluation onto durable tasks"
description: "All live manifest jobs run on the worker; APScheduler is removed from the web process."
status: blocked
blocked_by: ["05"]
---

# 06 — Move station ingest and advisor evaluation onto durable tasks

**Source:** [Change Spec](../spec.md) R3. **Track:** B — Durable background work. **Tier:** 3.

## Outcome

All live manifest jobs run on the worker; APScheduler is removed from the web process. Replay order, farm-scoped reset/catch-up and semantic golden outputs are unchanged.

## Scope

In: stations, advisor and remaining manifest jobs; `jobs.run_now` becomes enqueue plus an optional wait bounded by a short deadline (the chat confirm path must answer even if the worker is slow); delete the in-process live scheduler once parity is proven. Out: new agronomic logic.

## Touchpoints

core/jobs.py; apps/stations, apps/advisor job entry points; chat confirm path that calls `run_now`; replay CLI/tests.

## Blocked by

- [05 — Run the live forecast fetch as a durable, fenced task](05-durable-forecast-task.md)

## Acceptance criteria

- [ ] Restarting web or worker, or switching replay → live, loses no due work and creates no duplicate evaluations.
- [ ] Expired or mode-stale attempts cannot publish observations or assessments; other farms progress.
- [ ] Replay golden outputs unchanged; the four ordered lifecycle tests keep their documented exception.
- [ ] `apscheduler` is gone from dependencies.

## Verification

- [ ] `uv run pytest tests/core/test_jobs.py tests/core/test_tasks.py tests/apps/advisor tests/apps/stations -q`
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests`.
- [ ] `uv run alembic heads` shows the supported heads only; populated-legacy upgrade tested for schema changes.
- [ ] Tier-3 `verify`: written failure model first, adversarial/property cases, mutants killed and restored, real CLI/HTTP/worker/container smoke.
- [ ] Independent review against this ticket on a frozen SHA. See [execution rules](../tickets.md#execution-rules).

## Evidence

Not started.
