---
type: Work Ticket
title: "Farmer requests and receives an advisor check"
description: "Milestones 1–4: farmer requests and receives an advisor check."
status: in-progress
blocked_by: ["05"]
---

# 06 — Farmer requests and receives an advisor check

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Farmer requests and receives an advisor check. Touchpoints: review app routes/templates/manifest; advisor cards; notification and assistant read tools. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain. Implementation workers propose schema changes; only the designated coordinator authors/serializes revision files. During access tickets 01–03, the access chat coordinator owns revisions exclusively; other chats request schema changes from the integration coordinator.

## Blocked by

- [05](./05-reviewed-parameters.md)

## Acceptance criteria

- [x] Owner requests review of an exact stored assessment; assigned advisor responds with note and reviewer/time.
- [x] Review does not mutate model output or automatically endorse newer assessments.
- [x] Golden questions test facts, missing inputs and unauthorized farm requests; deterministic fallback retained.
- [x] No automatic training/few-shot ingestion of private feedback; no model-authorized write.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [x] `uv run pytest tests/apps/advisor tests/apps/assistant tests/apps/notifications -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [x] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [x] `uv run alembic heads` — one head after integration.
- [x] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Claim 2026-11-06 19:40 CET: implementer `fc06-deepseek` (OpenCode Go `opencode-go/deepseek-v4.1-flash`, M1), worktree `~/dev/farmcore-worktrees/06-recommendation-review`, branch `feat/06-recommendation-review`. Coordinator and migration author: `fleet-head-opus` (M1). Reviewer: Dell Codex. Stuck deadline 2026-11-07 09:00 CET.

Pending implementation.

Implementation 2026-11-06/07 (fc06-deepseek). No schema change: a recommendation review is an
append-only event on the exact saved advice row (`res_model="advice"`, kinds
`advice.review_requested` / `advice.reviewed`), not new advice state. No Alembic revision was
authored; the chain stays at `review_0001`.

- Owner asks from the saved advice card fragment (`/review/assessments/{id}/card`, owner-only
  request form); an assigned advisor answers from `/review/recommendations/` or the same fragment
  with an opinion (`agree|disagree|unsure`), required note, and reviewer/time from the farm clock.
  The answer is final; a second answer is `409`, and a new request is allowed only after an answer.
- The held assessment id is checked against this farm and the replay clock (unknown, foreign or
  future rows are `404`). Reads require owner/advisor membership; requests require an owner,
  answers require an advisor role that is still active. Owner-cookie mutations need Origin and
  the server nonce. A logs-only consent cannot read the review note: the read names the
  `recommendation_review` resource, scoped to `advice:read` (test in `test_scope_policy.py`).
- The advice row, its reasons, products and props are never written. A review of an older saved
  check does not touch, or appear on, a newer assessment; the response says which saved check it
  belongs to and whether a newer one exists.
- The bell reports an open check and, for 7 clock days, its answer with reviewer/time. It never
  repeats the note. Private review events are excluded from the shared Activity feed.
- The assistant's read-only `review_assessment_status` tool answers golden questions through the
  deterministic keyword fallback (no model configured): reviewer/time/opinion/note for the exact
  saved check, an explicit limited-assessment line when inputs are missing, and `403` with no
  data for a foreign farm. No question writes a review; the conversation store keeps only the
  question/tool/parcel, never the note. Review declares no write tool.

TDD observations (working evidence, not final verification):

- First journey RED: owner `POST /review/assessments/1/request` returned `404` (route absent);
  minimum GREEN `1 passed in 3.08s`.
- Consent policy RED with the resource mapping removed: `DID NOT RAISE ScopeDenied`; GREEN
  `5 passed in 1.19s`.
- Bell RED: `"Advisor check requested"` absent from the feed; GREEN
  `10 passed, 1 skipped in 5.43s`.
- Golden-question RED: `4 failed` (`KeyError: 'advice_id'`; tool not declared); GREEN
  `5 passed in 8.65s`.
- Activity privacy RED: private review row appeared in the shared feed; GREEN
  `14 passed in 2.09s`.
- Older-check RED: `KeyError: 'current'`; GREEN `9 passed in 14.27s`.

Verification commands and results:

- `timeout 600 uv run pytest tests/apps/advisor tests/apps/assistant tests/apps/notifications -m "not data" -q`
  → `555 passed, 3 skipped, 120 deselected in 139.83s` (final tip re-run).
- `timeout 600 uv run pytest tests/apps/review tests/core -m "not data" -q`
  → `796 passed, 2 deselected in 273.02s` (final tip re-run).
- `uv run ruff check src tests` → pass; `uv run ruff format --check src tests` → `509 files
  already formatted`.
- `uv run alembic heads` → `review_0001 (head)` (one head; no revision authored here).
- `timeout 300 node --test tests/core/*.mjs tests/apps/fields/*.mjs` → `32 pass, 0 fail`.
- Full non-data/non-slow regression through
  `~/.local/bin/fleet-build --min-free-gib 30.15 --wait 21600 --timeout 3600 --workdir $PWD --`
  split into two shards because one pytest process exceeds the required 600 s cap:
  `timeout 600 uv run pytest -q tests/core -m 'not data and not slow'` →
  `746 passed, 2 deselected in 232.87s`; `timeout 600 uv run pytest -q tests/apps -m 'not data
  and not slow'` → `1985 passed, 3 skipped, 262 deselected in 415.97s`. Both `EXIT=0`.
- The first combined run was stopped by the 600 s per-pytest cap at 87 % (not a pass); the
  split run above is the green result. It exposed one stale contract test: the assistant tool
  registry expected six tools; `review_assessment_status` is now the seventh and the expectation
  was updated (`tests/apps/chat/assistant/test_collect_tools.py`).

Tier 3 failure model for the auth/concurrency/public-contract slice: a stale or revoked
membership must fail closed on the next request; two answers must never create two opinions; a
foreign numeric id must resolve to nothing, not to another farm's row; a logs-only consent must
not read advice-review notes; a model-chosen tool must not write. Covered by
`test_recommendation_review.py` (roles, CSRF, foreign/future ids, revocation, concurrent answers),
`test_scope_policy.py`, and `test_recommendation_golden.py`; manual mutants below.

Manual mutants (each planted in source, focused tests rerun, original source restored; `git diff`
clean afterwards):

- Removed `require_farm_owner` from the JSON request route → owner/advisor role test failed
  (`1 failed, 8 deselected`). Killed.
- Removed the advisor-role predicate from `require_assigned_advisor` → same role test failed.
  Killed.
- Removed the "already answered" guard in `recommendations.respond` → duplicate-answer and
  concurrent-answer tests failed (`2 failed, 7 deselected`). Killed.
- Removed the one-open-request guard in `recommendations.request_check` → duplicate-request test
  failed. Killed.
- Removed the `evaluated_at > now` guard in the assessment lookup → future-assessment test failed.
  Killed.

The assistant model boundary was re-read: `llm.VertexToolChooser` receives only the question and
the declared tool schemas, before the tool runs; the tool's facts/text are returned to the
browser, never fed back to the model. The conversation store keeps `(question, tool, parcel)`
only. No review note enters a prompt, and no declared review tool writes.

A no-JS owner can open `/review/assessments/{advice_id}` (tested) and use the same request form;
the card keeps the fragment for the normal path.

Draft PR: https://github.com/LucasGeno/farmcore/pull/52 (base `main`, branch
`feat/06-recommendation-review`). No deployment, no merge, no background jobs. The fresh-reviewer
item stays open for independent QA.

## Independent review disposition of PR #52 at `6cbec1d`

FAIL with two findings; fixed at `ad8f07f` (TDD, RED observed first).

**HIGH — anonymous demo visitor could read a review note through the assistant.** `GET
/assistant/ask` offered `review_assessment_status` to every demo-mode viewer, and the tool
returned the advisor note; an anonymous demo visitor therefore reached private review material.
Fix: `ToolContext` now carries the viewer's active `member_role` and `is_admin`. The assistant
offers the review tool only to an owner/advisor on the selected farm (never anonymous, grant-only,
tour or bot contexts), and the tool itself refuses such a context as defence in depth (the bot
constructs a context with no role). All review routes already called `authorized_review`.
RED: `test_anonymous_demo_visitor_cannot_read_the_review_note` selected
`review_assessment_status` for an anonymous demo visitor. GREEN after the fix:
`tests/apps/review/test_recommendation_golden.py` → `8 passed`. Added coverage for an anonymous
demo visitor (assistant and all five review routes are denied), a member of another farm, tour
mode, owner/advisor access (existing golden questions), and a direct tool call with no member
role.

**MED — review reads were not bounded by the farm clock.** `_events` ignored `occurred_at`, so
rewinding the clock showed later requests/answers, and a later open request made a request at the
earlier clock time return `409`. Fix: `state`, `states` and `request` take the farm clock; `_events`
filters `occurred_at <= now`; request/answer selection and list order use farm time
(`occurred_at`, then id), not insertion order. RED: the rewind test saw a later answer. GREEN:
`test_review_events_are_bounded_by_the_farm_clock` → `1 passed`, covering a hidden later answer, a
hidden future request, a successful request at the earlier clock time, and all four events still
stored. Notifications, the card/page routes and the assistant tool all pass their clock through.

Focused checks after the fixes (final source before the full re-run):

- `timeout 600 uv run pytest tests/apps/advisor tests/apps/assistant tests/apps/notifications -m
  "not data" -q` → `555 passed, 3 skipped, 120 deselected in 132.71s`.
- `timeout 600 uv run pytest tests/apps/review tests/apps/notifications tests/apps/chat/assistant
  tests/apps/assistant -q -m "not data"` → `256 passed, 3 skipped, 53 deselected in 140.09s`.
- `uv run ruff check src tests` → pass; `uv run ruff format --check src tests` → `509 files
  already formatted`.

Manual mutants for the review fixes (planted, focused tests rerun, source restored):

- Removed the `member_role`/admin gate in `review.tools.status` → anonymous demo test failed.
- Removed the assistant's review-tool filter → anonymous demo test failed (tool selected).
- Removed `Event.occurred_at <= now` from `recommendations._events` → clock-bound test failed.

Full non-data/non-slow regression re-run through `fleet-build` (same split as above) is pending
below.
