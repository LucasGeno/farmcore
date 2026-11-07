---
type: Work Ticket
title: "Complete planned work into one confirmed operation"
description: "Milestones 1–4: complete planned work into one confirmed operation."
status: in-progress
blocked_by: ["06", "08", "10"]
---

# 11 — Complete planned work into one confirmed operation

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Complete planned work into one confirmed operation. Touchpoints: planning completion; operations confirmation; inventory; advisor recalculation; evidence links. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain. Implementation workers propose schema changes; only the designated coordinator authors/serializes revision files. During access tickets 01–03, the access chat coordinator owns revisions exclusively; other chats request schema changes from the integration coordinator.

## Blocked by

- [06](./06-recommendation-review.md)
- [08](./08-resource-scheduling.md)
- [10](./10-broader-capture.md)

## Acceptance criteria

- [x] Preview actual work and differences from saved plan; confirmed completion atomically links/create canonical operation.
- [x] Double click/retry/concurrent completion cannot duplicate operation or stock movement.
- [x] Keep original assessment/version, human review, plan revision and immutable available weather separately.
- [x] Re-evaluate only after successful commit; failed transactions leave plan/log/stock unchanged.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [x] `uv run pytest tests/apps/planning tests/apps/operations tests/apps/inventory tests/apps/advisor -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [x] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [x] `uv run alembic heads` — one head after integration.
- [x] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Claim 2026-11-07 17:15 CET: implementer `fc11-deepseek` (OpenCode Go `opencode-go/deepseek-v4.1-flash`, M1), worktree `~/dev/farmcore-worktrees/11-job-completion`, branch `feat/11-job-completion`. Coordinator and migration author: `fleet-head-opus` (M1). Reviewer: Dell Codex. Stuck deadline 2026-11-08 09:00 CET.

Implemented 2026-11-07 (implementer `fc11-deepseek`). No Alembic revision was authored: the
implementation follows the coordinator's `planning_0003` (`1911a99`, head `planning_0003`).

### What changed

- `apps/planning/service.py`: read-only `completion_preview` and transactional `complete`.
  Preview validates actual start/duration/parcels/doses and reports time, duration, parcel and
  product differences against the saved plan; it writes nothing. `complete` takes the existing
  per-farm advisory lock, locks the job row, and then, in one transaction, creates or merges the
  canonical done spray through `core.logs.record_operation` (`source_type="planning"`), sets the
  completion metadata and operation link, deletes the now-free assignments, appends the immutable
  `completed` revision snapshot (plan + completion + actual-day availability) and flushes. A
  canonical weather receipt is captured on the completion's own source before a merge can move
  evidence. Retries with the same request key return the existing operation; a different body
  under that key, or another key on a completed job, is a 409.
- `apps/planning/routes.py`: JSON `POST /planning/jobs/{id}/completion/preview` and
  `POST /planning/jobs/{id}/complete`, plus form preview/confirm endpoints used by the page.
  The advisor `advisor.evaluate` job is added through `BackgroundTasks` only after a successful
  service return, so the request transaction commits before re-evaluation; a failed write raises
  before that point and rolls plan/log/stock back together.
- `core/logs.py`: `RANK_PLANNING = 25`, so farmer-confirmed planned work replaces file/voice
  doses but never a machine file.
- `apps/planning/templates/planning/index.html`: completion form and completed-work summary;
  new `completion_preview.html` shows differences and confirms the same values.
- `apps/planning/manifest.py` declares `logs:write`; `operations.BADGES` labels `planning`
  provenance. `apps/planning/demo.py` already deletes jobs before core deletes logs.

### TDD observations

- RED before implementation:
  `timeout 600 uv run pytest -q tests/apps/planning/test_completion.py tests/apps/planning/test_completion_concurrency.py tests/apps/planning/test_authorization.py -m "not data"`
  → **9 failed, 6 passed** (all completion endpoints 404; existing authorization tests passed).
- GREEN after the minimum implementation, same files → **all passed**; final focused runs below.
- Additional RED during review-level checks: the failed-transaction case initially returned a 500
  because an autoflush raised the per-farm request-key `UniqueViolation` before the guarded flush;
  the job mutations and revision insert are now inside `session.no_autoflush` with one explicit
  flush, so the same test observed **409 with plan/log/stock unchanged**.
- Requirement coverage: `test_completion_preview_shows_actual_differences_and_writes_nothing`,
  `test_completion_creates_one_confirmed_operation_and_retries_are_idempotent`,
  `test_completion_merges_into_a_matching_confirmed_spray_once`,
  `test_a_failed_completion_leaves_plan_log_and_stock_unchanged`,
  `test_the_completion_form_previews_differences_then_confirms`,
  `test_demo_reset_removes_a_completed_job_and_its_operation`,
  `test_only_the_owner_completes_a_planned_job`,
  `test_cross_farm_assessment_and_parcel_ids_are_refused` (completion branch),
  `test_completion_concurrency.py` (same-key and different-key two-session races).

### Verification commands and exact results

```sh
timeout 600 uv run pytest -q tests/apps/planning tests/apps/operations tests/apps/inventory tests/apps/advisor -m "not data"
# 908 passed, 132 deselected in 107.72s (0:01:47)

~/.local/bin/fleet-build --min-free-gib 30.15 --wait 21600 --timeout 3600 --workdir $PWD -- \
  bash -lc "timeout 900 uv run pytest -q tests/core -m 'not data and not slow' --ignore=tests/instances && timeout 1200 uv run pytest -q tests/apps -m 'not data and not slow'"
# tests/core: 760 passed, 2 deselected in 242.04s (0:04:02)
# tests/apps: 2123 passed, 3 skipped, 262 deselected in 486.90s (0:08:06)

node --test tests/core/*.mjs tests/apps/fields/*.mjs
# tests 32, pass 32, fail 0

uv run ruff check src tests          # All checks passed!
uv run ruff format --check src tests # 542 files already formatted
uv run alembic heads                 # planning_0003 (head)
```

### Tier-3 failure model and adversarial cases

- **Authorization**: completion/preview routes are owner-only; the advisor and a foreign owner
  receive 403, an anonymous visitor 403, and a foreign farm selecting the job id 404, all before
  any write. Covered by `test_only_the_owner_completes_a_planned_job` and the completion branch
  of `test_cross_farm_assessment_and_parcel_ids_are_refused`. Planning reads stay member-only.
- **Concurrency**: every write first takes the existing per-farm advisory lock (`get_repo`) and
  `service.complete` re-takes it, then locks the job row `FOR UPDATE`. Two concurrent requests
  with one key therefore serialise: the second sees `completed` and returns the same operation
  (two 200s, one log, one quantity, one completion revision, one stock movement). Two concurrent
  requests with different keys produce one 200 and one 409. Covered by the two real
  `committed_session_factory` + two-`TestClient` tests.
- **Idempotency and retry**: the completion request key is unique per farm in the database
  (`ix_planning_job_farm_request_key`); the stored fingerprint rejects the same key with a changed
  body (409). A database-key collision raised after the canonical write is translated to 409 and
  the transaction rolls back, leaving the second plan, log set and stock untouched.
- **Public contracts**: only a confirmed completion creates a done spray; planned work still
  never enters the register or moves stock. The completed operation appears in `/operations/`
  and the register. Demo reset removes the completed job, its revisions and its operation.

### Mutation evidence (manual fallback, actually applied and restored)

In `service.complete` and `_completion_target` the completed-status, planned-status and revision
guards were replaced with `if False:` (copy backed up to `/tmp`, then restored byte-for-byte):

```sh
timeout 600 uv run pytest -q tests/apps/planning/test_completion_concurrency.py tests/apps/planning/test_completion.py -m "not data"
# 3 failed, 5 passed (both races duplicated/advanced the job; retry advanced revision to 3)
# after restore: 8 passed in 4.50s
```

### Acceptance evidence

1. *Preview and atomic link/create*: preview returns planned vs actual start/duration/parcels/
   doses, differences and the assessment/version, and writes no row; completion creates/merges a
   canonical done spray and links it via `operation_log_id` in the same transaction.
2. *No duplicate operation or stock movement*: retry returns the same log id; same-key and
   different-key concurrency tests assert exactly one `Log`, one `Quantity`, one `completed`
   revision (`job.revision == 2`) and one stock consumption.
3. *Separate evidence*: the job keeps its frozen assessment/parameter version; creation/edit/
   cancellation/completion revisions stay append-only and distinct; the completion revision
   embeds the saved plan, actual work and availability read on the actual day; the completion
   source stores its own immutable weather receipt (`farmcore.spray_weather`) with policy and
   parcel ids; recommendation review rows are untouched.
4. *Re-evaluate only after commit*: the service never runs a job; routes add `advisor.evaluate`
   only after `service.complete` returns, through background tasks after the function-scoped
   repository commits (the same seam as confirmed corrections).
   `test_advisor_re_evaluation_runs_only_after_a_committed_completion` attaches a synthetic
   station and observes the public seam: a successful completion adds one `advice` row per parcel
   at the farm clock's now, while a completion that fails on the per-farm request key adds none
   and leaves the second plan untouched. A failed completion therefore leaves plan, log set and
   stock unchanged and queues no evaluation.

### Review round 1 (independent review of `8d1194b`, fixed 2026-11-07)

- **MED — future work could be confirmed as done.** `service._completion_inputs` now rejects an
  actual start, or an actual end derived from start + duration, later than the farm clock's
  `now`, for both preview and completion; the form answers a 303 redirect with a clean
  `error=` message and writes nothing. Existing tests were moved to valid past actuals.
  RED: `timeout 600 uv run pytest -q tests/apps/planning/test_completion.py -m "not data"`
  → `1 failed, 6 passed` (`test_a_future_completion_is_refused_with_a_clean_form_error` saw
  preview `200 != 422`). GREEN after the check → `8 passed`.
- **MED — re-evaluation proof gap.** Added the station-backed test described in acceptance point
  4. It passed against the existing `BackgroundTasks` seam (no code fix was needed) and covers
  both a committed completion and a failed one. The earlier Evidence caveat about the fixture
  farm having no station is removed.

Not claimed: independent reviewer sign-off (Dell Codex) — the last Verification box stays
unticked. No merge, deploy, force-push or Alembic revision was performed.
