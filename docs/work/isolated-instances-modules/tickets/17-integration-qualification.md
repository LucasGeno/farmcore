---
type: Work Ticket
title: "Qualify the integrated self-hostable release"
description: "Independent spec and standards review plus a real release drill: two installations, backup of A restored as C, an upgrade with an injected failure and recovery, and the external app installed on one of them."
status: blocked
blocked_by: ["03", "07", "13", "14", "16", "../../farmcore-revamp/tickets/12-integration-qa.md"]
---

# 17 — Qualify the integrated self-hostable release

**Source:** [Change Spec](../spec.md) All. **Track:** Final. **Tier:** 3.

## Outcome

Independent spec and standards review plus a real release drill: two installations, backup of A restored as C, an upgrade with an injected failure and recovery, and the external app installed on one of them.

## Scope

In: all acceptance criteria except deferred hosting; runbooks; exact evidence. Out: real customers, production cutover, ticket 18.

## Touchpoints

Whole initiative; release notes; docs/self-hosting.md; docs/architecture.md.

## Blocked by

- [03 — Upgrade an installation through one migrator with a tested recovery path](03-safe-upgrades.md)
- [07 — Dispatch notifications through a transactional outbox](07-notification-outbox.md)
- [13 — Record fertilization through an optional in-tree app](13-fertilization-records.md)
- [14 — Remove crop-protection assumptions from shared core](14-neutral-core-contract.md)
- [16 — Install an externally packaged threshold-alert app](16-external-threshold-alerts.md)
- [Revamp 12 — integration QA](../../farmcore-revamp/tickets/12-integration-qa.md)

## Acceptance criteria

- [ ] Two-installation isolation, backup/restore, upgrade/recovery, worker crash/fencing, outbox pause and module disable drills pass on Linux containers.
- [ ] Full Python/data replay, browser (desktop/mobile, reduced motion) and migration checks pass on the actual release SHA.
- [ ] Exact SHA, bundle lock, commands, counts and honest skips are published; any required failure blocks qualification.

## Verification

- [ ] Full randomized suite plus the separately ordered lifecycle tests, data replay, browser and Linux container checks.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests`.
- [ ] `uv run alembic heads` shows the supported heads only; populated-legacy upgrade tested for schema changes.
- [ ] Tier-3 `verify`: written failure model first, adversarial/property cases, mutants killed and restored, real CLI/HTTP/worker/container smoke.
- [ ] Independent review against this ticket on a frozen SHA. See [execution rules](../tickets.md#execution-rules).

## Evidence

Not started.
