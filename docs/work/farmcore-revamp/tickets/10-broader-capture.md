---
type: Work Ticket
title: "Confirm field observations and irrigation facts"
description: "Milestones 1–4: confirm field observations and irrigation facts."
status: in-progress
blocked_by: ["09"]
---

# 10 — Confirm field observations and irrigation facts

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Confirm field observations and irrigation facts. Touchpoints: chat extractor DTO/handlers; operations models/routes; assistant entry points. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain. Implementation workers propose schema changes; only the designated coordinator authors/serializes revision files. During access tickets 01–03, the access chat coordinator owns revisions exclusively; other chats request schema changes from the integration coordinator.

## Blocked by

- [09](./09-audited-corrections.md)

## Acceptance criteria

- [x] Resolve missing/ambiguous parcel/date/unit before preview; confirm/cancel supported for observation/irrigation facts.
- [x] Idempotent farm/member-bound write; no unconfirmed record exposed as done.
- [x] Preserve original irrigation amount/unit and gross/net basis when provided; no silent efficiency assumption.
- [x] No irrigation recommendations or spray stock consumption for these record types.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [x] `uv run pytest tests/apps/chat tests/apps/operations tests/apps/assistant -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [x] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [x] `uv run alembic heads` — one head after integration.
- [x] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Claim 2026-11-06 18:55 CET: implementer `fc10-deepseek` (OpenCode Go `opencode-go/deepseek-v4.1-flash`, M1), worktree `~/dev/farmcore-worktrees/10-broader-capture`, branch `feat/10-broader-capture`. Coordinator and migration author: `fleet-head-opus` (M1). Reviewer: Dell Codex. Stuck deadline 2026-11-07 07:00 CET.

Implemented 2026-11-06 (implementer `fc10-deepseek`). No schema change was needed, so no Alembic revision was authored: an irrigation/observation is a canonical `log` row (`type` `irrigate`/`observation`, status pending → done) with no `quantity` rows; its validated app facts live in `log.props` (the canonical model's app-data slot), guarded by the new `apps/operations/facts.py`.

- Chat seam: `DraftOperation` gains `amount_value`/`amount_unit`/`basis`/`observation`; `finalize` keeps only the fields of the draft's own type. The regex fallback reads irrigation verbs/amount units (mm, m3, L) and gross/net (gross/brut, net/nette), and observation verbs; the Vertex response schema and parser carry the same fields. `service.check` resolves parcels, date and (for irrigation) an explicit unit before any preview, and refuses an unstated unit or a basis other than gross/net with a clarifying reply. `handlers` store the validated props, preview without label checks, confirm without advisory or stock work, and answer `This record is already logged.` on a replay.
- Operations seam: recently confirmed facts show `Irrigation 20 mm gross`, `Irrigation 12 m3 (basis not stated)` or `Observation: …` on `/operations/` and the parcel timeline from validated props only; pending drafts stay out of recent records, the register and fact rows; invalid props render `(details unavailable)`, never a guess.
- Assistant seam: the panel captures irrigation/observation turns (including `P5 20 mm` with no verb), asks for a missing unit before a preview, and `GET /assistant/ask` never pretends to record a fact.

TDD evidence (observed RED before minimum GREEN, through public seams):

- `uv run pytest tests/apps/chat/test_broader_capture.py -m "not data" -q` → `15 failed in 9.27s` before implementation (extractor type/amount/basis, preview/confirm, clarify replies, missing parcel/unit, idempotency, cancel, farm binding, no-advisor/no-stock).
- `uv run pytest tests/apps/operations/test_broader_capture_records.py -m "not data" -q` → `4 failed, 2 passed` before the operations view/facts renderer (confirmed fact lines and invalid-props guard); pending/register exclusion already passed.
- `uv run pytest tests/apps/assistant/test_web_facts_capture.py -m "not data" -q` → `2` behaviour failures before the panel fallback and ask wording (no-verb `P5 20 mm` routed to ask; ask text spray-only). The first draft also put a pooled-factory test under the file's autouse `client` fixture; that fixture conflict was fixed by moving the member test into `tests/apps/assistant/test_member_capture_binding.py` and following that module's existing pooled pattern. No product behaviour was weakened.
- Vertex schema/parse test added to `tests/apps/chat/test_extractor_vertex.py`; inventory guard added to `tests/apps/inventory/test_usage.py`.

Verification commands and exact results (heavy suite run through the M1 build lock):

```sh
~/.local/bin/fleet-build --min-free-gib 30.15 --wait 21600 --timeout 3600 --workdir $PWD -- uv run pytest tests/apps/chat tests/apps/operations tests/apps/assistant -m 'not data' -q
# 721 passed, 3 skipped, 66 deselected in 170.06s (0:02:50)

uv run ruff check src tests
# All checks passed!

uv run ruff format --check src tests
# 508 files already formatted

uv run alembic heads
# review_0001 (head)
```

Tier 3 — auth, concurrency and public contracts (written failure model and adversarial cases):

- Auth model: capture and ✅/❌ require the bound farm's owner identity (or the installation operator); anonymous, tour, revoked/expired session, advisor and foreign-farm requests fail before any write. Adversarial: `test_members_capture_irrigation_bound_to_their_farm_and_only_the_owner_confirms` (bob cannot confirm alice's irrigation; the saved log keeps alice's farm and `web:farm:<id>:identity:<alice>:` key); `test_member_capture_and_confirmation_fail_closed` (403, no rows); `test_an_irrigation_belongs_to_the_farm_bound_to_the_capture` (chat-side farm binding); `test_a_button_for_another_farms_log_finds_nothing` pattern extended to the non-spray reply.
- Concurrency model: confirmation takes the farm advisory lock then the log row lock (`core.logs.confirm`); two taps on ✅ cannot both write. Adversarial: `test_concurrent_irrigation_confirms_create_one_record_and_one_event` — two concurrent HTTP confirms → one `done`, one `reply`, one `irrigate` row and exactly one `log.confirmed` event on real pooled PostGIS sessions.
- Idempotency: Telegram redelivery (same `tg:` external key) returns the same draft; a second ✅ returns `This record is already logged.`; `test_a_redelivered_irrigation_and_a_second_confirm_change_nothing`.
- Public contracts: `GET /assistant/ask` refuses to record (`tool is None`); pending records are absent from recent records and the spray register (`test_a_pending_draft_is_not_a_confirmed_record`); invalid props never render as a fact.
- Manual mutant fallback (documented, not an automated mutation run): (a) storing an irrigation as `type="spray"` is killed by the props assertions and by `test_confirmed_irrigation_runs_no_advisor_job_and_uses_no_spray_stock` plus `test_a_confirmed_fact_never_consumes_spray_stock`; (b) dropping the `basis` field is killed by `test_irrigation_preview_and_confirm_keep_the_entered_amount_unit_and_basis` and the panel props assertion; (c) running the advisor for every confirmed type is killed by `SEEN_BY_JOB == []` in the no-advisor test.

Not claimed: independent reviewer sign-off (Dell Codex) — the last Verification box stays unticked. No merge, deploy or force-push was performed.
