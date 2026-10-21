---
type: Work Ticket
title: "Confirmed spray records can be corrected safely"
description: "Milestones 1–4: confirmed spray records can be corrected safely."
status: in_progress
blocked_by: ["02"]
---

# 09 — Confirmed spray records can be corrected safely

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Confirmed spray records can be corrected safely. Touchpoints: operations correction service/routes; chat confirmation; stock transactions and source receipts. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain. Implementation workers propose schema changes; only the designated coordinator authors/serializes revision files. During access tickets 01–03, the access chat coordinator owns revisions exclusively; other chats request schema changes from the integration coordinator.

## Blocked by

- [02](./02-member-sessions.md)

## Acceptance criteria

- [x] Preview/cancel/confirm correction scoped to farm/member; retain original, correction reason and actor/time.
- [x] Retries and concurrent confirmation apply one correction and stock delta; failure rolls back all writes.
- [x] Existing weather receipts and source provenance remain immutable; correction does not overwrite original evidence.
- [x] Original pending/done spray confirmation paths remain supported with explicit authority.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [x] `uv run pytest tests/apps/operations tests/apps/chat tests/apps/inventory -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [x] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [x] `uv run alembic heads` — one head after integration.
- [x] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Branch `feat/audited-corrections`, rebased onto `main` after the revamp merge (it includes PR #21:
assistant routes end the request transaction before the chat handlers open their own sessions).
Status stays `in_progress` until review passes and the branch merges.

Acceptance criteria:
- Preview/cancel/confirm, scoped to farm and member, keeping the original, reason, actor and time:
  `test_preview_cancel_confirm_moves_computed_stock_once_and_keeps_the_receipt[operator|member]`,
  `test_owner_reaches_correction_from_records_and_confirms_in_the_browser[/operations/|/operations/register]`
  (an owner follows the spray card's link, edits the HTML form, previews and confirms; QA found no
  link on the register page, so register cards now link to the correction screen),
  `test_correction_audit_is_private_until_final_and_scoped_to_the_farm`,
  `test_foreign_hostile_or_unknown_targets_are_uniformly_not_found`.
- One correction and one stock delta under retries and concurrency; failure rolls everything back:
  the lifecycle test above (20 kg bought, 2 kg/ha on 2 ha leaves 16; corrected to 1 kg/ha leaves
  18, unchanged by cancel or a repeated confirm),
  `test_retried_request_key_returns_the_same_correction_and_never_rewrites_it`,
  `test_independent_previews_conflict_after_one_commits`,
  `test_concurrent_terminal_actions_have_one_durable_outcome`,
  `test_real_constraint_failure_rolls_back_canonical_audit_and_postcommit`,
  `test_another_writer_changing_the_record_invalidates_a_pending_preview`.
- Weather receipts and source provenance stay immutable:
  `test_actual_machine_import_invalidates_preview_and_retains_weather_evidence`,
  `test_an_edit_changes_only_the_edited_fact` (untouched doses keep their row, exact value and
  label; an untouched treatment time keeps its seconds and DST fold),
  `test_generic_log_events_do_not_copy_private_audit_receipts`.
- Original pending/done capture paths stay supported with explicit authority:
  `test_owners_cancel_and_confirm_only_their_own_drafts_and_a_double_confirm_logs_once`,
  `test_selected_private_farm_and_clarification_are_not_shared`,
  `test_member_capture_and_confirmation_fail_closed`,
  `test_authority_lost_during_external_extraction_writes_nothing`,
  `test_original_shared_web_draft_is_claimable_only_by_the_default_farm_operator`,
  `test_capture_controls_appear_only_for_an_owner_on_the_selected_farm`, the existing Telegram
  and operator capture tests, and `test_capture_csrf.mjs`.

Privacy: the audit snapshot keeps the full original record, including source payloads, but
correction pages and JSON show only the corrected facts and each source's type and time. Advisors
and co-owners reading a final receipt do not see transcripts, chat ids or receipt keys. The
immutability of a confirmed or cancelled correction is enforced by the application only; the
runtime database role can still update `operation_correction` rows.

Tier 3 failure model, one test per failure mode:
- A stale preview after another writer (machine import, a replaced quantity row, a changed last
  digit) returns 409 and writes nothing.
- A retry, a double click, or a concurrent confirm and cancel leaves one terminal state, one
  `log.corrected` event and one stock delta; a retry never rewrites the stored receipt.
- A database failure during confirm rolls back the log, the audit row, the event and the advice
  run, and the same intent confirms on retry.
- If the owner's membership, identity or session is revoked while the request waits for the farm
  lock, the request gets 403 (`test_authority_revoked_while_request_waits_for_farm_lock_cannot_confirm`).
- Another farm's spray or correction, ids past bigint and unknown ids all return 404.
- Anonymous visitors, advisors, owners of another farm and tour visitors cannot preview or
  finalize; a co-owner cannot finalize someone else's pending correction
  (`test_only_the_author_can_finalize_and_only_owners_can_preview`).
- Invalid intent the app itself rules on (unchanged dose, unknown or delisted product, unknown
  parcel, end before start, a dose past numeric storage, a cleared required value) is refused
  with nothing written (`test_invalid_correction_intent_is_rejected_without_writes`).
- No request holds its farm locks while code that opens another session runs. The advice test and
  `tests/apps/assistant/test_capture_locks.py` run on a pooled engine with `lock_timeout=1000`, so
  a regression fails in about 1 s instead of hanging.
- A member clearing the cookie does not reset the assistant quota
  (`test_signed_in_quota_follows_the_identity_not_the_browser_cookie`).

Mutation sample (manual mutants, the focused file after each):
- In `corrections.py`, these were killed: dropping the finalize actor check, the base fingerprint
  check, the revoked-session predicate, terminal idempotency, and the farm filter on the log
  lookup (after adding another farm's real ids to the not-found test).
- Dropping the farm filter on the correction lookup survives. Accepted: the farm-scoped log lookup
  that follows still returns 404.
- Running the advisor job inside the locked confirm request fails the advice test within 1 s.
- Dropping the request commit before the chat handler fails `test_capture_locks` within 1 s.

Checks after the last code edit:
- `uv run pytest tests/apps/operations tests/apps/chat tests/apps/inventory tests/apps/assistant tests/core/test_member_sessions.py -m "not data"`:
  789 passed, 3 skipped, 66 deselected, 3:35.
- `scripts/check.sh -rs` (ruff lint and format pass): 2742 passed, 3 skipped (SIGPAC / InfoRiego
  files not bundled), 281 deselected, 11:20.
- `uv run alembic heads`: `review_0001 (head)`. No migration was authored; the ticket uses the
  existing `operations_0002`.

Tests: 24 test functions (16 in `tests/apps/operations/test_corrections_http.py`, 8 in
`tests/apps/assistant/test_member_capture_binding.py`) plus one node file, down from 53 in the
implementation draft. Over the usual per-ticket budget because the ticket adds two authority
surfaces (corrections and member capture) with a tier 3 concurrency contract; each remaining
test owns one named failure mode above.

Not tested (known limits): no live-browser smoke run; reachability is proven by following the
rendered link through TestClient.
