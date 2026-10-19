---
type: Work Ticket
title: "Recipient-bound grants preserve farmer control"
description: "Milestones 1–4: recipient-bound grants preserve farmer control."
status: in_progress
blocked_by: ["02"]
---

# 03 — Recipient-bound grants preserve farmer control

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Recipient-bound grants preserve farmer control. Touchpoints: share services/routes/models/migration; core identity integration; grantee views and exports. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain. Implementation workers propose schema changes; only the designated coordinator authors/serializes revision files. During access tickets 01–03, the access chat coordinator owns revisions exclusively; other chats request schema changes from the integration coordinator.

## Blocked by

- [02](./02-member-sessions.md)

## Acceptance criteria

- [ ] Recipient identity is bound to a grant; numeric URL alone is insufficient.
- [ ] Recipient sees only allowed farm/resources; expiry/revoke blocks next read.
- [ ] No audit/raw transcript access through ordinary grants; own/grantee views keep audited reads.
- [ ] Owner creates/revokes grants in the browser; foreign member cannot administer them.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [ ] `uv run pytest tests/apps/share tests/core -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [ ] `uv run alembic heads` — one head after integration.
- [ ] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Speculative implementation authorized from frozen ticket02 commit
`563b11ffffcc910a707793b7b4c8fedbcb2961aa`; ticket02 exact-head CI passed and it is merged,
and its root actual-tip integration gate has passed. Ticket03 retains its own QA/CI/integration gates.

Schema-only foundation is being verified before runtime authorization changes so other approved
workstreams can extend the single migration chain. Exact contract: nullable indexed bigint
`data_grant.recipient_identity_id` -> `local_identity.id`, NO ACTION; index
`ix_data_grant_recipient_identity_id`, FK `fk_data_grant_recipient_identity_id_local_identity`.
Revision `core_0004_grant_recipients` follows `core_0003`. No identity-name backfill, no new routes
or imports; existing grant authority is unchanged in this foundation. Coordinator alone authors
revision/schema tests; one worker owns normal runtime implementation after foundation freeze.

Tier3 foundation failure model (before final verification):
- Legacy grantee names accidentally confer identity authority -> upgrade preserves owner/named
  grants and leaves every recipient binding NULL, even when a matching local identity exists.
- Dangling or cascaded recipients -> real FK unknown-ID property checks and NO ACTION deletion
  assertions; grant rows survive attempted invalid changes.
- Partial upgrade/downgrade loses farm/grant/auth rows -> real migrated PostGIS round-trip verifies
  original grants, identities, memberships and sessions survive; downgrade only removes binding.
- Divergent revisions/ORM or accidental runtime dependency -> one head, metadata/no-autogenerate
  diff, full schema checks and legacy share regressions; no missing runtime imports.
- Bad foundation guards escaping tests -> three serialized manual migration mutants planned:
  missing FK, legacy-name backfill and destructive downgrade; exact restoration required.

TDD: schema-head RED1 failed in1.49s (`core_0003` vs `core_0004_grant_recipients`), then
minimum nullable FK/index migration plus one ORM attribute. First schema suite9 passed in2.80s.
Two Ruff context-manager formatting findings were corrected; final schema/property/review/gate
evidence is pending. Preflight clean/linter/services healthy; generic mypy probe is inapplicable
(no checker configured), and manual collection passed3082 tests in2.68s after script10s timeout.

Runtime policy: ordinary grant URLs require authenticated explicit binding even in demo; normal
allowlisted public farm reads remain separate. Positive recipient fixtures use real authenticated
bound recipients, not an operator/global-demo authority flip. Existing grants use UTC wall validity
independent of farm replay. Recipient-only landing/pages avoid farm switchers/private polling.
Owner scoped preview/management stays own-farm; ordinary grants never expose audit/raw/pending
capture; legacy broad/reserved-owner grants fail closed for recipients. No runtime completion claim.

### Verified standalone schema foundation

Tier3 gate PASS on actual base `563b11ffffcc910a707793b7b4c8fedbcb2961aa`:102 passed,4 deselected
in11.57s, seed19032026;100% changed-line coverage; Python lint/secrets clean. Independent schema
and legacy-share review/run:102 passed in7.34s, no findings. Schema includes20 real unknown-ID
FK property examples. Three manual mutants killed: missing FK, legacy-name backfill and
destructive downgrade; exact revision bytes restored (SHA256
`4a741d3db55d0af524430d2ec5b2229468b1afbcf6507526ba7a1a705b147820`).

Manual CLI smoke on a disposable old-core0003 database: `farmcore migrate` returned
`migrated to head`; head `core_0004_grant_recipients`, one legacy grant preserved unbound.
Disposable database removed. No production database was migrated. Types unconfigured, dependencies
unchanged, smoke/mutation conventions absent: honest generic skips with manual fallbacks above.
Sole new head `core_0004_grant_recipients`; metadata/no-autogenerate diff, nullable BigInteger FK,
NO ACTION, index, preservation and downgrade/re-upgrade verified. Foundation changes only one
model attribute, one revision and schema tests, with no new route imports or grant authority.

```bash
PYTEST_ADDOPTS='tests/core/test_schema.py tests/apps/share -m "not data" --randomly-seed=19032026 -ra --junitxml=/tmp/farmcore-access03-foundation-junit.xml --cov-report=json:/tmp/farmcore-access03-foundation-coverage.json' bash ~/.claude/scripts/gauntlet.sh --tier=3 --base=563b11ffffcc910a707793b7b4c8fedbcb2961aa --suite-timeout=1800
```

This verifies only the standalone migration foundation. Runtime recipient authorization, browser
management and all ticket03 acceptance criteria remain pending implementation/review/full tier3.
