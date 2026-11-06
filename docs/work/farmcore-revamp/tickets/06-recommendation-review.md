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

- [ ] Owner requests review of an exact stored assessment; assigned advisor responds with note and reviewer/time.
- [ ] Review does not mutate model output or automatically endorse newer assessments.
- [ ] Golden questions test facts, missing inputs and unauthorized farm requests; deterministic fallback retained.
- [ ] No automatic training/few-shot ingestion of private feedback; no model-authorized write.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [ ] `uv run pytest tests/apps/advisor tests/apps/assistant tests/apps/notifications -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [ ] `uv run alembic heads` — one head after integration.
- [ ] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
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
  → `555 passed, 3 skipped, 120 deselected in 133.41s`.
- `timeout 600 uv run pytest tests/apps/review tests/core -m "not data" -q`
  → `794 passed, 2 deselected in 261.70s`.
- `uv run ruff check src tests` → pass; `uv run ruff format --check src tests` → `509 files
  already formatted`.
- `uv run alembic heads` → `review_0001 (head)` (one head; no revision authored here).

Tier 3 failure model for the auth/concurrency/public-contract slice: a stale or revoked
membership must fail closed on the next request; two answers must never create two opinions; a
foreign numeric id must resolve to nothing, not to another farm's row; a logs-only consent must
not read advice-review notes; a model-chosen tool must not write. Covered by
`test_recommendation_review.py` (roles, CSRF, foreign/future ids, revocation, concurrent answers),
`test_scope_policy.py`, and `test_recommendation_golden.py`; mutation fallback recorded below once
the full gate returns.

Full non-data/non-slow regression is running through `fleet-build` (see final evidence).

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
