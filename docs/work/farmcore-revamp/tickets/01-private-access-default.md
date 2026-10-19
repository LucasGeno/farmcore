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

- [ ] Private mode rejects anonymous farm pages, exports, fragments and assistant reads; health/static/login remain usable.
- [ ] Existing installation admin token can access farms; forged tour/farm selectors and numeric grant IDs do not bypass privacy.
- [ ] Demo mode is explicit and limited to designated demo farms; existing demo tests use deliberate fixtures. Keep separate anonymous-private, authenticated and explicit-demo clients; do not globally set test settings/client to demo.
- [ ] Global model-call audit remains installation-admin-only; no private model payload metadata leaks.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [ ] `uv run pytest tests/core tests/apps/share tests/apps/assistant tests/apps/chat -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [ ] `uv run alembic heads` — one head after integration.
- [ ] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Tier-3 failure model (recorded before final verification): missing/forged token is caught by
HTTP middleware and `is_admin`; hostile query/cookie/overflow farm selection by farm
read authorization; private numeric grants by the grant-farm check before validity details;
global audit metadata by installation-admin guards even in demo; unsafe demo/tour mutations
by middleware, `require_admin` and tour guard. Middleware denies newly added/manual-query
HTTP routes by default. HTTP assertions check denied reads create no access-log rows.
Concurrent session/member mutations are ticket02, not implemented here.

TDD public seam: approved spec HTTP requests against real migrated throwaway PostGIS.
Observed RED: anonymous `/today/` 200 instead of403 (1 failed,13.34s), then GREEN8passed.
Explicit demo RED403 instead of200, then GREEN9passed. Private grant RED200 instead of403,
then GREEN10passed. Concealed private grant IDs RED403 instead of404, then minimum uniform404.
Exact verification commands/results follow after final run.

Additional TDD: valid operator tour was RED403 at `/tour` (1 failed,1.61s), then credential
read authority separated from tour-disabled admin actions. Unknown demo selector was RED200
instead of403 (1 failed,1.67s), then demo authorization moved before farm lookup.

`verify` tier3, targeted evidence (2026-10-18):
- `uv run pytest tests/core/test_private_access.py -q --cov=farmcore.core.privacy --cov-report=term-missing`:
  **24 passed in3.73s**; new middleware **30 statements,0 missed,100%**.
- Deterministic generated invariants:8 hostile selector strings ×3 endpoints =24 denied requests,
  using each selector simultaneously as query/cookie/forged operator token; exact generic403 and
  unchanged access-log count. A second6-selector demo sweep verifies equal403 responses for
  negative/zero/unknown/overflow/non-numeric/SQL-like selectors.16 private-default cases span
  8 public HTTP surfaces ×tour present/absent. This bounded sweep is not exhaustive fuzzing.
- Manual mutation fallback:4 plausible bugs, each applied separately, focused real HTTP tests
  returned exit1, original file restored in `finally`. Mutants: unset token becomes admin;
  remove demo farm authorization; remove foreign-grant farm restriction; remove chat audit
  admin guard. First unset-token mutant survived because tour masked credential evaluation;
  adding absent-tour cases killed it. Final **4/4 killed**; restored suite **22 passed in2.06s**
  before2 additive tests; final24 passed above. `git diff --check` and production diff inspected
  after restoration. No live mutants remain. Temporary logs `/tmp/farmcore-mutant-*.txt`.
- `uv run ruff check src tests`: **All checks passed!**
- `uv run ruff format --check src tests`: **444 files already formatted**.
- `uv run alembic heads`: **inventory_0001 (head)**; no schema change.
- `ui-craft`: sign-in copy only; existing template semantics/tokens/layout retained. Inspected
  `core/static/farmcore.css`; no new styling or interaction states.

Broad regression, gauntlet and fresh independent acceptance/diff review are pending with the
access coordinator. Do not treat targeted evidence as full completion or skipped tools as passes.

Fresh QA requested a true generated property in addition to the fixed selector sweep.
Added Hypothesis as a dev-only dependency (`hypothesis>=6.170.1`, locked6.170.1 with
`sortedcontainers`2.4.0); runtime dependency graph unchanged. Used `uv add --dev hypothesis
--no-sync` to preserve coordinator-installed verification tools. Dependency audit remains the
coordinator's pending check; these are test tooling dependencies, not an assertion of zero risk.

`uv run pytest tests/core/test_private_access.py -q --hypothesis-show-statistics`:
**25 passed in3.59s**. New public HTTP property: **30 passing,0 failing,0 invalid** generated
examples,6 denied requests each (**180 requests**) across private/demo pages, GeoJSON and
assistant reads. Generates bounded selector strings, forged tokens and tour variants; demo
foreign IDs are constructed by positive offsets or nonnumeric prefixes, never filtered.
Each request must return exact generic403 and each example preserves the access-log count.
Function-scoped Hypothesis fixture warning suppressed deliberately: seed setup is idempotent,
the connection rolls back at test teardown, and every example asserts no audited side effect.
No production behavior changed for this coverage extension; original RED/GREEN evidence stands.
Alphabet excludes transport-invalid control characters; bounded30 examples are not exhaustive.
Fresh `uv run ruff check src tests` passes, format check reports **444 files already formatted**,
and `git diff --check` passes. Worker sources frozen after this evidence update.
