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

### Frozen runtime review and browser evidence

P2 error-shell fix observed real HTTP HTML RED4 failed37 deselected3.95s -> GREEN4 passed3.68s
(revoked, expired, unknown and4400-digit validation inputs). Final errors/recipient/revoke command:
`uv run pytest tests/core/test_errors.py tests/apps/share/test_recipient_identity.py tests/apps/share/test_revoke.py --randomly-seed=19012026 -q --tb=short`:
59 passed26.85s. JSON status/details preserved, HTML errors generic/standalone/no-store.
Frozen runtime commit `bcc003b5894d9f8813c6caec4c3a81cc63abc63c`, source SHA256
`d54595c4e7a9769309f2b0b88c66ef1f513395b1bc1da5685a61e94f1a2066ec`.

Fresh independent static/browser review clears all4 acceptance criteria and standards, with no
weakened legacy assertions. Four actual visible Chromium journeys: desktop1280x900 and
mobile390x844, each normal/reduced motion. Actual owner login/form create/revoke, zero-membership
recipient login/landing/read/export/logout, foreign-owner GET and valid-CSRF revoke denial, and
next-read revocation all pass. Sixteen unknown/expired/revoked/postlogout navigations pass;
192 recipient requests logged, zero unauthorized private polling, page exceptions or overflow.
Twenty screenshots retained; representative active and desktop/mobile error views visually
inspected. Mobile Sunlight18px/black ink verified. Source digest identical before/after; app port
closed, disposable DB count0, secret state absent and TLS directories removed. P2 closed with
real browser request evidence, not HTML-only inspection. Reports:
`/tmp/farmcore-sharing03-report.json`, `/tmp/farmcore-sharing03-browser.log`.

Fresh auxiliary checks: Ruff check/format466 files clean; Node30 passed0 skipped; ordered fixture
isolation4 passed1.88s; one Alembic head `review_0001`. Independent root HTTP review remains separate.

Final PR target is `codex/farmcore-revamp` (then `fe08924`), NOT main. The actual target merge-base
is `563b11ffffcc910a707793b7b4c8fedbcb2961aa`; final tier3 must include the complete common
DDL/models/policies plus recipient runtime against this base. `e89dcef` is only the independently
verified shared prerequisite and runtime-mutant restoration fingerprint, not the final PR gate base.

Independent root HTTP review subsequently BLOCKED `bcc003b`:3 failed11 passed14.29s.
Enabled wrong-identity/active bound/role/CSRF/overflow/mixed-private controls passed, but expired,
revoked and future bound-grant JSON errors exposed private grantee labels and validity/revocation
timestamps through the legacy `denied_message` helper. Generic HTML did not establish JSON privacy.
Retained RED: `/tmp/farmcore-access03-independent/test_recipient_boundaries.py`, parameterized
`test_next_read_status_and_identity_denial` expired/revoked/future. Earlier deliberate JSON-detail
preservation is superseded by the approved no-metadata denial requirement. A constant generic403
with explicit metadata-absence assertions is required; no implementation edits while root reviewer
finishes active probes. Prior browser/static evidence applies only to its frozen snapshot and is
insufficient for completion. Final QA/gate evidence must be renewed after this correction.

### Root HTTP correction and final-gate controls

Root's identical independent HTTP recheck on source `8e81c8e326e013aab758fc3a710e0879c43d835535ace0c86dcce80eaefd7792`:
16 passed24.77s; actual private mapped-model public-repository probe1 passed1.53s. No remaining
spec/standards finding. Renewed independent browser/static review: four journeys/16 error
navigations pass,192 recipient requests with zero private polls/errors/overflow, plus20 real
expired/revoked JSON assertions; source unchanged and all app/DB/state/TLS cleanup confirmed.
Report `/tmp/farmcore-sharing03-report-source-8e81c8e3.json`.

Five serialized runtime mutants killed (semantic assertion failures, no fixture errors): recipient
binding bypass, inactive-grant HTTP bypass, manifest-resource bypass, private advice-props leakage,
and private error-shell regression. Exact production bytes and original runtime diff restored;
source SHA above, then runtime-diff SHA `6cc1d7ffeefa008b6e519a58793567b0dfdd5998fc0d830c050b2dc1f66129b4`.
Reports `/tmp/farmcore-access03-runtime-mutants-result.json` and corresponding per-mutant logs.

First full true-base tier3:1861 passed,3 skipped,72 deselected672.06s, but overall FAIL:
changed coverage98% (145 lines,2 missing at share/routes.py487–488 advice-denial handler) and
scanner false positives for two synthetic private-payload marker assignments. Failed reports
preserved `/tmp/farmcore-access03-before-test-completion-{gate.json,junit.xml,coverage.json}`.
No production fix, threshold change or weakened assertion: test-only `b864d04` adds a real
register200 -> advice403 consent test with private stored advice, no denied-read audit and
unchanged grant scopes; accurately renames payload canary variables.46 recipient cases passed
33.70s; retained JSON confirms both formerly missing lines executed; Ruff466 files clean.
Scanner added-line candidates0; canonical self-test rejects4/4 known-bad, accepts2/2 benign and
keeps the allowlist line-scoped2/2. Production source hash unchanged, so preceding independent
HTTP/browser/mutant evidence remains applicable without duplicate execution.

Coverage blind spot discovered and fixed by integration coordinator: package-name source omitted
standalone dynamic Alembic files. The145-line metric therefore did NOT establish migration line
coverage. Verified config prerequisite `90419c8eb009728169cc03178b88d4b84b20da1f` changes only
coverage source to `src/farmcore` and its explanatory comment; merged at `c2eac05890131622b52c999f1c781d3ea6fa2194`.
Exact default bare `--cov` now includes core0004 15/15, operations0002 18/18 and review0001 28/28
executed lines in coordinator's migration check. Canary rejects old report's3 absent files and
accepts the new path-based report. Config gate17 passed12.56s, secrets clean. Existing functional
round-trip/property/manual-DDL-mutant evidence remains separate. No application/dependency changes.

Full true-base tier3 rerun is pending on the committed test correction and path-based configuration.
The same six directories, random seed3037958567 and separate ordered lifecycle checks are retained;
no source-code changes or repeated independent QA. Preflight dependency audit (triggered by generic
pyproject file detection) exit0/no known vulnerabilities; own editable FarmCore is not a PyPI package
and is explicitly unaudited by that tool.

### Final local evidence — tier3, actual PR base

PASS at `c2eac05890131622b52c999f1c781d3ea6fa2194`, actual target merge-base
`563b11ffffcc910a707793b7b4c8fedbcb2961aa`. Local implementation/review is verified; PR CI and
root actual-tip integration gate remain required before status changes to done.

| Acceptance criterion | Evidence |
|---|---|
| Explicit recipient binding; numeric URL insufficient | Real wrong-enabled-identity/anonymous/demo/legacy controls; independent16 HTTP probes;32 generated hostile-ID examples/64 denied requests with authorized control |
| Allowed farm/resources; expiry/revoke next read | Scoped real HTTP and private mapped-model probes; constant inactive403; browser next-read denial and no unauthorized polling |
| No ordinary audit/raw transcripts; audited reads | Stored advice whitelist and no unauthorized station query/audit; pending/transcript exclusion; mixed-sensitive whole-grant denial; persisted user/grant/farm/purpose assertions |
| Own-farm owner browser create/revoke; foreign denial | Four actual desktop/mobile×motion journeys; valid-CSRF foreign-owner denial; real concurrent double-revoke yields one event |

| Layer | Fresh result |
|---|---|
| Relevant ordinary regressions |1862 passed,3 skipped,72 deselected636.20s; random seed3037958567 |
| Changed executable coverage |100%:206/206 changed lines,0 missing; includes dynamic Alembic revisions |
| Dynamic migration coverage |core0004 15/15; operations0002 18/18; review0001 28/28, each0 missing |
| Python lint/format |Ruff clean;466 files formatted |
| Frontend |Node30 passed,0 skipped |
| Ordered lifecycle |4 passed1.88s, run separately without random reorganization |
| Schema head |Sole `review_0001` |
| Secrets |Clean,3 explicitly synthetic allowlisted lines; gate self-test rejects4/4 known-bad |
| Types |Skipped: no configured mypy/tsconfig |
| Dependencies |Generic skip: dependency set unchanged; separate installed-environment pip-audit exit0/no known vulnerabilities, own unpublished editable project explicitly unaudited |
| Real execution |Generic smoke convention absent; actual HTTPS/browser+HTTP/PostGIS execution above |
| Mutation |Generic tool absent;5 runtime mutants killed/restored; separate migration mutants/property/round-trip evidence above |

```bash
PYTEST_ADDOPTS='tests/core tests/apps/share tests/apps/chat tests/apps/assistant tests/apps/operations tests/apps/inventory -m "not data" --ignore=tests/test_conftest_isolation.py --randomly-seed=3037958567 -ra --junitxml=/tmp/farmcore-access03-final-junit.xml --cov-report=json:/tmp/farmcore-access03-final-coverage.json' bash ~/.claude/scripts/gauntlet.sh --tier=3 --base=563b11ffffcc910a707793b7b4c8fedbcb2961aa --suite-timeout=1800
uv run pytest tests/test_conftest_isolation.py -q --randomly-dont-reorganize
uv run ruff check src tests
uv run ruff format --check src tests
node --test tests/apps/fields/test_popup.mjs tests/core/test_overflow_discovery.mjs tests/core/test_navigation.mjs
uv run alembic heads
```

Reports `/tmp/farmcore-access03-final-{gate.json,junit.xml,coverage.json}` and auxiliary logs.
Production source remains `8e81c8e326e013aab758fc3a710e0879c43d835535ace0c86dcce80eaefd7792`;
source/test PR diff SHA256 `7d74f7d6552ff890733f25643aa021d3cb732a7f8e43ded0de15448073f24a89`.
The final evidence commit changes only this ticket; production/tests/config match the passing gate.

Spec/standards conformance: no remaining local findings. Failed then fixed: obsolete bearer/form
contracts, inactive landing metadata, broad legacy scopes, private HTML error shell and private JSON
error labels/timestamps; retained observed RED and independent rechecks above. Final gate also
corrected an untested consent-denial branch and coverage's dynamic-migration blind spot without
weakening guards, assertions, scanner patterns or thresholds.

Known limits: three assistant cases skip because SIGPAC/InfoRiego files are absent;72 data-marked
cases were excluded by the approved ordinary-test gate. Full data/whole-application milestone
closure remains ticket12. Operator-only preview uses legacy shared `grant:<id>` attribution;
individual operators cannot be distinguished by one shared operator token. No production deployment
or database migration performed. Worktree retained for root exact-head CI/review/integration.
