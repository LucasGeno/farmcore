---
type: Work Ticket
title: "Live weather arrives with provenance and freshness"
description: "Milestones 1–4: live weather arrives with provenance and freshness."
status: approved
blocked_by: []
---

# 04 — Live weather arrives with provenance and freshness

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Live weather arrives with provenance and freshness. Touchpoints: forecast sources/jobs/manifest/store; stations ingestion; data-source views; focused tests. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain. Implementation workers propose schema changes; only the designated coordinator authors/serializes revision files. During access tickets 01–03, the access chat coordinator owns revisions exclusively; other chats request schema changes from the integration coordinator.

## Blocked by

None — can start immediately.

## Acceptance criteria

- [x] Scheduled bounded fetch ingests real forecast responses through the existing run store, preserving issued/fetched/available/location/unit semantics.
- [x] Retries/duplicates/provider failure preserve prior valid data and report freshness/coverage.
- [x] Replay never consumes runs not available as of its clock; no reanalysis masquerading as issued forecast.
- [ ] Missing required observations/forecast inputs are explicitly unavailable; demo saved runs remain usable.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [ ] `uv run pytest tests/apps/forecast tests/apps/stations tests/apps/advisor -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [ ] `uv run alembic heads` — one head after integration.
- [ ] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Verification tier 3 (public ingestion/replay contracts). Failure model recorded before verification:
- Malformed/oversized/nonfinite provider response or wrong units: bounded adapter validation, adversarial job tests.
- Repeated/concurrent fetch: transactional farm lock and persisted cache; duplicate receipt must not overwrite values.
- Timeout/provider failure: preserve prior valid immutable receipt; expose fetch health separately.
- Future-issued or unavailable data leaks into replay: store selection filters plus HTTP/job regressions.
- Missing required hourly fields/observations: typed evaluation remains limited/unavailable; UI states coverage explicitly.
- Requested/grid locations or fetch/issue time confusion: explicit provenance metadata and rendering tests.

Baseline: focused forecast/stations/advisor suite: 603 passed, 134 deselected (26.20s). Initial DB connection exit 3 resolved by parent starting local PostGIS. Preflight Ruff passed; 2927 tests collected. mypy unavailable and not configured; not a passed layer.

Ownership dependency accepted by integration coordinator: ticket 05 owns the historical-observation guard in advisor jobs/service (no station, empty/insufficient historical series, rejected inputs), with explicitly unavailable stored assessment/headline and public-seam regressions. Ticket 04 owns missing forecast inputs and weather UI. Full M1 missing-input acceptance remains dependent on ticket 05 integration; this branch alone does not establish that criterion.

Implementation evidence (production frozen; full gate pending):

| Contract | Evidence |
|---|---|
| Bounded ingestion, units/location/rain, duplicate preservation, failure health | `tests/apps/forecast/test_live_weather.py` — 41 tests |
| Generated missing-value invariant through public job/store | `tests/apps/forecast/test_live_properties.py` — 30 passing Hypothesis examples |
| Forecast missingness/replay/source labels | HTTP Home/Plan/Data sources/Notifications and assistant public tool contracts |
| Cross-process duplicate protection | Real separate-session concurrent `run_now`: one fetch, one receipt |
| Per-farm malformed-response isolation | Bad second-farm timestamp cannot roll back first farm receipt |

Latest post-mutation contract run: `.venv/bin/python -m pytest tests/apps/forecast/test_live_weather.py tests/apps/forecast/test_live_properties.py -q --hypothesis-show-statistics --cov=farmcore --cov-report=xml:/tmp/farmcore-weather-restored.xml` — **42 passed in 9.93s**; Hypothesis 30 passing, 0 failing, 1 discarded generated case.

Independent QA: broader forecast/stations/advisor/notifications/assistant regression **690 passed, 3 skipped, 136 deselected (128.45s)** before the final test-only boundary additions; independent adverse reproductions **2 passed**. Reviewer then inspected the final added property/boundary tests and reported no remaining spec/standards findings. Browser layout not exercised.

Manual mutation fallback: no mutation tool configured. Three plausible bugs planted in an isolated package copy with verified import path: dropping future-issued selection, converting null weather to zero, removing content deduplication. All **3 killed** (1 failed each: 2.07s, 18.92s, 1.46s); null mutation killed by Hypothesis. Original source SHA-256 stayed unchanged; copied mutants restored. Fresh 42-test run above passed afterward.

Ruff whole-tree check passed; **445 files already formatted**. Alembic **one head** (`inventory_0001`); no migration authored. Secrets gate self-test rejected 4/4 bad and accepted 2/2 benign controls; allowlist isolation 2/2. Manual execution on a throwaway migrated DB with controlled realistic 144h hourly response: job `ok=True`, HTTP **200**, fetched/unknown-issue labels present. No production DB or live credentials used.

Changed-line coverage diagnostic: every changed forecast line covered; assistant/notification historical-run branches await the full-suite coverage result. Tier-3 gauntlet started as a diagnostic against docs-only base `8709609`; it was interrupted before source edits after integration QA found two additional real defects (499 passed, 8 skipped in 1220.67s, **not a pass**). The final gate uses the verified real merge base `b44245b` and this ticket’s approved non-data selection (`PYTEST_ADDOPTS='-m "not data"'`). This is **not a completed gauntlet** and branch is not ready to integrate until its result is resolved.

Failed then fixed: QA reproduced malformed timestamp batch rollback and fetched-time-as-issued labels in notifications/assistant; both received public-seam regression coverage and independent passing reproductions. Invalid finite values, numeric overflow, malformed health, cache starvation and incomplete forecast grading also covered.

Known limits: historical-observation assessment guard belongs to ticket 05; provider uptime/commercial usage and browser layout are not established by controlled-response tests. Missing mypy/pytest-randomly/mutation tools are not passed layers.


Integration QA follow-up (2026-10-19): read both P2 findings before fixes and independently reproduced the public-job probes: **2 failed in 1.63s**. Extreme offset timestamp `0001-01-01T00:00:00+01:00` overflow escaped the per-farm boundary; buffered `httpx.get` consumed all 6,553,600 bytes before checking the size. Minimum fixes isolate `OverflowError` per farm and use `httpx.stream` with decoded-byte accumulation capped at 256,000 bytes and context-managed closure. Streaming transport regression was observed RED (100 chunks), then GREEN (at most 4 chunks, closed, prior receipt immutable). Existing external response mocks moved to actual HTTPX `MockTransport`/`client.stream` boundaries without weakening assertions. Fresh forecast regression: **137 passed in 19.66s**; weather/property contracts **44 passed**, including 30 generated examples. New final gate and independent QA pending; PR stays draft. HTTP timeout is per read; no total wall-time deadline claim.
