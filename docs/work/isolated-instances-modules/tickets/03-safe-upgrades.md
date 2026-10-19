---
type: Work Ticket
title: "Upgrade an installation through one migrator with a tested recovery path"
description: "`farmcore upgrade` (and the documented Compose procedure) checks compatibility, takes a backup, runs migrations exactly once and reports health."
status: blocked
blocked_by: ["02"]
---

# 03 — Upgrade an installation through one migrator with a tested recovery path

**Source:** [Change Spec](../spec.md) R2. **Track:** A — Portable installation. **Tier:** 3.

## Outcome

`farmcore upgrade` (and the documented Compose procedure) checks compatibility, takes a backup, runs migrations exactly once and reports health. When a migration or post-start check fails, the runbook restores the pre-upgrade backup, and that path is tested.

## Scope

In: release and Alembic-heads compatibility check (module `core_api` checks arrive with ticket 16), pre-upgrade bundle, advisory lock around the `migrate` service from ticket 01, maintenance/failed state in `/health`, upgrade + recovery runbook in self-hosting docs. Out: fleet-wide orchestration (ticket 18), automatic schema downgrade.

## Touchpoints

core/commands/migrate.py; core/startup.py; docker-compose.yml; docs/self-hosting.md "Upgrading".

## Blocked by

- [02 — Back up and restore an installation into an independent copy](02-backup-restore.md)

## Acceptance criteria

- [ ] Concurrent upgrade attempts execute one migration; the other waits or exits cleanly.
- [ ] Injected preflight, migration and post-start failures leave A recoverable through the documented restore while B keeps serving.
- [ ] An incompatible release (unknown or newer Alembic heads) fails before any migration runs; `/health` states the actual release and Alembic heads in use.
- [ ] Docs state plainly that reverting the image does not revert the schema.

## Verification

- [ ] `uv run pytest tests/instances/test_upgrade.py -q`
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests`.
- [ ] `uv run alembic heads` shows the supported heads only; populated-legacy upgrade tested for schema changes.
- [ ] Tier-3 `verify`: written failure model first, adversarial/property cases, mutants killed and restored, real CLI/HTTP/worker/container smoke.
- [ ] Independent review against this ticket on a frozen SHA. See [execution rules](../tickets.md#execution-rules).

## Evidence

Not started.
