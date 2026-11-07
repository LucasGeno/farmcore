---
type: Work Ticket
title: "Resource-aware scheduling offers feasible alternatives"
description: "Milestones 1–4: resource-aware scheduling offers feasible alternatives."
status: done
blocked_by: ["07"]
---

# 08 — Resource-aware scheduling offers feasible alternatives

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Resource-aware scheduling offers feasible alternatives. Touchpoints: planning engine/resource assignment; forecast ranking; machine/operator forms. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain. Implementation workers propose schema changes; only the designated coordinator authors/serializes revision files. During access tickets 01–03, the access chat coordinator owns revisions exclusively; other chats request schema changes from the integration coordinator.

## Blocked by

- [07](./07-persistent-jobs.md)

## Acceptance criteria

- [x] Eligible hours combine existing product/weather constraints, estimated duration and configured operator/machine availability.
- [x] Overlapping assignments reject atomically; unknown capacity is shown rather than assumed safe.
- [x] Offer eligible alternate time/job/parcel or configured machine; preserve infeasible job with reason.
- [x] No soil suitability, reduced dose, unsupported price or calibrated probability claims; alternatives are side-effect free.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [x] `uv run pytest tests/apps/planning tests/apps/forecast -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [x] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [x] `uv run alembic heads` — one head after integration.
- [x] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [x] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Claim 2026-11-07 12:35 CET: implementer `fc08-deepseek` (OpenCode Go `opencode-go/deepseek-v4.1-flash`, M1), worktree `~/dev/farmcore-worktrees/08-resource-scheduling`, branch `feat/08-resource-scheduling`. Coordinator and migration author: `fleet-head-opus` (M1). Reviewer: Dell Codex. Stuck deadline 2026-11-08 06:00 CET.

Implementation follows the coordinator schema handoff in the claim run: this stream authored no Alembic revision. `planning_0002` (commit `d41e769`, head `fleet-head-opus`) adds `planning_machine`, `planning_operator`, `planning_resource_window` and `planning_job_assignment`, with two partial GiST exclusion constraints (btree_gist) over half-open `tstzrange` bookings and partial unique indexes allowing one machine and one operator per job.

Code: `planning/engine.py` (pure eligibility), `planning/resources.py` (configured resource CRUD), `planning/offers.py` (read-only assessment and alternatives), `planning/resource_routes.py` (owner-only forms), assignment endpoints/forms in `planning/routes.py`, `planning/service.py` (bookings, revisions, cancellation), `RESOURCE_SCOPES`, demo reset and the planning page.

### TDD observations

- Pure engine RED: `timeout 600 uv run pytest -q tests/apps/planning/test_resource_engine.py -m "not data"` → collection `ImportError: cannot import name 'engine'`; minimum GREEN → `5 passed`. Later product-sulphur and busy-day tests added through the same seam.
- Configured resources RED: `test_resources.py` → `ImportError: cannot import name 'resources'`; GREEN after forms/service → `6 passed`, then `7 passed` with the deactivation route.
- Assignments RED: `test_assignments.py` → `7 failed` (404 on `/planning/jobs/{id}/assignments`); GREEN after service/routes → `7 passed`; form-conflict and demo-reset tests added.
- Offers RED: `test_resource_offers.py` → `5 failed` (`KeyError: 'resource_assessment'`); GREEN after `offers.py` integration → `7 passed`, then busy-day job offers → `8 passed`.
- Review finding 1 RED: the concurrent-edit test let SQLSTATE `23P01` escape as `IntegrityError` (`test_...concurrent_booking_that_beats_the_precheck...` failed); GREEN after translating the flush failure to `JobConflict` → `1 passed`.
- Review finding 2 RED: wait-advice and non-overlapping-window parcels were listed without eligibility (`4 failed` with the machine checks); GREEN after window/status eligibility → `70 passed` in `tests/apps/planning`.
- Review finding 3 RED: an alternate machine was `available` with closed weather and with a busy booked operator (`4 failed`); GREEN after requiring weather + operator + capacity + windows → `70 passed`.
- Re-review finding 1 RED: a parcel whose advice window started 30 minutes into the 90-minute job was still `eligible`; GREEN after requiring the advice window to cover the full job window → `13 passed` in `test_resource_offers.py`.
- Re-review finding 2 RED: a spare machine was `available` on a farmer-declared Busy day; GREEN after passing the farm-day state into `_machine_offers` and excluding Busy days with an explicit reason → `13 passed`.

### Verification commands and results

1. `timeout 600 uv run pytest -q tests/apps/planning tests/apps/forecast -m "not data"` → `217 passed in 49.02s`.
2. `uv run ruff check src tests` → `All checks passed!`; `uv run ruff format --check src tests` → `539 files already formatted`.
3. `uv run alembic heads` → `planning_0002 (head)`.
4. Full split, foreground, disk-guarded, instances excluded:
   `~/.local/bin/fleet-build --min-free-gib 30.15 --wait 21600 --timeout 3600 --workdir $PWD -- bash -lc "timeout 900 uv run pytest -q tests/core -m 'not data and not slow' --ignore=tests/instances && timeout 1200 uv run pytest -q tests/apps -m 'not data and not slow' --ignore=tests/instances"`
   → both commands passed; apps summary `2112 passed, 3 skipped, 262 deselected in 496.15s`. Core summary from the same tree: `752 passed, 2 deselected`.
5. `git diff --check` → clean.

### Tier-3 failure model (owner writes, bookings, public contracts)

- **Stale edit/cancel/booking revision**: `expected_revision != job.revision` returns 409 before any job, assignment, revision snapshot or access-log write. Assignment changes bump the revision once and append `assignments` to the snapshot. Covered by `test_a_stale_revision_returns_conflict_and_writes_nothing`, `test_the_assignment_form_reports_a_conflict_instead_of_failing` and the existing revision tests.
- **Concurrent bookings**: two committed HTTP sessions booking the same machine for overlapping windows produce exactly one 200 and one 409 and exactly one assignment row. The service pre-check gives the readable message; the database exclusion is the backstop. Covered by `test_concurrent_assignments_to_one_machine_have_one_winner`.
- **Concurrent edit that bypasses the pre-check**: one session holds a new booking uncommitted; the second edit's pre-check cannot see it, then its assignment flush reaches `ex_planning_job_assignment_machine` (`23P01`). The service rolls back and raises `JobConflict` (“A booked resource already has another job at the new time.”); the job time and booking stay unchanged. Covered by `test_a_concurrent_booking_that_beats_the_precheck_is_a_clean_job_conflict`.
- **Adversarial ownership**: owners (or installation operator) configure machines/operators/windows and book; an assigned advisor reads the same resources and page but receives 403 on machine forms, JSON assignment and assignment forms. Anonymous demo and tour cannot read `/planning/` or `/planning/jobs.json`. Covered by `test_advisors_read_resources_but_cannot_configure_or_book_them`, `test_anonymous_demo_visitor_cannot_read_resources`, and the existing authorization/demo tests.
- **Cross-farm/unknown ids**: guessed foreign machine/operator ids are rejected by the farm-scoped composite foreign keys and service checks; inactive or missing resources return 422 and write nothing; invalid capacity/kind/times redirect with a plain error. Covered by `test_resource_forms_reject_invalid_values_and_foreign_ids`, `test_assignments_reject_foreign_unconfigured_and_inactive_resources`, and `test_owner_deactivates_a_machine_and_it_cannot_be_booked`.
- **Scope separation**: a grant with only `fields:read` cannot read `planning_machine`; `planning:read` is required. Covered by `test_a_grant_without_planning_read_cannot_read_resources`.
- **Demo reset ordering**: assignments and windows are deleted before jobs and resources, so demo reset clears the new farm state without foreign-key blocks. Covered by `test_demo_reset_clears_assignments_and_resources` and `test_demo_reset_clears_configured_resources`.
- **Unknown inputs**: NULL work rate, absent availability windows and missing forecast hours stay `unknown`; a `wait` decision, a non-overlapping advice window or one that covers only part of the job is shown as not eligible, never as an offer. A farmer-declared Busy day removes alternate machines. Covered by the engine/offers tests above.
- **Manual mutation fallback**: changing the JSON assignment route dependency from `require_farm_owner` to `require_farm_member` made the advisor test fail (`1 failed`), restored → `1 passed`. Changing `capacity_state(None, ...)` to return `fits` made the unknown-capacity engine and offer tests fail (`2 failed`), restored → `2 passed`. The concurrent-edit test is itself a real-database mutant of the pre-check: removing the DB backstop or the translation fails it.

### Acceptance evidence

- **Eligible hours combine constraints**: `test_resource_engine.py` (weather lookahead, duration, resource window coverage, capacity, product-sulphur temperature), `test_resource_offers.py::test_a_job_combines_weather_resources_and_known_capacity`, `..._busy_farm_day...`.
- **Atomic overlap + unknown capacity**: `test_overlapping_assignments_are_rejected_and_write_nothing`, `test_concurrent_assignments_to_one_machine_have_one_winner`, the head's `tests/core/test_revamp_migrations.py` exclusion checks, and `test_unknown_capacity_is_shown_and_never_called_feasible`.
- **Alternatives and preserved reasons**: `test_an_infeasible_job_stays_visible_with_reason_and_offers_a_time`, `...offers_another_planned_job...`, `...offers_another_parcel_with_current_advice` and `...advice_window_must_cover_the_full_job_duration` (full-window eligibility only), `...busy_machine_offers_another_configured_machine...` and `...alternate_machine_also_needs_open_weather` / `...needs_the_booked_operator` / `...not_offered_on_a_busy_farm_day` (weather, operator, capacity and farm-day constraints), plus the page rendering the status and reasons.
- **No unsupported claims / side-effect-free**: `test_reading_offers_writes_nothing_and_makes_no_unsupported_claims` asserts unchanged job/assignment/revision counts across repeated reads and no probability, soil, dose, cost, price, saving or currency text in the JSON.

Independent reviewer: pending (Dell Codex). No merge or deploy performed by this stream.

Closeout 2026-11-07 (fleet-head-opus): independent adversarial review by Dell Codex passed at frozen SHA `b568996` after two fix rounds recorded above; CI green; merged as PR #56 (`1c1d70c4`). Schema `planning_0002` authored by the coordinator. One Alembic head on main.
