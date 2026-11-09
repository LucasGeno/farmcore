---
type: Work Ticket
title: "Independent QA verifies milestones 1–4"
description: "Milestones 1–4: independent qa verifies milestones 1–4."
status: done
blocked_by: ["03", "06", "11"]
---

# 12 — Independent QA verifies milestones 1–4

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Independent QA verifies milestones 1–4. Touchpoints: whole branch review; migration chain; regression/browser/security tests; README/SECURITY. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain. Implementation workers propose schema changes; only the designated coordinator authors/serializes revision files. During access tickets 01–03, the access chat coordinator owns revisions exclusively; other chats request schema changes from the integration coordinator.

## Blocked by

- [03](./03-authenticated-sharing.md)
- [06](./06-recommendation-review.md)
- [11](./11-job-completion.md)

## Acceptance criteria

- [x] Fresh QA agent reports spec gaps separately from standards findings; all blocking findings fixed.
- [x] Real PostGIS role/expiry/revoke/CSRF/retry/concurrency/rollback regressions and browser journeys pass.
- [x] One Alembic head, fresh-install upgrade and relevant existing-database upgrade pass.
- [ ] verify tier3 evidence includes failure model, test/lint/frontend/build/security results and manual fallbacks for missing tooling; PRs integrated only after QA.
  - Ruling-accepted exception, not met: the milestone PRs were merged before QA under Lucas's
    2026-11-07 ruling (head-recorded in `run-2026-11-06-48h.md`), and QA ran post-merge on
    `main`. The tier-3 evidence clause is verified on the merged tree; the sequencing clause
    is recorded as an exception, not claimed.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [x] Scoped tier-3 command that actually passed on merged HEAD `ef11ff4`: `SCOPED_DIFF=$PWD/.qa-scratch/qa12-milestone-scope.diff bash .qa-scratch/gauntlet-scoped.sh --tier=3 --base=bf3ae8475470dcb531ec0bb100a1adf8f251a28f --suite-timeout=1800` through `fleet-build --min-free-gib 30.15 --wait 21600 --timeout 3600 --workdir $PWD`, plus `node --test tests/core/*.mjs tests/apps/fields/*.mjs` — verdict pass, scoped coverage 100% (4550 changed lines, 0 missing). Instructed: `bash ~/.claude/scripts/gauntlet.sh --tier=3 --base=origin/main; node --test tests/core/*.mjs tests/apps/fields/*.mjs`; the scoped run is the recorded adaptation.
- [x] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [x] `uv run alembic heads` — one head after integration.
- [x] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [x] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Claim 2026-11-07 21:25 CET: fresh QA agent `fc12-qa` (OpenCode Go `opencode-go/deepseek-v4.1-flash`, M1; wrote none of tickets 01-11), worktree `~/dev/farmcore-worktrees/12-integration-qa`, branch `qa/12-integration-qa`. Fixes for blocking findings go to a separate writer; reviewer Dell Codex. Coordinator and migration author: `fleet-head-opus`. Stuck deadline 2026-11-08 13:00 CET.

Pending implementation. No acceptance criteria verified yet.

### Tier-3 integration failure model

| Failure mode | Required verification layer |
|---|---|
| Anonymous/foreign/expired/revoked identity reads private data or global audit | Real HTTP role matrix across pages, exports, fragments and assistant; generated hostile selectors; manual authorization mutants |
| Cookie mutation bypasses CSRF or role checks | Real HTTP absent/foreign token cases plus browser form journeys |
| Weather retry/failure destroys last good receipt; future receipt leaks into replay | Job/CLI with controlled provider failures and separate-session races; clock-bounded HTTP/store assertions; missingness properties |
| Parameter activation affects another farm or rewrites historic assessment | Real HTTP version/review/activation history and unchanged assessment snapshots; deterministic engine examples |
| Stale or concurrent plan edit loses decisions; resource overlap accepted | Separate committed database sessions, revision conflicts, adjacent/overlap boundary properties |
| Duplicate correction/completion applies stock effect twice or fails partially | Retry and barrier-coordinated concurrent HTTP confirmations, canonical source identity, forced transaction rollback and computed-stock assertions |
| Re-evaluation runs before commit or after failed transaction | Public confirmation/completion outcome and persisted assessment assertions across independent sessions |
| Browser form, focus, refresh or HTMX response breaks the authorized journey | Real-browser login/review/plan/replan/complete/correct paths and console/response checks |
| Schema upgrade or intermediate integration leaves parallel heads/data loss | One-head assertion, fresh migration, existing-database upgrade/round trip and metadata check |

### Pre-feature baseline

At `d0f44f7` product source (integration docs through `0b577c4`): non-data regression `uv run pytest -m 'not data' -q` reports **2656 passed, 3 skipped, 268 deselected in 311.40s**; Node tests **30 passed**; Ruff check/format **442 files pass**; Alembic head **inventory_0001**. Full randomized suite (seed **1025145244**) completed **2901 passed, 25 skipped, 1 failed in 1450.04s**. Exact inherited failure: `tests/test_conftest_isolation.py::test_row_is_gone_in_the_next_test`, `_require_writer("session")` asserted `session in _WROTE` but `_WROTE` was empty. This module explicitly requires writer-before-reader order. Minimized module-only reproduction with the same seed: **1 failed, 3 passed in 1.74s**; changing only `--randomly-dont-reorganize`: **4 passed in 1.51s**. No product changes were present. Final gates randomize ordinary tests with `--ignore=tests/test_conftest_isolation.py`, then execute those four lifecycle tests separately with `--randomly-dont-reorganize`; assertions remain unchanged and ordered evidence is reported separately. Feature coverage gates use their approved `-m "not data"` selection; full regression including available data runs separately without coverage instrumentation. Incomplete coverage/data diagnostic runs never count as passed. These are baseline results, not milestone acceptance evidence.

Verification dependencies installed only in local environment: pytest-cov, diff-cover, hypothesis, pytest-randomly. Mypy is unconfigured/unavailable; it is not reported passed. No mutation runner or repository smoke script is configured: final tier3 uses documented manual mutants and actual application execution. Built-in secrets gate self-test rejected **4/4** bad controls and accepted **2/2** benign controls.


### Browser auth regression — final aggregate blocker (2026-10-19)

A real visible Chromium journey on PR8 `cef01d7` with a disposable migrated database completed anonymous denial, wrong-token denial, operator form login, secure/HttpOnly/Strict cookie, operator audit access, logout cookie removal, post-logout denial and forged-tour denial. The overall browser check **failed** its zero-page-exception assertion: operator sign-out redirects to protected `/`, which returns JSON403 and produces native `InvalidStateError: Transition was aborted because of invalid state. ViewTransition opt-in disabled` with normal motion. The identical reduced-motion control produced no exception, while both denied private access. Eight public sign-in/health navigation loops did not reproduce it.

The public sign-in screen also polls notifications while logged out and displays a notification failure banner. Ticket02 owns a public sign-in logout landing and removal of private widgets/polling from logged-out bootstrap screens, with HTTP/browser regressions for the real landing and absence of unauthorized widget requests. Preserve anonymous403/no metadata behavior. Final aggregate/main merge remains blocked until the actual browser journey passes after this fix; no full browser pass is claimed for ticket01. Report/scripts: `/tmp/farmcore-access-browser-qa.md`, `/tmp/playwright-test-farmcore-private-access.js`, `/tmp/playwright-test-farmcore-logout-repro.js`; screenshot `/tmp/farmcore-access-browser-login-mobile.png`. The fixture server shut down and its disposable database was dropped.


### Preliminary composed validation — 2026-10-19

Before feature merges, exact frozen01/04 patches composed cleanly on aeb7ece in an isolated checkout, staged tree `53e48c630df30f58b08e051a7fc675ccced3ee3a`. Tier2 ordinary randomized non-data regression (seed19012026): **2726 passed,3 skipped,268 deselected in501.29s**, changed coverage100%, lint/secrets/dependency audit clean; unconfigured types/smoke and tier2 mutation skipped. Ordered lifecycle4passed1.62s and Node30passed separately. Gate JSON identifies HEADaeb7ece; the staged tree above, not that HEAD alone, is the tested composition. After PR7/8 merges, `git diff --quiet` confirms src/tests/pyproject.toml/uv.lock byte-identical to integration546b077. Full available-data regression on this composition is still running; not a pass or final12 acceptance. No02 code included. Report `/tmp/farmcore-combined-qa-gauntlet.json`.


Full available-data regression on the same composed source completed: `.venv/bin/pytest --ignore=tests/test_conftest_isolation.py --randomly-seed=19012026 -q` → **2972 passed,25 skipped in950.62s**. No ordinary case failed; all four unchanged ordered lifecycle cases passed separately above. Source/test/dependency equivalence to actual integration546b077 was verified before completion; this is strong01/04 regression evidence, not final02–12 acceptance. Log `/tmp/farmcore-combined-qa-full-data.log`.


### Inherited quantity-order assertion — diagnosed and corrected

The frozen01 baseline and02 actual-base gate both failed `tests/core/test_logs.py::test_confirm_merge_null_product_uses_measure_label_unit[rate-sulphur-kg/ha-True]` with the same copper/sulphur order difference. Root retained the02 JUnit trace; no other ordinary failure appeared in that gate (1764passed,3skipped,72deselected,312.48s). This result was not reported passing.

Ranked hypotheses: unordered database reload, wrong merge values, fixture leakage. A minimal copy of the exact four-case test added only real PostgreSQL `CLUSTER quantity USING (id DESC)` before expiration/reload on a disposable database. Original assertion: **3failed,1passed in1.01s**; all added-quantity cases returned identical tuples in reversed physical order. `Log.quantities` has no `order_by`; the adjacent product-ID merge test already normalizes tuple order. This confirms physical row order, with neither a changed merge value nor fixture leakage needed.

Only that existing assertion now compares sorted exact `(measure,label,unit,value)` tuples to sorted expected tuples. Multiplicity, quantity IDs/reassignment, merged target, total row count and event payload checks remain. No production, fixture, skip, selector, xfail or threshold change. Same physical-order probe **4passed in0.80s**; deliberate expected dose5→6 control still **3failed,1passed in0.86s**. Fresh independent reviewer confirmed no semantic weakening/missing check. Root tier1 verification (test-harness-only, real basef3461c5): **29passed in1.11s**, randomized seed3037958567, lint/secrets clean; types unconfigured, coverage/audit/execution/mutation skipped at tier1. Reports `/tmp/farmcore-quantity-order-{red.log,green.log,gate.json}` and `/tmp/farmcore-quantity-wrong-dose-red.log`. Subsequent02 full gate must include this correction and remain fully randomized.


### Member integration and bootstrap browser disposition

Ticket02 reviewed source563b11f integrated atdca8477 after exact-head Python/Node/Docker CI. Root actual-tip focused tier2 passed1783tests,3skipped,72deselected327.86s,cov100%; ordered4passed2.16s,Node30passed,singlecore_0003. `git diff --quiet` confirms source/tests/dependencies identical to the frozen source that passed independentHTTP6probes4.40s and four actual browser journeys after the final corrections. Those browser journeys show public sign-out landing, no logged-out private polling/page exceptions, unreflected credentials and preserved Sunlight. The original bootstrap browser finding above is closed; remaining final12 review/planning/correction/completion journeys remain unverified and this ticket stays incomplete. Reports `/tmp/farmcore-access02-browser-qa.md`, `/tmp/farmcore-auth02-browser-final.log`, `/tmp/farmcore-post-member-gate.json`.


### Serialized schema foundation — bounded tier3 verification

Frozen source `e89dcef248bd82f11ab5e2cddc0058cf93732599` on `codex/farmcore-revamp-migrations` composes the original03 recipient revision,05 parameter models/policies and09 correction models/policies. The integration coordinator exclusively authored `operations_0002` and `review_0001`, following literal `core_0004_grant_recipients`. The sole head is `review_0001`. These are shared schema/policy prerequisites, not completion of03/05/09 or a feature integration/main merge.

Failure model written before verification: model/revision drift, legacy-row rewrite or recipient inference, evidence lost through FK cascades, duplicate retries/partial terminal metadata, cross-scope parameter ancestry, mutable parameter history and false-positive gates. Controls use public Alembic, real PostGIS constraints/transactions and the public scoped repository. Correction service lifecycle/stock effects, parameter approval business rules, HTTP authorization and browser workflows remain their feature tickets' responsibility.

Observed schema RED: public metadata comparison failed once for four missing modeled tables (2.75s). Coordinator DDL made the migration tests pass; no ORM-generated migration imports or parallel heads. Fresh final verify tier3 command:

```sh
PYTEST_ADDOPTS='tests/core/test_schema.py tests/core/test_revamp_migrations.py tests/core/test_access.py tests/core/test_correction_scope_policy.py tests/core/test_registry.py tests/core/test_apps_routes.py tests/core/test_apps_page.py tests/core/test_startup.py tests/apps/operations/test_routes.py tests/apps/share -m "not data" --randomly-seed=19012026 --hypothesis-show-statistics --junitxml=/tmp/farmcore-foundation-gate-junit.xml' bash ~/.claude/scripts/gauntlet.sh --tier=3 --base=f4c9c4effa1c68d20e1b0ba2bae5cc6232b24f2f --suite-timeout=1800
```

This bounded prerequisite gate compares the verified original03 schema ancestor `f4c9c4e`; it is not a feature PR-target gate. The first feature PR carrying this foundation must additionally gate its entire actual target diff: for frozen03 `bcc003b` targeting integration `fe08924`, `git merge-base` returns `563b11ffffcc910a707793b7b4c8fedbcb2961aa`, including all shared DDL/models/policies and03 runtime. Bounded foundation result: **246 passed,4 deselected in30.23s**, changed coverage **100%**, Python lint/secrets clean. Types unconfigured, unchanged dependency audit skipped, no changed shell files. No configured smoke/mutation runner: actual public HTTP catalogue/startup and independent disposable-database Alembic execution provide the execution fallback; four manual mutants below provide the mutation fallback. Separate ordered lifecycle **4passed1.89s**, Node **30passed,0skipped**, Ruff format **10files unchanged**. The final gate covers the category fix below; earlier green subsets are not substituted for this run.

Before this final run, the wider gate failed **1failed,245passed,4deselected31.45s**: the new review manifest lacked a category and rendered under special-last Other, violating the real app-catalogue contract. Isolated unchanged HTTP test RED **1failed2.33s**; adding only `category="Crop protection"` GREEN **1passed1.76s**. Independent reviewer reran that same public test **1passed1.36s**, no new findings. No existing assertion, category sorting, skip or fixture was changed.

Four plausible faults planted one at a time were all killed: removed retry unique constraint (**1failed1.83s**); live-log SET NULL changed to CASCADE, destroying evidence (**1failed1.57s**); omitted parameter UPDATE/DELETE triggers (**1failed1.54s**); removed parameter-version private resource policy (**6failed18passed0.27s**). Exact bytes restored and git diffs clean after each; these three files' source is unchanged by the subsequent category-only fix. Runtime scope matrix tests use public Repo.check and prove policy configuration, not feature HTTP acceptance. Generated FK and retry-key checks each exercise20examples, including explicit blank/length boundaries.

Fresh independent review separates Spec/Standards and found no blocking issue in this bounded foundation. Seven real execution groups passed: public Alembic upgrade/metadata, cross-farm/region/year ancestry, review/activation linkage, append-only UPDATE/DELETE, actual scoped repository denial/farm filtering, downgrade function cleanup/legacy-farm retention, and re-upgrade/metadata. Disposable database dropped. Root round-trip tests also preserve canonical rows and NULL legacy recipient bindings and retain correction snapshots/retry identity after log/creator deletion. Parameter history guards protect against application UPDATE/DELETE; unrestricted database owners can disable triggers. Correction immutability/atomic stock effects remain application-service acceptance, not a schema-only claim.

Reports: `/tmp/farmcore-foundation-gate-final.json`, `/tmp/farmcore-foundation-gate-junit.xml`, `/tmp/farmcore-foundation-mutants.json`, `/tmp/farmcore-schema-foundation-integration-qa.md`, `/tmp/farmcore-schema-qa-probe.py`, `/tmp/farmcore-foundation-{ordered.log,node.log,heads.log,format.log}`. This ticket remains incomplete; no final milestone acceptance follows from foundation verification.

### Independent QA report — 2026-11-07 (`fc12-qa`)

Fresh independent QA. This agent wrote none of tickets 01–11 and changed no `src/` or `tests/`
file: every probe and walkthrough below lives in the worktree-local, git-excluded
`.qa-scratch/` directory. Worktree `~/dev/farmcore-worktrees/12-integration-qa`, branch
`qa/12-integration-qa` (claim `377192f`), HEAD at review `377192f` on main `d42d7db` (milestones
1–4 merged). Reviewed diff base: `bf3ae8475470dcb531ec0bb100a1adf8f251a28f`, the first parent of
the revamp merge `f1a8255`, whose schema head is `inventory_0001` (the two pre-revamp revision
files are byte-identical at base and HEAD). Environment: M1, Python 3.12.13, PostgreSQL 17.11 +
PostGIS on `localhost:55442`; no Playwright. Disk guard checked before every heavy run
(`df -k /System/Volumes/Data` ≥ 225,000,000 KiB available against the 31,614,567 KiB floor).
`tests/instances` was never run locally.

#### Verification results

| Check | Command / method | Fresh result |
|---|---|---|
| One head, linear chain | `uv run alembic heads`; `uv run alembic history`; Alembic `ScriptDirectory` assertions | `planning_0003 (head)`; one linear 12-revision chain `core_0001 → core_0002 → forecast_0001 → inventory_0001 → core_0003 → core_0004_grant_recipients → operations_0002 → review_0001 → forecast_0002 → planning_0001 → planning_0002 → planning_0003`; no branch labels or dependencies |
| Fresh-install upgrade | Disposable `farmcore_qa12_fresh_*` database: `alembic upgrade head` | PASS; `planning_0003`, 1 alembic row, 39 tables including every milestone table; `uv run alembic -x url=… check` rc 0 (no ORM/revision drift) |
| Existing-database upgrade | Disposable `farmcore_qa12_upgrade_*`: `alembic upgrade inventory_0001`, insert legacy farm/parcel/product/advice/two named grants, then `alembic upgrade head` | PASS; legacy rows preserved, both grants keep `recipient_identity_id = NULL` (no name backfill), zero identities/memberships inferred, advice props byte-equal; downgrade to `inventory_0001` and re-upgrade preserve the same rows. Both scratch databases dropped |
| Focused regression | `timeout 600 uv run pytest -q tests/core -m 'not data and not slow' --randomly-seed=12012026` then the same for `tests/apps`, through `fleet-build` | core **760 passed, 2 deselected in 245.26s**; apps **2123 passed, 3 skipped, 262 deselected in 505.18s**; both exit 0 |
| Gauntlet tier 3 | `PYTEST_ADDOPTS="tests/core tests/apps -m 'not data and not slow' --randomly-seed=12012026 …"` + `fleet-build --min-free-gib 30.15 --wait 21600 --timeout 3600 -- bash ~/.claude/scripts/gauntlet.sh --tier=3 --base=bf3ae84 --suite-timeout=1800` | verdict **fail**: tests **2883 passed, 3 skipped, 264 deselected in 1391.30s**; suite_health, lint_python, lint_shell pass; `coverage_changed` **80% (5869 changed lines, 1126 missing)**; `secrets` **fail**; types/deps/execution/mutation skipped honestly (details below) |
| JavaScript contracts | `node --test tests/core/*.mjs tests/apps/fields/*.mjs` | **32 passed, 0 failed/skipped** |
| Lint/format | `uv run ruff check src tests`; `uv run ruff format --check src tests` | clean; **542 files already formatted** |
| Container/Compose layer | Not run locally (starts and removes Docker containers). Cited hosted CI on reviewed main `d42d7db`: [run 42410650123](https://github.com/LucasGeno/farmcore/actions/runs/42410650123) — **python**, **node**, **docker** and **slow** jobs all `success`; the python job ran `scripts/check.sh`, the slow job ran `pytest -q -m slow` (the `tests/instances` journeys) |
| Independent PostGIS probes | `timeout 600 uv run pytest .qa-scratch/qa12_probe -q -p no:randomly` against the real app and a throwaway migrated PostGIS database | **7 passed** — real role matrix, CSRF origin+nonce, wall-clock session expiry, membership revocation, bound-grant expiry/revocation/wrong-identity, forged session and foreign selectors, completion retry, failed-transaction rollback, two-session concurrent completion, rejected completion intent |
| Browser-journey manual fallback | `timeout 600 uv run python .qa-scratch/qa12_walkthrough.py` — real HTTPS uvicorn on a free port, disposable migrated PostGIS database, seeded scene B, CLI accounts, then HTTP/HTML forms | **13/13 steps passed**; uvicorn stopped (SIGTERM), database dropped |

The three skips are the optional SIGPAC/InfoRiego fixtures, not passes. `pytest-cov`,
`pytest-randomly` and `diff-cover` were installed only in this worktree's `.venv`; they are not
repository dependencies.

#### Tier-3 failure model exercised here

| Failure mode | Independent evidence |
|---|---|
| Anonymous/foreign/forged identity reads private data or metadata | Anonymous `/today/` 403 without farm text; owner of farm A gets 403 + no metadata for farm B selectors and `/operations/register.json`; forged opaque session cookie 403; zero-membership recipient gets 404 for a foreign grant, 403 for expired/revoked grants, and never sees the grantee label |
| Cookie mutation bypasses CSRF or role checks | Advisor `POST /planning/jobs` 403 with no row; owner same-origin POST without the server nonce 403 and with a forged nonce 403, no row; valid nonce 201; correction cancel without the nonce 403; browser walkthrough uses the same HTML forms |
| Expiry/revocation take effect on the next request | Session `expires_at` moved into the past → next `/planning/jobs.json` 403 with no job name; membership `revoked_at` → next read 403; binding grant `valid_to`/`revoked_at` edited → next advice export 403 |
| Retry/double tap/concurrent completion duplicates work or stock | Same request key returns the same operation id; two pooled-session TestClients complete one job concurrently → both 200, one `Log`, one `Quantity`, one completion revision, one stock movement (`10 − 1.5 × 2 ha = 7 kg`) |
| Failed transaction leaves plan/log/stock unchanged | Farm-scoped idempotency-key collision → 409 with the second job still `planned`/revision 1/no operation, log count unchanged, stock unchanged; invalid dose units (`cups`, `kg`) → 422/400 with no canonical row, no revision, no stock movement |
| Re-evaluation only after commit | Covered by the repository suite (`test_advisor_re_evaluation_runs_only_after_a_committed_completion`) and the commit-before-background-task seam; not re-run as a separate probe |
| Schema upgrade or intermediate integration leaves parallel heads/data loss | Fresh install, pre-revamp existing upgrade, downgrade/re-upgrade and metadata check above; one head |

#### Browser-journey manual fallback (no Playwright)

Exact HTTP/HTML-level walkthrough executed against a real uvicorn (`uvicorn
farmcore.main:create_app --factory`, high port, self-signed TLS) with a disposable database
created, migrated, seeded (`farmcore seed --scene B`, `farmcore replay --to "2024-04-10 06:00"`)
and dropped by the script; the server is stopped before the script exits. Steps, all through
the same forms a browser submits (`Origin` + `farmcore_csrf` / hidden `csrf_token`):

1. `GET /health` → 200 `{"db":"ok","alembic_heads":["planning_0003"],"state":"ok"}`.
2. Anonymous `GET /today/` → 403 and no farm text.
3. `GET /login` → 200; `POST /login` (`login`, `password`, `csrf_token`) → 303 with
   `farmcore_session`/`farmcore_csrf` set `HttpOnly; Secure; SameSite=strict`.
4. Owner `GET /today/` → 200; `GET /operations/register` → 200.
5. `GET /farms/members` → 200; `POST /farms/members` (`login=qa-advisor`, `role=advisor`) → 303;
   the advisor appears on the page.
6. `GET /review/assessments/1/card` → 200; `POST /review/assessments/1/request-form`
   (`note`) → 303 with `requested=1`.
7. `POST /logout` → 303; next `GET /today/` → 403.
8. Advisor `POST /login` → 303; `GET /review/recommendations/` → 200 and the
   `action="/review/requests/<id>/response-form"` form; `POST` it (`opinion=agree`, `note`) → 303.
9. Owner `GET /planning/new?parcel=1&assessment=1` → 200; `POST /planning/jobs-form`
   (`name`, `parcel_ids`, `assessment_id`, `product_id`, `scheduled_start`, `duration_minutes`)
   → 303; `/planning/` renders the completion form for the new job.
10. `POST /planning/jobs/<id>/complete-preview-form` (actual start/duration/parcel/dose) → 200
    preview; `POST /planning/jobs/<id>/complete-form` → 303; `/operations/register` shows the
    confirmed spray.
11. `GET /operations/logs/<id>/corrections` → 200; `POST
    /operations/logs/<id>/corrections/preview` (`reason`, one corrected dose, parcels, time,
    request key) → 303 to the receipt; `POST /operations/corrections/<id>/confirm`
    (`confirm=yes`) → 303; the receipt and the `quantity` row show the corrected `0.4 kg/ha`
    (was `0.5`).
12. `POST /operations/corrections/<id>/cancel` without the CSRF nonce → 403.

Residual limits of this fallback: it is HTTP/HTML, not a rendering engine, so it proves form
wiring, CSRF, redirects, status codes and persisted state, not layout, focus or motion. The
instructed `tests/instances` Compose journeys are covered by the green hosted CI above.

#### SPEC GAPS (spec/acceptance criteria not met)

| ID | Ticket | Criterion and location | Evidence / repro | Severity | Status |
|---|---|---|---|---|---|
| SG-1 | 11 — job completion | "Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence" is unticked and the ticket remains `in-progress` with no closeout: `docs/work/farmcore-revamp/tickets/11-job-completion.md:5`, `:40`, `:179-180`. The feature is merged (`d42d7db`) and behaviour passes this QA. | `sed -n '5p;40p;179,180p' docs/work/farmcore-revamp/tickets/11-job-completion.md`; completion behaviour repro is the 7 independent probes above plus `test_completion.py`/`test_completion_concurrency.py`. | Medium | NON-BLOCKING (record a reviewer closeout like tickets 06/07/10) |

No product-level spec gap was found for milestones 1–4: every acceptance criterion in tickets
01–10 is exercised by the selected suite (2883 passed) and independently by the probes and
walkthrough above. The remaining spec/acceptance risk is evidence recording, not behaviour.

#### STANDARDS FINDINGS (code quality, security, docs drift)

| ID | Area | Finding and location | Evidence / repro | Severity | Status |
|---|---|---|---|---|---|
| ST-1 | Docs drift — security/setup | The setup guide still tells operators that Sharing and recipient grants are operator-only and that ordinary members get only a read-only assistant: `docs/self-hosting.md:181-185`. This contradicts the shipped owner browser grant management and recipient-bound sharing described at `README.md:125-130` and in `SECURITY.md:12-30`. `SECURITY.md:183` separately says advisor recommendation-review writes "arrive with the review feature; advisors currently read", although advisors now answer requests (`review/routes.py:523`, `review/recommendations.py:173`). | Owner creates/revokes a bound grant in the browser (`tests/apps/share/test_recipient_identity.py::test_owner_browser_binds_existing_login_and_revokes_next_recipient_read`, `::test_owner_shell_exposes_sharing_navigation`); advisor response returns 201/303 (`tests/apps/review/test_recommendation_review.py:94-106`). My walkthrough steps 5–8 are the same authority path. | Low | **BLOCKING** (docs-only fix; update `docs/self-hosting.md:181-185` and `SECURITY.md:183`, and add recommendation-review response to the advisor row at `SECURITY.md:24`) |
| ST-2 | Security gate | The tier-3 secrets layer fails on two credential-shaped strings added by the operator backup/restore work (outside milestones 1–4): `tests/instances/test_bundle_checks.py:339` (`secret = "AGE-SECRET-KEY-1QQQQ…"`, a synthetic placeholder used to prove a pasted private key is refused) and `tests/instances/test_isolation.py:484` (`install(password=HOSTILE_PASSWORD, …)`, a constant name). Neither is a usable credential, but the gate is red, so the security layer cannot be reported green. | Reproduced with the same matcher: `git diff -U0 bf3ae84...HEAD \| grep '^+' \| grep -v '^+++' \| grep -Ei 'secret…' \| grep -v gauntlet:allow-secret` → the two lines above. The repository already uses `# gauntlet:allow-secret` on six synthetic lines. | Low | **BLOCKING** (one-line marker on each synthetic line; no production change) |
| ST-3 | Verification scope | `coverage_changed` reports 80% (5869 changed lines, 1126 missing) against the instructed whole-base diff. The diff spans post-revamp main work whose tests live in `tests/instances` (Compose/backup/restore/upgrade): `src/farmcore/core/commands` plus `apps/fields/commands.py` alone add **2,410** source lines, and this QA is forbidden to run that suite locally. With only the milestone test directories, the milestone source itself measures **93%** (3334 statements, 231 missing); each feature ticket also records 100% changed-line coverage on its own diff. | `git diff --numstat bf3ae84 HEAD -- src/farmcore/core/commands src/farmcore/apps/fields/commands.py` → 2410 added lines; focused report `.qa-scratch/qa12-milestone-coverage.txt`; hosted CI slow job green on `d42d7db`. Recommendation: run tier 3 in CI (or accept a milestone-scoped base) rather than locally. | Medium | NON-BLOCKING |
| ST-4 | Ticket artifact drift | Ticket 03's four Verification boxes remain unticked although the ticket is `done` and its closeout says the final gate passed: `docs/work/farmcore-revamp/tickets/03-authenticated-sharing.md:34-37`. | `sed -n '34,37p' docs/work/farmcore-revamp/tickets/03-authenticated-sharing.md`; the closeout at `:311-321` records the passing gate. | Low | NON-BLOCKING |
| ST-5 | Docs drift — README | The user-facing feature list still describes only the pre-revamp apps; planned jobs, recommendation/parameter review, member accounts and record corrections are absent from `README.md:44-66` even though they are first-class pages. | `sed -n '44,66p' README.md`; compare the manifests and routes under `src/farmcore/apps/{planning,review,share}` and `/operations` corrections. | Low | NON-BLOCKING |

#### Disposition

Counts: **0 blocking spec gaps** and **2 blocking standards findings** (ST-1, ST-2; both Low
severity, both docs/comment-only fixes), plus 1 non-blocking spec gap and 3 non-blocking
standards findings. The instructed tier-3 gauntlet verdict is **fail** for the two reasons in
ST-2 and ST-3; its test, randomized-order, Python-lint and shell-lint layers pass, and the
missing frontend/build/security/execution evidence has the documented fallbacks above. No
`src/` or `tests/` file was changed by this QA; the working tree is clean apart from this
report, and every scratch database and uvicorn process created for it was removed.

This ticket stayed `in-progress` at first review: a separate writer was to fix ST-1 and ST-2
(documentation and test-comment only), record the ticket 11 closeout (SG-1), and the maintainer
was to decide ST-3 (milestone-scoped base or CI tier 3). **Superseded by the re-verification
below**, where the fixes are merged and checked.

Evidence files (worktree-local, git-excluded): `.qa-scratch/qa12-migrations.log`,
`qa12-focused-shards.log`, `qa12-gauntlet.log`, `qa12-gauntlet-junit.xml`,
`qa12-independent-probe.log`, `qa12-browser-fallback.{log,json}`, `qa12-walk-pages/`,
`qa12-milestone-coverage.{txt,xml}`, `qa12-uvicorn.log`; scripts `qa12_migrations.py`,
`qa12_probe/`, `qa12_walkthrough.py`.

### Re-verification after fixes — 2026-11-08 (`fc12-qa`)

Fetched `main` from `https://github.com/LucasGeno/farmcore.git` and merged normally (no rebase):
merge commit `bfbc30c`, parents `54c273e` (this report) and `a56be65` (#60), which also contains
#59 `40f8329`. The merged tree changes only docs, ticket sources and two test comments; no
product source changed. CI on the fix head `a56be65` is green:
[run 42422127386](https://github.com/LucasGeno/farmcore/actions/runs/42422127386) (python, node,
docker, slow all successful).

| Finding | Status | Re-verification evidence |
|---|---|---|
| SG-1 — ticket 11 not closed | **RESOLVED** | `11-job-completion.md:5` is `status: done`, all five Verification boxes are `[x]`, and `:182` records the Dell Codex review, green CI and PR #58 merge. |
| ST-1 — stale sharing/advisor docs | **RESOLVED** | `docs/self-hosting.md:181-188` now states that owners capture/confirm sprays, advisors answer recommendation-review requests, owners manage Sharing grants in the browser, and only active bound grants reach a recipient. `SECURITY.md:24` grants advisors "answer recommendation-review requests … with an opinion and note" and `SECURITY.md:183-184` replaces the stale "writes arrive with the review feature" sentence. Read against `share/routes.py`/`review/routes.py` and the passing share/review suites (`test_owner_browser_binds_existing_login_and_revokes_next_recipient_read`, `test_recommendation_review.py:94-106`). |
| ST-2 — secrets layer false positives | **RESOLVED** | `tests/instances/test_bundle_checks.py:339` and `tests/instances/test_isolation.py:492` now carry `# gauntlet:allow-secret`. Exact `SECRET_RE` replay over `git diff -U0 bf3ae84...HEAD` with the allowlist removed: **0 unmarked hits, 9 allowlisted lines at that point** (10 after #62). Fresh gauntlet on the merged tree (tier 1, `--base=bf3ae84`): **verdict pass**, `secrets: ok — no secret patterns in the diff (9 line(s) allowlisted)`, lint and tests pass. |
| ST-3 — coverage scope and figure | **RESOLVED** | Head ruling applied: the gate is scoped to milestones 1–4; operator installation/backup/restore/upgrade work is out of scope. After #62 the final scoped tier-3 run passes with **scoped coverage 100% (4550 changed lines, 0 missing)**. Pre-#62 figure was 94.2%; #62 adds the missing milestone tests and six Codex-accepted `pragma: no cover` lines, each with a stated invariant. Scoping and layers below. |
| ST-4 — ticket 03 verification boxes | **OPEN (recorded NON-BLOCKING)** | Left exactly as recorded; not part of this re-verification. |
| ST-5 — README feature list | **RESOLVED** | `README.md:57-75` now lists audited record corrections, field observations/irrigation, private member accounts, authenticated sharing, reviewed advice and persistent planned jobs. |

Coverage re-measurement and final tier-3 run, stated exactly:

- Scope (unchanged from the recorded milestone scope): `git diff bf3ae84...HEAD` restricted to
  milestone 1–4 product paths — every changed file under
  `src/farmcore/apps/{activity,advisor,assistant,chat,forecast,inventory,kpi,notifications,planning,review,share,stations,today}`,
  `apps/fields/{service.py,static,templates}`, and
  `apps/operations/{corrections.py,facts.py,manifest.py,routes.py,templates}`; `core/**` except
  the operator installation/backup/restore/upgrade commands; plus `core/commands/users.py`,
  `main.py`, `settings.py`, `web.py`, and `migrations/versions/**` (revision files only).
  Explicitly excluded under the head ruling: `core/commands/{apps,backup,effects,farms,migrate,restore,upgrade}.py`,
  `core/startup.py`, `db.py`, `apps/demo_es/**`, `apps/fields/commands.py`,
  `apps/operations/isoxml.py`, and `migrations/env.py` (the supplied-connection branch used only
  by the operator `farmcore migrate` path; its tests are the green `tests/instances` CI job).
- Fresh tier-3 command on merged HEAD `ef11ff4` (main `cf0792c`), run through
  `fleet-build --min-free-gib 30.15 --wait 21600 --timeout 3600 --workdir $PWD`:
  `SCOPED_DIFF=$PWD/.qa-scratch/qa12-milestone-scope.diff bash .qa-scratch/gauntlet-scoped.sh
  --tier=3 --base=bf3ae8475470dcb531ec0bb100a1adf8f251a28f --suite-timeout=1800`, with
  `PYTEST_ADDOPTS="tests/core tests/apps -m 'not data and not slow' --randomly-seed=12012026 …"`.
  The only difference from `~/.claude/scripts/gauntlet.sh` is the coverage layer's
  `--diff-file="$SCOPED_DIFF"` instead of `--compare-branch="$BASE"` (recorded diff shown above);
  the diagnostic copy of the XML is retained only for this evidence.
- **Verdict: pass.** Layers: tests **2942 passed, 3 skipped, 264 deselected in 1404.96s**;
  suite_health ok (pytest-randomly, randomized order); lint_python and lint_shell ok;
  `coverage_changed: Coverage: 100%` (**4550 changed lines, 0 missing**); secrets ok (10
  allowlisted lines, 0 unmarked); types/deps/execution/mutation skipped honestly (no mypy or
  tsconfig, dependency set changed but no pip-audit/npm audit, no `.gauntlet-smoke`, no mutation
  tool).
- Two earlier runs of the same scoped gate failed at 99% with 4 missing lines. Those four lines
  are `src/farmcore/migrations/env.py:103-106`, the operator `farmcore migrate` supplied-connection
  branch; the recorded scope is `migrations/versions/**`, so the first two diff files were
  over-broad. With the recorded scope they are out, and the coverage layer is 100%.
- Pre-#62 history for the record: 94.2% (4342 lines, 250 missing). #62 covered those milestone
  branches with tests and left six `pragma: no cover` lines, each with a stated invariant
  (recommendations.states filtering in notifications, Span/forecast non-empty guarantee, FK
  guarantees for the completion operation, the advice parcel FK in two review reads, and "other
  assignment constraints are pre-validated"); Codex accepted all six across four review rounds.

Other re-verification results on the merged tree:

- `uv run alembic heads` → `planning_0003 (head)`; `history` still one linear chain.
- `node --test tests/core/*.mjs tests/apps/fields/*.mjs` → **32 passed, 0 failed/skipped**.
- `uv run ruff check src tests` → clean; `uv run ruff format --check src tests` → **542 files
  already formatted**.
- Independent PostGIS probes re-run on the merged tree → **7 passed**.
- Browser fallback re-run with a replan step added → **14/14 passed**: anonymous denial, owner
  login cookies, advisor assignment, owner request, advisor answer, job create, **job replan via
  `/planning/jobs/<id>/edit-form`**, preview/complete, correction to `0.4 kg/ha`, CSRF refusal;
  uvicorn stopped and the scratch database dropped.
- No blocking finding remains. ST-3 is resolved by the passing scoped run above; ST-4 remains the
  single recorded non-blocking item (ticket 03's unticked verification boxes). The only unmet
  acceptance clause is the "PRs integrated only after QA" sequencing, recorded above as a
  ruling-accepted exception rather than a pass.

Acceptance criteria 1–3 and every Verification box are ticked on this evidence. Criterion 4 stays
unticked because of its sequencing clause, as instructed by the head review; every other
criterion is proven and the ticket status is `done`. New evidence:
`.qa-scratch/qa12-tier3-scoped3.log`, `qa12-tier3-diffcover.log`,
`qa12-tier3-coverage.xml`, `qa12-milestone-scope.diff`; earlier secrets/re-verification logs
remain under `.qa-scratch/`.

### Integrated acceptance follow-up — 2026-11-08

Reviewed current `main` **`f4ed86661f4770fb8bcab0bbd794d56185ff3d6a`** in isolated
`~/dev/farmcore-worktrees/integrated-acceptance-20261108`. The initial pass made no product
changes; subsequent screenshot review found the terminal-card defect below. Acceptance is
qualified until that fix is integrated.
This adds a real rendering-engine journey to the earlier HTTP/HTML fallback; it does not
change the recorded pre-QA merge sequencing exception.

Fresh checks, all substantial execution through `fleet-build --min-free-gib 30.15`:

| Check | Actual result |
|---|---|
| `uv run pytest -q tests/apps/review tests/apps/planning tests/core/test_revamp_migrations.py -m 'not data and not slow'` | **189 passed**, no skips, 108.46s. Includes role/CSRF isolation, overlap/revision conflicts, separate-session booking/completion races, retry and rollback. |
| `node --test tests/core/*.mjs tests/apps/fields/*.mjs` | **32 passed**, no failures/skips. |
| Ruff lint/format of `src tests`; `alembic heads`; `git diff --check` | Pass; **552** formatted files, sole head **`planning_0003`**, clean whitespace. |
| Visible Chromium **153.0.8010.12**, real HTTPS uvicorn, migrated/seeded disposable PostGIS database | **18/18 checks passed**; zero page exceptions and HTTP 5xx; desktop and 390px mobile screenshots captured, mobile has no horizontal overflow. See the terminal-card finding below. |

The browser used ordinary member forms and default motion: anonymous HTML redirects to
sign-in without private metadata, anonymous JSON is 403, owner signs in with installation-scoped
Secure/HttpOnly/Strict cookies, assigns an existing advisor, requests review of saved
assessment #1, and the advisor records an opinion visible on that exact assessment. The owner
then creates a job from that assessment, configures a sprayer/operator and both availability
windows, books them, replans from 08:00/90 minutes to 09:00/60 minutes, previews actual work at
05:00 with 0.5 kg/ha, and explicitly confirms. The identical completion form retry returns
303; refresh shows one completed job and the confirmed register renders. Fixture dates and
weather are replayed demo inputs; no agronomic accuracy or current-weather claim is made.

Independent database/inventory assertions after browser completion and retry:

- Job #1 is `completed`, revision **4**, linked to confirmed operation #11. History is
  `(1, created), (2, edited [booking]), (3, edited [replan]), (4, completed)`.
- Original assessment row is byte-equal. The planning source retains assessment #1 and plan
  revision **3**; the terminal history snapshot keeps both resource assignments.
- Active bookings are released (**0** assignment rows). Exactly **one** new planning source
  and one product quantity exist, including after retry.
- Actual dose **0.5 kg/ha × 5.55 ha = 2.775 kg**. Inventory service reports used
  **0 → 2.775 kg**, on hand **150 → 147.225 kg**, spray count **0 → 1**.
- Final database `farmcore_qa12_walk_00aa14c0` was dropped; uvicorn was terminated and reaped
  (exit -15). A separate catalog read found no remaining `farmcore_qa12_walk_*` databases.

Local evidence: `/tmp/farmcore-acceptance-focused-20261108.xml`,
`/tmp/farmcore-acceptance-integrated-20261108.json`,
`/tmp/farmcore-acceptance-browser-20261108.json`,
`/tmp/farmcore-acceptance-{planned,completed-mobile}-20261108.png`.
Harness: `.qa-scratch/run_acceptance.py`, `.qa-scratch/qa12_helpers.py`, and
`/tmp/playwright-test-farmcore-acceptance-20261108.js`; the first two are local ignored files,
not permanent regression coverage. Final integrated JSON SHA-256:
`c5f1b481c01b7d8dfa211b076c57b4052843a122193bacc024e8ca73be637c99`.

Harness failures were retained as limitations, not product regressions: the default Playwright
browser revision was absent, so the installed cached Chromium was selected explicitly;
initial assertions expected API-style anonymous denial and unscoped cookie names, and were
corrected to existing tested browser contracts. One immediate post-login navigation attempt
reported Chromium `net::ERR_TOO_MANY_RETRIES`; waiting for navigation's network-idle terminal
state produced the final complete pass. This is one successful final browser journey, not a
claim of long-run browser stability. Preflight collected 3383 tests but reported unavailable
`mypy`; no type-check pass is claimed. Full default/data/slow/Compose suites, live providers,
production routing, and load tests were not rerun by this follow-up.

#### Terminal-card display finding

Subsequent review of the completed-mobile screenshot found a **product defect**, not a harness
failure: after completion releases active bookings, the card says no machine/operator is
booked and shows future resource/capacity warnings and scheduling alternatives. Both resource
names remain in the saved terminal revision, but the card renders only live assignments.
The 18 browser assertions above did not assert this wording; their pass does not establish
correct historical resource presentation. Canonical completion/stock/history assertions still
hold. A separate fix uses the saved terminal revision for displayed resources and suppresses
live feasibility/alternatives on completed and cancelled cards; planned-job warnings remain.
The public-page regression reproduced both terminal cases before the fix (**2 failed**),
then passed after the minimum change (**2 passed**).

Draft [fix PR #64](https://github.com/LucasGeno/farmcore/pull/64), frozen
`087a12bbe9ef6a657a5b287a75679f8a3a1f2c86`, passes **191** focused planning/review/migration
tests (96.17s), lint/format/whitespace and one-head checks. A new Chromium journey on that
exact commit passes **19/19** checks, including saved resource names and absence of future
booking/feasibility alternatives on the completed card; stock still changes exactly once by
**2.775 kg**. The new database was dropped and server stopped. After screenshot:
`/tmp/farmcore-terminal-history-completed-mobile-20261108.png`; it was inspected separately
from the unchanged before screenshot. Exact-commit JSON:
`/tmp/farmcore-terminal-history-integrated-20261108.json` (SHA-256
`4d4dd53854a70f72fb446debdbec4c933a6782332cb7cec0ad8501f324d8c578`);
regression XML `/tmp/farmcore-terminal-history-focused-20261108.xml`.
Independent review and hosted CI remain pending; #64 is not merged, so the display finding
remains open on current main. No migration, booking-release, completion, stock or authorization
behavior was changed by this display fix.

#### Draft #50 reassessment

[PR #50](https://github.com/LucasGeno/farmcore/pull/50) remains **open, draft and untouched**,
head **`345008c31958aa3669685b1fdf3cd0e11e700cf5`**. It supplies operator-only private fleet
inventory validation, `plan`, one-installation `apply`, ownership/credential journals and
`status`. It is **not a prerequisite** for recommendation review, resource planning or
completion. No hosting manager/customer portal is supplied.

Its four CI jobs are green on that old head
([run 42212570872](https://github.com/LucasGeno/farmcore/actions/runs/42212570872)); this does
not verify the present integrated tree. Current main has **97** commits absent from the PR,
and the PR has **11** absent from main. A disposable detached checkout merged the two without
committing: **no conflicts**, tree **`2c3c01161b01a7d93e9301d2dee5696522332330`**,
27 changed files relative to current main (4838 additions, 20 deletions). The merge retains
`planning_0003` in the instance-test helper and has one Alembic head. PyYAML becomes a direct
runtime dependency without a package-version change; the pyasn1 security pin already exists
on main. Restore's role-catalog busy translation remains a separate behavior change.

| Current-main + #50 check | Fresh result |
|---|---|
| `uv run pytest -q tests/core/test_fleet_plan.py tests/instances/test_data_guard.py tests/instances/test_private_bytes_guard.py` | **243 passed**, 25.21s. `/tmp/farmcore-hosting-composed-20261108.xml`. |
| `uv run pytest -q tests/instances/test_fleet_apply.py -m 'not slow'` | **39 passed**, **45 deselected**, 43.48s. `/tmp/farmcore-hosting-state-20261108.xml`. Exercises bounded state/refusal contracts; it does not provision installations. |
| Focused hosting Python lint, whitespace, Alembic head | Pass; **`planning_0003`**. |

The first composition run reused main's editable-install environment and had **2 failed,
241 passed**: audited subprocesses intentionally clear `PYTHONPATH`, so those two imported
main's CLI without `fleet`. A dedicated environment installed from the composed checkout
resolved the harness mistake; the exact 243-case rerun above passed. No source/assertion
change or skip was used. No Compose provisioning, restore or full integrated CI was run
for the new composition; old-head green CI is not substituted for those missing checks.

Merge danger remains operator provisioning: persistent resources and partial state survive
failure; state-root/Docker trust, image availability, external HTTPS routing/certificates and
configured-limit rather than OOM-stress evidence remain as documented in #50. Fleet commands
do not upgrade, move or delete installations. The bounded checks found no new blocker,
but do not establish release readiness.

**Next action:** keep #50 draft; update it by ordinary merge from current main, retain the
current migration head, and rerun its complete Python/Node/image/slow-instance CI on that
new head before a merge decision. This report changes neither #50 nor any deployment, live
installation, production service, external-effects switch or paid-model configuration.
