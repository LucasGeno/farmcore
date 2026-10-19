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

### Runtime failure model before verification

- Guessing a grant ID, sharing its URL, or matching a legacy grantee label confers authority:
  real HTTP anonymous, wrong-identity and unbound legacy controls must deny; authenticated
  exact-recipient controls must succeed without requiring unrelated farm membership.
- A selected farm, foreign farm query parameter, stale cookie or separate member role widens
  grant authority: HTTP tests vary identities, membership, resource IDs and farm selectors;
  principal resolution and exact-grant scope checks enforce both identity and farm boundaries.
- Revoked/expired grants or disabled identities remain usable: next-request wall-clock validity,
  revoke and identity-disable probes must deny every read/export surface.
- Broad/reserved grants or generic resource manifests expose audit, transcripts, pending capture,
  private advice inputs or parameter history: explicit resource policy and advice projection
  whitelist tests deny raw provenance and retain only consented stored assessment facts.
- Advice-only consent triggers station/weather reads: advice-only recipient probes must show
  limited current-input availability without weather payload or unauthorized resource audit;
  explicit weather consent and member provenance remain positive controls.
- Owner management admits foreign farms or forged requests: real browser/HTTP create/revoke
  tests cover owner authority, existing enabled recipient selection, CSRF and foreign-member denial.
- Recipient pages leak other farm metadata through layout/widgets/polling: fresh desktop/mobile
  browser review checks recipient-only landing, allowed content, logout and forbidden background reads.
- Guards pass vacuous tests: after source freeze, serialize plausible runtime mutants for recipient
  equality, validity/revocation, farm/resource scoping and private projection; require each killed,
  restore exact source bytes and diff, then run final gate on the restored tree.

These are verification obligations, not completed evidence. Foundation evidence above does not
prove runtime authorization or browser behavior.

Verification seam clarification: existing share HTTP exports are typed endpoints; there is no
arbitrary generic model-export HTTP route. Test the real advice HTTP projection directly, and
exercise the established public `ScopedRepository` resource-policy API against migrated PostGIS
for arbitrary app-owned resource denial, following `tests/core/test_access.py`. No mocked internal
collaborators or test-only production routes. Parameter/history and operation-correction resources
remain member-only in this ticket; ordinary recipient presets do not expose their private scopes.

Runtime TDD progress (not final verification): zero-membership exact recipient HTTP RED403
(1 failed2.53s) -> minimum GREEN1 passed2.48s. Owner create/revoke RED403 ->2 passed3.35s;
recipient landing redirect RED ->3 passed4.19s; own-farm owner scoped preview RED404 ->4 passed4.26s.
Adversarial identity/status/selector matrix14 passed8.36s. Advice projection HTTP RED403 ->
1 passed14 deselected3.12s. Public repository API unlisted app-owned resource RED (1 failed1.52s)
-> explicit grant-view readonly resource/scope guard,17 new tests passed9.73s.
First broad share diagnostic:26 failed82 passed4 deselected16.23s, reflecting obsolete
bearer-positive fixtures, recipient form and anonymous-ledger contracts. These must be converted
to real authenticated bound recipient positives while retaining genuine anonymous denials and
strengthening actor attribution; this diagnostic is not a passing gate.

Subsequent diagnostic core+share run:5 failed807 passed6 deselected139.15s. Remaining old
contracts concerned operator elevation inside grant audit, anonymous demo audit grants,
resolved-farm form URLs, recipient provisioning in a farm-switcher fixture, and anonymous tour
consent ledger access. Corrections preserve separate operator global-audit positive controls and
explicit recipient provisioning rather than widening grant or anonymous authority. Final focused
rerun and broad gate remain pending. Mixed-sensitive whole-grant denial focused control:
6 passed21 deselected6.10s. Runtime property work now includes32 generated examples/64 hostile
HTTP grant requests with an authorized control, no leaked data/audit and unchanged binding;
raw-transcript/pending export controls2 passed27 deselected3.23s. These interim numbers do not
replace frozen-source final evidence.

Runtime checkpoint `9c6c20a` preserved171 focused passing tests before clean merge of verified
common foundation `e89dcef248bd82f11ab5e2cddc0058cf93732599` at `ec7ca4e`. Core0004 revision
bytes remain exactly `4a741d3db55d0af524430d2ec5b2229468b1afbcf6507526ba7a1a705b147820`.
Actual parameter-version/review/activation and operation-correction persisted private-model
probes added: normal scoped member/operator reads succeed, foreign rows filter, ordinary grant
list/execute/unaudited/private-scope reads deny without audit. Recipient suite37 passed21.05s;
full Ruff check/format clean466 files. This is focused proof, not final gate evidence.

Fresh static review found one P2 blocker: grant-context HTML errors still extended the private
application shell, allowing unauthorized widget polling on revoked/expired/unknown grant pages.
Coordinator confirmed the renderer path; a bounded standalone error fix requires observed HTTP
HTML RED before GREEN. Browser execution, runtime mutants and final broad gate await that fix.
No other spec/standards findings at this review. Operator-only preview retains documented legacy
`grant:<id>` attribution; shared operator token cannot distinguish individual operators. Member
and recipient reads use authenticated `user:<id>` plus the actual grant/farm/purpose.
