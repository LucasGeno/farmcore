---
type: Work Ticket
title: "Advice becomes a persistent planned spray job"
description: "Milestones 1–4: advice becomes a persistent planned spray job."
status: in-progress
blocked_by: ["04", "05"]
---

# 07 — Advice becomes a persistent planned spray job

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Advice becomes a persistent planned spray job. Touchpoints: new planning app/models/routes/templates; forecast and advisor entry points; migration. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain. Implementation workers propose schema changes; only the designated coordinator authors/serializes revision files. During access tickets 01–03, the access chat coordinator owns revisions exclusively; other chats request schema changes from the integration coordinator.

## Blocked by

- [04](./04-live-weather.md)
- [05](./05-reviewed-parameters.md)

## Acceptance criteria

- [x] Create/edit/cancel planned job with parcels, assessment/version, product, duration and farmer availability.
- [x] State survives restart/device changes; stale revision returns conflict.
- [x] Planned jobs do not enter completed register or consume stock.
- [x] Read/write ownership uses central identity/scopes and explicit job lifecycle.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [x] `uv run pytest tests/apps/planning tests/apps/forecast tests/apps/operations -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [x] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [x] `uv run alembic heads` — one head after integration.
- [x] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Claim 2026-11-06 19:50 CET: implementer `fc07-deepseek` (OpenCode Go `opencode-go/deepseek-v4.1-flash`, M1), worktree `~/dev/farmcore-worktrees/07-persistent-jobs`, branch `feat/07-persistent-jobs`. Coordinator and migration author: `fleet-head-opus` (M1). Reviewer: Dell Codex. Stuck deadline 2026-11-07 09:00 CET.

Implementation follows coordinator schema handoff `4d755e1` (`forecast_0002_availability` → `planning_0001_jobs`, head `planning_0001`). This stream authored no Alembic revision file. The planning manifest now declares `depends=(fields, operations, advisor, forecast)`, routes, `planning:read`/`planning:write`, a demo-reset hook and the Planned jobs nav entry.

### TDD observations

- Planning RED before routes/services: `timeout 600 uv run pytest tests/apps/planning -m "not data" -q` → `31 failed in 6.36s` (missing `/planning/jobs` endpoints and scopes).
- Planning GREEN after implementation: `timeout 600 uv run pytest tests/apps/planning -m "not data" -q` → `34 passed in 10.18s`.
- Forecast availability rewrite RED: the old cookie suite failed 34 tests (`tests/apps/forecast`, assistant and notifications) before the tests were moved to `POST /forecast/busy` and `forecast_availability`.
- Forecast/assistant/notifications/shell re-check: `timeout 600 uv run pytest tests/apps/planning tests/apps/forecast tests/apps/assistant tests/apps/notifications tests/core/test_shell.py tests/core/test_farm_switcher.py -m "not data" -q` → `307 passed, 3 skipped, 2 deselected in 132.38s`.

### Verification commands and results

1. `~/.local/bin/fleet-build --min-free-gib 30.15 --wait 21600 --timeout 3600 --workdir $PWD -- timeout 600 uv run pytest tests/apps/planning tests/apps/forecast tests/apps/operations -m "not data"` → `494 passed, 14 deselected in 71.98s`. The build queued once (`Waiting for another heavy build to finish`) and passed.
2. `uv run ruff check src tests` → `All checks passed!`; `uv run ruff format --check src tests` → `520 files already formatted`.
3. `uv run alembic heads` → `planning_0001 (head)`.
4. `git diff --check` → clean.

### Tier-3 failure model (owner-only mutations, public job contract)

- **Stale edit/cancel revision**: `expected_revision != job.revision` returns 409 before any row, revision snapshot or access-log write. Covered by `test_a_stale_revision_returns_conflict_and_writes_nothing` and concurrency test.
- **Concurrent edits on different devices**: the request farm advisory lock and farm row lock serialize writes; one edit commits revision 2, the other observes revision 2 and returns 409. Covered by `tests/apps/planning/test_concurrency.py` (one 200, one 409, exactly one `edited` snapshot).
- **Forged origin / missing CSRF**: owner cookie mutations without `Origin`+`X-CSRF-Token` return 403; cross-origin returns 403 before any job row. Covered by `test_owner_writes_with_csrf_while_advisor_reads_only`.
- **Role and revocation**: assigned advisor can read a job but cannot create/edit/cancel (403); owner can; revoked membership takes effect on the next request (403 with no job title in the refusal body). Covered by the same test.
- **Foreign farm and cross-farm references**: a foreign member cannot select the farm (403); a job id from another farm is 404 with no body metadata; a foreign assessment, parcel or parameter version is 422 and writes nothing. Covered by `test_foreign_members_cannot_read_or_write_the_job`, `test_cross_farm_assessment_and_parcel_ids_are_refused`, and `test_a_job_freezes_the_assessment_parameter_version`.
- **Central grant scopes**: a grant with only `fields:read` cannot read `planning_job` (`ScopeDenied`, no access log); `planning:read` is required. Covered by `test_a_grant_without_planning_read_cannot_read_planned_jobs`.
- **Anonymous demo and tour writes**: a demo visitor with no principal gets 403 on `POST /forecast/busy`; tour mode refuses the mutation with 403 and writes no availability. Covered by `tests/apps/planning/test_availability.py` and `test_plan_tour_reads_and_busy_remain_farm_local_and_app_off_hides_the_page`.
- **Invalid availability and inputs**: past/out-of-range/invalid days return 422 with no row; invalid names, durations, parcel sets, unknown/foreign ids and non-evaluable advice return 422 with no job; planned work writes no canonical log or stock row. Covered by `tests/apps/planning/test_validation.py`, `tests/apps/planning/test_availability.py`, and `test_planned_work_never_becomes_a_log_or_moves_stock`.
- **Demo reset ordering**: the planning demo-reset hook deletes revisions, parcel links and jobs before core deletes advice, so the assessment foreign key cannot block a reset. Covered by `test_demo_reset_removes_planned_jobs_before_deleting_their_assessments`.

Manual mutant fallback executed: in `service.edit`, the revision guard was changed from `if expected_revision != job.revision:` to `if False:`; `timeout 600 uv run pytest tests/apps/planning/test_jobs.py::test_a_stale_revision_returns_conflict_and_writes_nothing tests/apps/planning/test_concurrency.py -q` → `2 failed in 1.66s` (stale edit and concurrent edit both reached revision 2). Restored; the same two tests → `2 passed in 1.40s`. The tests exercise the guard.

### Acceptance evidence

- Create/edit/cancel with parcels, frozen assessment/version, product, duration and recorded farmer availability: `test_create_edit_and_cancel_keep_a_revision_history`, `test_a_job_records_the_farmer_availability_at_each_revision`, `test_the_planning_form_creates_and_cancels_a_job`, `test_a_job_freezes_the_assessment_parameter_version`.
- Restart/device persistence and stale conflict: `tests/apps/planning/test_persistence.py`, `test_a_stale_revision_returns_conflict_and_writes_nothing`, `test_two_edits_with_the_same_revision_have_one_winner`.
- No completed register or stock movement: `test_planned_work_never_becomes_a_log_or_moves_stock` (no `Log`, stock `on_hand` unchanged, register/diary absent).
- Ownership and lifecycle: `tests/apps/planning/test_authorization.py`, `test_a_grant_without_planning_read_cannot_read_planned_jobs`, cancelled jobs read-only with explicit `cancelled_by`/`cancelled_at` and no second snapshot.

Fresh reviewer (Dell Codex) has not yet reviewed this implementation; the final verification checkbox remains open.

