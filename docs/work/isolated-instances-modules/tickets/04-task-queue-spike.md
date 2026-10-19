---
type: Work Ticket
title: "Spike: choose the Postgres task library"
description: "A time-boxed (≤1 day) throwaway prototype decides between Procrastinate and a minimal in-house `SKIP LOCKED` table, recorded as a Prototype Finding in this folder."
status: ready
blocked_by: []
---

# 04 — Spike: choose the Postgres task library

**Source:** [Change Spec](../spec.md) D3. **Track:** B — Durable background work. **Tier:** normal.

## Outcome

A time-boxed (≤1 day) throwaway prototype decides between Procrastinate and a minimal in-house `SKIP LOCKED` table, recorded as a Prototype Finding in this folder. No product code merges from this ticket.

## Scope

Prove or disprove, with a running script against a disposable PostGIS: (1) enqueue inside an existing SQLAlchemy/psycopg3 transaction so rollback drops the task; (2) its schema can be applied from an Alembic revision in our single chain; (3) a killed worker's task is retried and a slow task for farm A does not block farm B; (4) per-task deadline and cancellation; (5) periodic scheduling or a clean way to keep our manifest `JobSpec.interval`; (6) maintenance health (releases, issues, bus factor) and exit cost. Decide; if Procrastinate fails (1) or (2), choose the in-house table and sketch its ≤200-line design.

## Touchpoints

Scratch directory only; output `docs/work/isolated-instances-modules/task-queue-finding.md` (type: Prototype Finding).

## Blocked by

None — can start now.

## Acceptance criteria

- [ ] Each of the six criteria has an observed result (command + output), not an opinion.
- [ ] A single recommendation with the fencing seam (`publish_if_current(attempt)`) sketched for ticket 05.

## Verification

- [ ] Prototype Finding committed with commands and observed output for every criterion.
- [ ] Independent review against this ticket on a frozen SHA. See [execution rules](../tickets.md#execution-rules).

## Evidence

Not started.
