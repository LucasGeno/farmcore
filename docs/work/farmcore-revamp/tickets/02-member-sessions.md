---
type: Work Ticket
title: "Local identities, farm memberships and role enforcement"
description: "Milestones 1–4: local identities, farm memberships and role enforcement."
status: done
blocked_by: ["01"]
---

# 02 — Local identities, farm memberships and role enforcement

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Local identities, farm memberships and role enforcement. Touchpoints: core identity models/session/login/CLI; scoped repositories and farm switcher; web route guards; single Alembic migration. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain. Implementation workers propose schema changes; only the designated coordinator authors/serializes revision files. During access tickets 01–03, the access chat coordinator owns revisions exclusively; other chats request schema changes from the integration coordinator.

## Blocked by

- [01](./01-private-access-default.md)

## Acceptance criteria

- [x] CLI bootstrap creates local owner securely; no public signup, credentials or session tokens in URLs/logs.
- [x] Owner/advisor memberships, opaque hashed sessions, expiry/logout/revocation, rate limits and CSRF enforced.
- [x] Farm selectors/switcher/listing and read/write routes enforce membership; advisor cannot write operations or change installation settings.
- [x] Owner can assign/revoke farm memberships; revoked memberships and disabled users lose access on the next request. Never infer user ownership from legacy grant names.
- [x] Before ordinary-user capture is enabled, remove shared WEB_CHAT_ID/default-farm routing from web writes and bind draft/confirmation to authenticated principal plus farm; otherwise affected paths stay installation-admin-only and this limitation is explicitly recorded.
- [x] Role checks hold across browser and programmatic routes; installation admin break-glass remains documented.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [x] `uv run pytest tests/core tests/apps/share tests/apps/chat tests/apps/assistant -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [x] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [x] `uv run alembic heads` — one head after integration.
- [x] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [x] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Worktree `farmcore-member-sessions`, branch `codex/farmcore-member-sessions`, initially based on
frozen ticket01 PR8 head `cef01d7`. After exact-head CI, normal PR8 merge and the integration
gate passed, changes were safely carried onto verified integration/docs tip `669b398`, then `b98168a` including the root-owned quantity assertion correction. Ticket01
is complete in the integration tracker. Ticket02 remains in progress until its own final gate,
exact-head CI and integration checks pass.

Tier3 failure model (recorded before final verification):
- Credential guessing, spoofed peers or process restart -> bounded persisted account/peer/global
  budgets; atomic reservation must survive failed-login rollback; real concurrent-session checks.
- Token disclosure/fixation, replay expiry, disabled/revoked identity -> opaque hashed sessions,
  fresh nonces, wall-clock expiry and next-request DB validation; no URL/log credentials.
- CSRF and role confusion -> cookie mutation proof/origin checks and explicit owner/advisor/admin
  role matrix over browser and programmatic HTTP; operator header remains distinct break-glass.
- Forged/default/stale farm selector, exports/fragments/tools -> explicit current membership on
  every farm read/write, no identity inference from legacy grant names; no unauthorized metadata.
- Shared `WEB_CHAT_ID`/default-farm capture/history -> keep affected paths installation-admin-only
  until principal+farm binding exists; ordinary imports must bind member actor and selected farm.
- Concurrent membership removal/partial schema changes -> transactional authorization checks,
  one coordinator-authored linear migration and real migrated DB round-trip/preservation tests.
- Public sign-in private polling/logout JSON/native transition exception -> HTTP landing/layout
  assertions plus real browser normal/reduced-motion and narrow-screen journeys.

Observed TDD progress (not final completion evidence): CLI bootstrap first RED `No such command
users` (1 failed,3.54s); GET login404 ->200; schema-head RED `inventory_0001 != core_0003`
(1 failed,0.83s). Coordinator alone authored `core_0003_identity.py` onto `inventory_0001`.
Eight schema tests passed in1.87s, including metadata/no-autogenerate diff, round trip and old
farm/owner/named-grant preservation with zero inferred identities/memberships. No production
migration was executed.

Public operator-auth UI TDD: HTTP RED showed notification polling on sign-in and logout final403.
Minimum GREEN uses public auth layout and logout303 `/admin/sign-in`. Admin sign-in regression14
passed in1.35s. Own HTTPS browser fixture exercised4 journeys: desktop1280/mobile390 × normal/
reduced motion; wrong token, secure/HttpOnly/Strict cookie, successful login, public HTML logout,
cookie removal, private/demo403, zero page errors/private notification requests/horizontal overflow.
Native transition error resolved through correct HTML landing; no animation/CSS asset changes.
Settled mobile screenshots inspected; disposable DB/server/credential files cleaned. Independent
UI review found dropped saved Sunlight contrast preference. HTTP RED1 failed/14 deselected
in0.78s, then conditional public-shell contrast attribute restored. Admin suite15 passed in1.22s;
fresh mobile390 browser confirmed18px text, high-contrast ink, no overflow/poll requests/page errors.
Screenshot inspected and fresh DB/server/credential files cleaned. Reviewer recheck closed the Sunlight finding;
auth core and final tier3/role/browser QA remain incomplete.

Member browser verification (before final auth freeze): four HTTPS journeys passed on identical
start/end source digest `c234b8cd4da717765fd674d2d749535cb84a605a23fd70f6f8910357d8eb2bef`: desktop1280/
mobile390 × normal/reduced motion, with fresh migrated PostGIS and CLI-bootstrapped accounts.
Actual owner forms assigned/revoked advisor membership; advisor lost access next request. Advisor
write and foreign farm reads denied; selectors switched assigned farms. Missing/forged CSRF left
DB state unchanged. Actual production form changed only the selected second farm and persisted
AccessLog actor `user:<owner id>` plus selected farm; primary farm remained unchanged. Native
logout landed on public HTML and removed access. Zero page errors, public-login notification
requests or horizontal overflow; settled screenshots inspected. Disposable server/DB and credential/
TLS files cleaned. Import/inventory writes are covered by HTTP tests, not browser submission.
Coordinator schema recheck: eight passed in2.26s; `alembic heads` reports only `core_0003`.
Final tier3 gauntlet, mutations and fresh acceptance review remain incomplete.

Inherited baseline established on immutable ticket01 `cef01d7`: matching six-directory non-data
run with seed3037958567 produced1 failed,1701 passed,3 skipped,72 deselected in167.26s. Sole failure:
`tests/core/test_logs.py::test_confirm_merge_null_product_uses_measure_label_unit[rate-sulphur-kg/ha-True]`,
quantity list returned sulphur before copper. Same node/trace occurred in ticket02 broad run.
The standalone case and complete test_logs file pass at that seed (1/29 tests respectively);
no quantity implementation or assertion changed in ticket02. Integration coordinator owns
unrelated baseline disposition; this is not a passing regression claim.

Pre-final auth audit found that global Form-validation errors can reflect password input and
render private widgets for malformed public login requests. Identity worker owns a real-HTTP
RED and minimal generic standalone auth-error response; fresh QA confirms finding. Production
freeze and final browser/security evidence must be renewed after correction.

Exact inherited-baseline command (frozen ticket01):
```bash
uv run pytest tests/core tests/apps/share tests/apps/chat tests/apps/assistant tests/apps/operations tests/apps/inventory -m 'not data' --ignore=tests/test_conftest_isolation.py --randomly-seed=3037958567 -q --tb=short
```
Matching baseline trace at `tests/core/test_logs.py:434`: expected first quantity
`('rate', 'copper', 'kg/ha', Decimal('2'))`, actual first quantity
`('rate', 'sulphur', 'kg/ha', Decimal('5'))`. Root reserves disposition for ticket12.
Corrected ticket02 broad run before auth-error correction:1759 passed,3 skipped,72 deselected
in187.56s with the same selection/seed. New collection/context did not reproduce the inherited
ordering failure; no exclusions, production ordering edits or assertion changes were used.
This historical broad pass does not replace the required final post-correction gate.

Final independent acceptance QA cleared candidate all-source digest
`722514543561c0ad8c34a94ed1535d968edb17f0421e4ab4e7fb24af35caf2f8`:96 passed in30.72s
(seed902102), real migrated PostGIS, covering full member/schema/admin-sign-in/error suites
and three independent replay/stale-CSRF/decoded-capture/disabled-owner-cleanup probes. All six
acceptance criteria reviewed; no remaining spec/standards findings or test weakening. Linear
`core_0003` migration, principal+farm import attribution and operator-only shared capture checked.
Login-error finding: five observed RED cases ->32 passed in3.19s after auth-specific generic
standalone no-store HTML/JSON handling. Browser recheck at pre-cleanup digest `a0b2114c…11eb1`:
14 actual HTML error submissions, two JSON422 checks and two normal login/logout smokes; no
credential reflection, private shell, page errors, public polling or horizontal overflow. Saved
Sunlight preference preserved; settled screenshot inspected, disposable fixture cleaned.
Disabled co-owner finding: real HTTP RED1 failed in2.31s, minimal guard correction ->4 passed
in4.35s including actual last-owner and concurrent revocation protection. Disabled-target
assignment remains422. Final independent QA reproduced this correction GREEN. The final
cleanup delta touched membership authorization only; auth/browser files remain unchanged.
Tier3 mutations/broad gauntlet and exact-head CI remain outstanding at this point.

Manual mutation fallback on final candidate:5 plausible auth bugs planted separately and all
killed by existing public-seam tests: expired session accepted, disabled identity accepted,
account budget raised5->6, forged CSRF accepted, advisor write authorized. Every mutated file
was restored byte-for-byte; git diff equality and final all-source digest `72251454…caf2f8`
confirmed. The first narrow CSRF probe selected the role test, which did not exercise a forged
nonce; corrected selection used existing valid-Origin/session-bound-nonce tests and killed the
mutant. No source or test change was needed. Final authoritative run killed5/5.

Integration carryforward: stash/fast-forward/restore completed without conflicts onto
`669b398988461fbb3a593abae760025330ac520b`. Source+test feature patch remained byte-identical
(SHA256 `f0aec147ff2c59826cbb7ef1edb6ced40ddf76c30bd8467f40ba82a51fcaa17c`), with zero overlap
between the integrated weather source/tests and ticket02 changes. Fresh independent QA verified
this equality, clean diff and sole migration head; its acceptance review applies unchanged.
Combined all-source digest is now `bdd62aa6f25935e79de7fa9f37a1d1a5b32e72e3fa12ed9f424ce66842df8e85`.
Fresh ordered lifecycle checks:4 passed in1.34s; Node contracts:30 passed,0 skipped.

An interim stack-base gauntlet was deliberately interrupted when the integration base cleared:
1249 passed,1 failed,1 skipped,72 deselected before KeyboardInterrupt at71.57s; no completion or
coverage pass is claimed. Its retained pytest cache identifies the sole non-mutant failure node
as the same null-product quantity-order baseline case above. Original interim trace is unavailable:
gauntlet retained only trailing summary lines and deleted its temporary directory. Five other
cached failures are the exact deliberate manual mutants, not additional regression findings.
This limitation is explicit; the final uninterrupted actual-base gate retains external JUnit
and coverage JSON so any recurrence has its own complete failure trace.

First complete actual-base tier3 gate at `669b398` (before final boundary correction) returned
FAIL:1 failed,1764 passed,3 skipped,72 deselected in312.48s. Retained JUnit now confirms the
exact inherited quantity node and identical copper/sulphur ordering trace from frozen01. No
new ordinary test failure occurred. Changed coverage failed95% (448 statements,19 missing);
Python lint and secrets passed. No configured type checker, changed dependencies, smoke-file
convention or mutation tool: skipped honestly; manual execution/browser/mutant evidence above.
Retained JSON identifies missing meaningful CLI/auth/selector/error cases for regression work;
no threshold reduction or assertion weakening is allowed.

Root independent review added one P2: a valid owner posting an out-of-bigint-range membership
revoke ID receives500. Root reproduced three malformed boundaries with state unchanged and
normal in-range unknown404 control. Worker owns minimal same-generic404 positive-bigint guard
and persisted public-HTTP regression; final source freeze/review/mutants/gate must be refreshed.
Root independent four browser journeys also passed before this boundary correction, with no
private polling/page errors and preserved Sunlight/logout behavior; quick corrective recheck pending.

## Final local evidence — tier3, awaiting exact-head CI/integration

Tested staged source on actual merge base `b98168a56a74b57c1c1ea9241a1a95e3cb6a91d6`:
all-source SHA256 `bc2128aaeab8c66baeb99aad731ea0c11ee8321fe90e14347a909b5cd5f279b0`;
source+test feature patch SHA256 `dd434608044b1317b869fa8d15a46213dc4eac61fc48c8ff912f3862a43427b3`.
These fingerprints, not the base commit alone, identify the implementation tested before commit.

| Acceptance criterion | Verified by |
|---|---|
| Secure CLI bootstrap, no public signup or exposed credentials | CLI stdin/hidden-prompt bootstrap, rollback, password recovery and unknown-identity tests; public auth validation probes |
| Sessions, roles, expiry/logout/revocation/throttling/CSRF | Real HTTP session/hash/expiry tests, persisted concurrent login budgets, session-bound nonce/replay probes and5 fresh killed mutants |
| Farm isolation and owner/advisor write boundaries | Hostile-selector property tests, switcher/form/read-tool routes, owner/advisor imports/inventory tests and browser farm/actor mutation checks |
| Explicit membership administration and immediate denial | Assignment/revoke/disabled identities, last-enabled-owner and concurrent revocation tests; disabled-owner cleanup, bigint boundaries and real deleted-farm races |
| Shared capture limitation and real actor/farm routing | Ordinary shared capture/history denied; completed-only projections; principal-bound read conversation and persisted import actor+farm assertions |
| Browser/programmatic guards and break-glass | Explicit operator-header/cookie Origin tests, documented CLI recovery/settings boundary, four final root browser journeys |

| Layer | Fresh result after final code edit |
|---|---|
| Tier3 gauntlet | PASS;1783 passed,3 skipped,72 deselected in318.22s; seed3037958567 |
| Changed-line coverage |100%;452/452 changed statement lines,0 missing from retained JSON |
| Python lint/format |Ruff passed;456 files formatted |
| Secrets |Clean;3 explicitly allowlisted synthetic test values |
| Property/concurrency |Hostile farm selectors; persisted parallel login budget; owner revocation/operation lock ordering; real deleted-farm table-lock races |
| Mutation |Tool absent;5/5 manual mutants killed, exact source bytes/git diff/global digest restored |
| Independent feature QA |114 passed in37.03s, seed902102; all6 criteria reviewed, no findings or weakened tests |
| Independent integration QA |6 HTTP probes passed in4.40s; boundary/cleanup findings closed |
| Actual browser |Root four desktop/mobile × normal/reduced journeys passed; zero errors/private polling, Sunlight and public logout preserved; fixture cleaned |
| Ordered lifecycle / JavaScript |4 passed in1.28s separately;30 Node tests passed,0 skipped |
| Schema |Sole head `core_0003`; migration/metadata/preservation/round-trip checks included |
| Types / dependency audit |Skipped: no configured checker; dependency set unchanged |
| Execution convention |No `.gauntlet-smoke`; manual real HTTPS/CLI/PostGIS execution and persisted mutation evidence above |

Exact final gate:
```bash
PYTEST_ADDOPTS='tests/core tests/apps/share tests/apps/chat tests/apps/assistant tests/apps/operations tests/apps/inventory -m "not data" --ignore=tests/test_conftest_isolation.py --randomly-seed=3037958567 -ra --junitxml=/tmp/farmcore-access02-final-v2-junit.xml --cov-report=json:/tmp/farmcore-access02-final-v2-coverage.json' bash ~/.claude/scripts/gauntlet.sh --tier=3 --base=b98168a56a74b57c1c1ea9241a1a95e3cb6a91d6 --suite-timeout=1800
```

Spec conformance: no remaining findings. Failed then fixed: public auth credential reflection/
private widgets, disabled co-owner cleanup, bigint revoke IDs and deleted-farm fallback. The
inherited unordered quantity assertion was corrected separately by integration commit `575711d`;
exact multiset/duplicates/fields/counts/IDs/events remain checked, with deterministic physical-row
reordering RED and wrong-dose mutant evidence in ticket12. No access-workstream quantity edit.

Known limits: three assistant cases skip solely because optional SIGPAC/InfoRiego files are absent;
this is the focused non-data gate, not the final whole-app/data qualification. One defensive
foreign-selector raise has a reasoned per-line exclusion because PrivacyMiddleware rejects that
input before dispatch; actual boundary denial and empty-selector handler are tested. Ordinary web
capture remains installation-operator-only. Status stays in progress pending pushed-head CI and
root's actual integration-tip gate; local evidence does not claim either.


## Completed integration gate — 2026-10-19

Frozen563b11ffffcc910a707793b7b4c8fedbcb2961aa source/tests exactly match the staged source fingerprint above. Hosted [CI39290788752](https://github.com/LucasGeno/farmcore/actions/runs/39290788752) completed Python,Node,Docker successfully. PR11 merged normally into integration atdca8477c6d5ccfbba153eab6383ccbb6bf6266ef,17:12:34UTC.

Actual-tip tier2 gate, real premerge tip6665278 and same six directories/seed3037958567: **1783passed,3skipped,72deselected in327.86s**, changedcoverage100%, lint/secretsclean. Types unconfigured, unchanged-dependency audit, repository smoke convention and tier2 mutations skipped honestly; feature tier3 manual execution/mutation evidence remains above. Ordered lifecycle **4passed in2.16s**, Node **30passed,0skipped**, Alembic solecore_0003head. Source/tests/pyproject/lock byte-identical to reviewed frozen563b11f. Reports `/tmp/farmcore-post-member-{gate.json,junit.xml,ordered.log,node.log}`.

Ticket02 complete; authenticated recipient grants remain03, review/capture mutations remain their own tickets. Original public bootstrap browser blocker is closed on this identical reviewed source (four independent root journeys), not a claim that all final12 journeys pass. No main merge or deployment.
