---
type: Work Ticket
title: "Live weather arrives with provenance and freshness"
description: "Milestones 1–4: live weather arrives with provenance and freshness."
status: done
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
- [x] Missing required observations/forecast inputs are explicitly unavailable; demo saved runs remain usable.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [x] `uv run pytest tests/apps/forecast tests/apps/stations tests/apps/advisor -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [x] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [x] `uv run alembic heads` — one head after integration.
- [x] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [x] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

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


Total-deadline integration finding: independent real-loopback probe established that per-read timeout plus chunk buffering permits an unbounded drip while `STEP_LOCK_KEY` is held. This was a **new blocking P2**, not a passed limitation. Public job regressions were observed RED (delayed headers 4.094s; tiny body 3.117s, versus <0.75s with a 0.15s configured budget), before minimum GREEN. The sync job now runs a bounded async HTTP operation: `asyncio.timeout` encloses `AsyncClient`, headers, streamed decoded body, and context unwinding. Timeout cancels the actual request; no detached worker future. `forecast_fetch_timeout_seconds` defaults to 10, validated range 0.05–30. Per-farm timeout handling preserves prior receipts and allows the following farm to succeed. Both real socket tests assert closure and bounded whole-job return.

Fresh deadline-fix evidence: **139 forecast tests passed in 13.29s**; **46 weather/property contracts passed in 4.50s**, including 30 generated examples; real delayed-header/drip cases **2 passed in 1.90s**. Ruff check/format pass. Independent post-deadline QA and final feature gate remain pending. Earlier 42/44-test, mutation, coverage and smoke numbers refer to pre-deadline production snapshots and are not final evidence for this fix.

Recovery: the retained final gate log was empty and no pytest process survived; therefore no completed gate was recovered. Subsequent attempt exited **3** because dev PostGIS was unavailable (**5 passed, 268 deselected, 3.08s**); this is environment failure, not product failure or pass. Parent restored the existing local dev database. Final verification will record selected directories/seed and run four writer-before-reader lifecycle cases separately with `--randomly-dont-reorganize`. Runtime randomization tool installed with `uv pip install pytest-randomly==5.0.0`; no test imports this optional tool.


### Final forecast slice and integration evidence — 2026-10-19

The following supersedes pending/pre-deadline diagnostics above for the integrated forecast slice. Frozen `c6762c8a498f4be4bcff66ee85c1b0a331403212` adds a total async fetch deadline and bounded DNS admission (two lazy daemon workers, two outstanding permits held until native completion). A timed-out DNS lookup can linger within that fixed bound; canceled/closed-loop work cannot issue late HTTP or retain the job lock. Parent independently passed five public-job liveness probes in3.59s and cleared the blocking review.

Final feature tier3, real base3b7cca7, forecast/stations/advisor/assistant/notifications non-data selection, seed2688013886: **711 passed,3 skipped,136 deselected in251.55s**, changed coverage100%, Ruff447 formatted. Seven isolated manual mutants were all killed (future issue, missing rain, content deduplication, stream byte cap, timestamp isolation, total deadline, DNS admission); original source hashes unchanged. Ordered lifecycle4passed1.96s; real migrated-DB/controlled-provider smoke `forecast.fetch_live ok=True`,20ms, HTTP200 with fetched/unknown-issue labels. Feature environment skipped unavailable dependency audit/types/mutation tool; no skip is a pass. Report `/tmp/farmcore-weather-feature-final-gauntlet.log`, mutants `/tmp/farmcore-weather-reviewed-mutations.json`, smoke `/tmp/farmcore-weather-reviewed-smoke.log`.

Exact-head hosted [CI39281777500](https://github.com/LucasGeno/farmcore/actions/runs/39281777500) completed successfully; authenticated completed logs verify checkout of the frozen PR merge and Python **2702 passed,5 skipped,268 deselected in321.11s**, lint, dependency audit, Node and Docker. Stale queued API/job metadata did not override the completed run/logs. No cancellation succeeded and no CI setting was changed. PR7 merged normally into integration at `f2630fa4785687b90c862bc0903225f0c92c561a` (16:24:17UTC).

Actual-tip tier2 at f2630fa, real pre-merge base9cdf1bc, same selected directories/seed: **711 passed,3 skipped,136 deselected in273.62s**, changed coverage100%, lint/secrets/dependency audit clean (local package unavailable on PyPI), unconfigured types/smoke and tier2 mutation skipped. Report `/tmp/farmcore-post-weather-gate.json`. Single `inventory_0001` migration head, no weather migration.

Forecast slice integrated; ticket04 remains in progress until ticket05's historical-observation missingness guard completes the final acceptance criterion. No full M1 or deployment claim.

### Fresh reviewer close-out — Staff, 2026-10-27 (main `9530045`)

AC-4 verified after ticket 05 landed (PR #26 merged, `b3e93f2`): the advisor returns
explicit unavailable states — `unavailable_reason` in `advisor/service.py` ("no weather
station attached to this farm"), the engine sets `assessment_status="unavailable"`
(`engine/decision.py`, Literal in `engine/types.py`), and consumers never reassure:
`test_consumers_never_reassure_about_an_unavailable_or_limited_assessment`
(`tests/apps/advisor/test_review_evidence.py`) and
`test_empty_observations_are_unavailable_in_season_and_a_gate_before_it`
(`tests/apps/advisor/engine/test_invariants.py`) are the current successors of the
handover-era AC-4 test name. Demo saved runs stay usable through the stored-assessment
read (`test_exact_assessment_read_survives_station_detach_and_current_overlay`). Feature
tier-3 PASS, exact-head CI and integration merge are on record above. Status set to done.
