---
type: Work Index
title: "Isolated installations and modules — implementation graph"
---

# Implementation graph

**Source:** [Approved Change Spec](spec.md). Publication is not a completion or deployment claim. Read each ticket for outcome, bounded scope, exact blockers, proposed public test deliverables and verification.

**Execution status:** this request publishes the architecture/specification and ticket set. New runtime implementation is outside this documentation task; publication and passing dependencies do not start or schedule execution. Existing milestones 1–4 continue under their separate implementation, QA and merge authorization.

Current revamp identifiers refer to the separate [milestones 1–4 ticket set](../farmcore-revamp/spec.md). A passing draft branch or locally green slice is not a completed dependency. The integration coordinator preserves a single runtime migration chain and handles the hosting control database's separate history explicitly. No parallel coordination store.

| Ticket | Blocked by |
|---|---|
| [01 — Run two isolated installations from one release](tickets/01-portable-installations.md) | revamp01 |
| [02 — Restore an installation into an independent self-hosted copy](tickets/02-portable-recovery.md) | 01, revamp02 |
| [03 — Provision one customer installation safely through a management command](tickets/03-hosted-provisioning.md) | 01, 02, revamp02 |
| [04 — Let a customer claim and configure their hosted installation](tickets/04-customer-onboarding.md) | 03, revamp03 |
| [05 — Upgrade one installation with a verified recovery path](tickets/05-safe-upgrades.md) | 02, 03 |
| [06 — Suspend, resume and archive an installation without data loss](tickets/06-suspend-retire.md) | 02, 03, 04 |
| [07 — Recover a leased forecast task after worker failure](tickets/07-durable-forecast-task.md) | 01, revamp12 |
| [08 — Move station and assessment execution onto durable tasks](tickets/08-durable-job-adoption.md) | 07 |
| [09 — Dispatch committed notifications safely after crashes](tickets/09-committed-effect-dispatch.md) | 06, 08, revamp11 |
| [10 — Make module activation and disablement effective across processes](tickets/10-module-lifecycle.md) | 08, 09 |
| [11 — Install a separately packaged compatible FarmCore app](tickets/11-addon-contract.md) | 10 |
| [12 — Add a neutral catalog and typed quantities beside spray data](tickets/12-catalog-quantity-expand.md) | revamp12 |
| [13 — Retain distinct crop seasons on a stable parcel](tickets/13-crop-seasons.md) | revamp12 |
| [14 — Move records and capture onto neutral catalog contracts](tickets/14-record-consumer-migration.md) | 11, 12 |
| [15 — Preserve stock accounting through neutral quantities](tickets/15-inventory-consumer-migration.md) | 12, 14 |
| [16 — Keep crop-protection assessments typed and reproducible](tickets/16-assessment-consumer-migration.md) | 11, 12, 13 |
| [17 — Remove mandatory crop-protection assumptions from shared core](tickets/17-neutral-core-contract.md) | 14, 15, 16 |
| [18 — Run a weather-risk app without crop protection](tickets/18-independent-weather-risk.md) | 11, 13, 17 |
| [19 — Confirm and correct fertilization through an optional app](tickets/19-fertilization-records.md) | 11, 13, 15, 17 |
| [20 — Qualify hosted and self-hosted modular installations](tickets/20-integration-qualification.md) | 04, 05, 06, 09, 18, 19, revamp12 |

## Execution order

1. Complete current private access/member identity prerequisites; portable release/recovery can then run independently of remaining revamp work.
2. Hosted lifecycle03–06 follows the portable foundation. It does not implement payment processing or authorize real customer provisioning.
3. After current revamp12 passes, worker07–09 and neutral catalog/season12–13 can progress in isolated scopes; serialize revisions.
4. Module lifecycle10 and external contract11 enable consumer migrations14–16. Contract17 waits for every consumer group.
5. Independent app proofs18–19 validate broadening; final20 closes qualification for the integrated hosted/self-hosted release.

All behavior changes use public-seam TDD, relevant tier3 failure modeling, focused checks, fresh independent QA and final integrated qualification. Existing implementation agents continue current approved work; publishing this graph does not redirect their file ownership.
