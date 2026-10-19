---
type: Work Ticket
title: "Private farm reads are closed by default"
description: "Milestones 1–4: private farm reads are closed by default."
status: in_progress
blocked_by: []
---

# 01 — Private farm reads are closed by default

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

This first slice closes private farm reads using the existing installation operator token; it does not by itself finish member authentication or all of milestone 1. Touchpoints: core access/startup/settings; web administrator checks; share routes; security/setup docs; HTTP tests. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain. Implementation workers propose schema changes; only the designated coordinator authors/serializes revision files. During access tickets 01–03, the access chat coordinator owns revisions exclusively; other chats request schema changes from the integration coordinator.

## Blocked by

None — can start immediately.

## Acceptance criteria

- [x] Private mode rejects anonymous farm pages, exports, fragments and assistant reads; health/static/login remain usable.
- [x] Existing installation admin token can access farms; forged tour/farm selectors and numeric grant IDs do not bypass privacy.
- [x] Demo mode is explicit and limited to designated demo farms; existing demo tests use deliberate fixtures. Keep separate anonymous-private, authenticated and explicit-demo clients; do not globally set test settings/client to demo.
- [x] Global model-call audit remains installation-admin-only; no private model payload metadata leaks.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [x] `uv run pytest tests/core tests/apps/share tests/apps/assistant tests/apps/chat -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [x] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [x] `uv run alembic heads` — one head after integration.
- [x] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [x] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Local verification passed on 2026-10-19 at `f8861ef8f4b8308ff20c177b44884a43e1638c3d`.
PR [8](https://github.com/LucasGeno/farmcore/pull/8) stays draft while exact-head CI is queued;
status remains in progress until that integration gate completes. This is ticket01 operator-token
privacy, not full M1/member/recipient authentication.

### Failure model and TDD

Tier3 failure model, recorded before verification: absent/forged operator token -> default-deny
HTTP boundary; hostile query/cookie/farm overflow -> authorization before lookup; foreign numeric
grant -> concealed denial before validity metadata; global model metadata -> installation-admin
checks before queries; unsafe demo/tour writes -> middleware and existing mutation guards;
new/manual-query HTTP routes -> default-deny boundary. Denied reads assert no audit side effects.
Sessions, member revocation and concurrent identity mutation belong to02.

Approved seam: real application HTTP requests and migrated throwaway PostGIS databases.
Observed RED before minimum GREEN: anonymous `/today/` returned200 instead of403; explicit demo
returned403 instead of200; foreign numeric grant returned200; concealed private grant returned403
instead of404; valid operator tour returned403; unknown demo selector returned200. Each was rerun
GREEN after its minimum change. Review retained the operator/tour authority distinction.

The first focused gate found two obsolete anonymous sanitized-audit expectations (1401 passed,
2 failed, 3 skipped, 58 deselected); exact reproduction was2 failed in1.64s. Approved spec makes
all global audit admin-only, including demos. Updated tests assert private/demo403, no metadata,
no audit access log and no `LlmAudit` SELECT; existing admin full-payload/time assertions remain.
Coverage was71/72 because the permitted operator grant-page audit probe lacked a positive test.
A real HTTP200 case now checks shared-audit UI and exact actor/farm/grant/scope/purpose audit row.
No production changes were needed for either final verification finding.

### Fresh tier3 evidence

Exact gate (real merge base `3b7cca7435b8b432e47bf41d6b0d6430efcc1c34`):

```bash
PYTEST_ADDOPTS='tests/core tests/apps/share tests/apps/assistant tests/apps/chat -m "not data" --randomly-seed=19012026 --cov-report=json:/tmp/farmcore-access01-final-coverage.json' bash ~/.claude/scripts/gauntlet.sh --tier=3 --base=3b7cca7 --suite-timeout=1800
```

| Layer | Fresh result |
|---|---|
| Focused HTTP/core regression | 1404 passed, 3 skipped, 58 deselected in247.39s; seed19012026 |
| Changed executable lines | 72/72 covered, 0 missing, 100%; retained coverage JSON independently counted |
| Ruff check / format | `uv run ruff check src tests`: clean; format check444 files |
| Migration | `uv run alembic heads`: single `inventory_0001 (head)`; no schema edits |
| Frontend | `node --test tests/core/*.mjs tests/apps/fields/*.mjs`:30 passed,0 failed/skipped |
| Secrets | Gauntlet scan clean; self-test rejects4/4 bad controls, accepts2/2 benign controls |
| Dependency audit | Exit0; local `farmcore` not on PyPI and not auditable; runtime dependencies unchanged |
| Types | Skipped: no configured mypy or tsconfig; not reported as passing |
| Mutation tool | Skipped: unavailable; manual fallback below |
| Execution convention | No `.gauntlet-smoke`; actual fresh-DB smoke below substitutes |

Fresh manual mutants, applied individually after test freeze: unset token becomes admin;
remove demo authorization; remove foreign-grant farm check; remove chat audit guard. All4 killed
by HTTP assertions. Exact staged production-source restoration checked with `git diff --exit-code
-- src`; restored private suite26 passed in1.75s. One earlier mutant survived because a tour
cookie masked the token path; absent-tour coverage was added and the final4/4 run killed it.

Hypothesis:30 generated examples /180 HTTP denial requests across private and foreign-demo
selectors, forged cookies/headers and tour flags; exact403 and unchanged access-log count.
The function-scoped fixture health-check exemption is deliberate: idempotent setup and denied
requests share the rollback connection, with side effects checked per example. Bounded ASCII
alphabet and example count are not exhaustive fuzzing.

Separate unchanged lifecycle harness: `uv run pytest tests/test_conftest_isolation.py
--randomly-dont-reorganize -q` ->4 passed in0.79s. Its documented writer-before-reader contract
fails under global random reordering in the pre-feature baseline; ordinary selected tests remain
randomized and no assertion was weakened.

Both exact legacy visitor regressions were rerun: `uv run pytest
tests/apps/advisor/test_debug_panel.py::test_anonymous_pages_leak_nothing_even_with_debug_1
tests/apps/operations/test_isoxml_routes.py::test_upload_form_is_shown_to_admins_only -q`
->2 passed in3.13s. Private anonymous403 is checked first; demo designation is then local to only
the seeded farm and original no-debug/no-upload-form assertions remain.

Manual app smoke used a new migrated PostGIS database, seeded a farm, exercised real HTTP and
dropped the database: health200 `{"db":"ok"}`, anonymous `/today/`403, operator fields export200
`FeatureCollection`. Reproducible public-seam equivalent:
`uv run pytest tests/core/test_private_access.py::test_operator_and_public_bootstrap_remain_usable -q`.

### Independent QA and limits

Fresh feature reviewer: no Spec/Standards findings; final test-only delta also no findings,
including SQL observation without mocks and exact access-log attribution. Fresh integration
reviewer: no Spec/Standards findings;8 independent adversarial real-DB HTTP probes passed
in1.46s (one TestClient cookie deprecation warning). Reviewed production source remained
unchanged: diff SHA256 `bcc6207892405f97d0af19ab4844ec4ea47deb6cc4ec94ce48c977322e1729a0`.

Three skips are optional Spanish SIGPAC/InfoRiego data, not passes.58 data-marked cases are
outside this approved focused gate. The whole-branch full-data regression belongs to12.
An exploratory full-data coverage run was stopped at1275.70s (623 passed,12 skipped,2 legacy
visitor failures); interrupted/empty logs are incomplete evidence. Those exact visitor failures
were fixed and rerun above. A recovered run exited3 because Docker/PostGIS was down (22 pure
tests passed before exit); parent restored only the existing dev DB and the final gate is fresh.
No source skip/xfail or relaxed security assertion was added.

Member sessions/CSRF and recipient-bound grants remain02/03. Shared assistant capture remains
installation-admin-only. No production deployment or migration was performed. Exact-head
GitHub CI remains a separate pending merge gate, never inferred from local evidence.
