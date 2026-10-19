---
type: Work Ticket
title: "Independent QA verifies milestones 1–4"
description: "Milestones 1–4: independent qa verifies milestones 1–4."
status: approved
blocked_by: ["03", "06", "11"]
---

# 12 — Independent QA verifies milestones 1–4

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Independent QA verifies milestones 1–4. Touchpoints: whole branch review; migration chain; regression/browser/security tests; README/SECURITY. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain. Implementation workers propose schema changes; only the designated coordinator authors/serializes revision files. During access tickets 01–03, the access chat coordinator owns revisions exclusively; other chats request schema changes from the integration coordinator.

## Blocked by

- [03](./03-authenticated-sharing.md)
- [06](./06-recommendation-review.md)
- [11](./11-job-completion.md)

## Acceptance criteria

- [ ] Fresh QA agent reports spec gaps separately from standards findings; all blocking findings fixed.
- [ ] Real PostGIS role/expiry/revoke/CSRF/retry/concurrency/rollback regressions and browser journeys pass.
- [ ] One Alembic head, fresh-install upgrade and relevant existing-database upgrade pass.
- [ ] verify tier3 evidence includes failure model, test/lint/frontend/build/security results and manual fallbacks for missing tooling; PRs integrated only after QA.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [ ] `bash ~/.claude/scripts/gauntlet.sh --tier=3 --base=origin/main; node --test tests/core/*.mjs tests/apps/fields/*.mjs` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [ ] `uv run alembic heads` — one head after integration.
- [ ] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Pending implementation. No acceptance criteria verified yet.

### Tier-3 integration failure model

| Failure mode | Required verification layer |
|---|---|
| Anonymous/foreign/expired/revoked identity reads private data or global audit | Real HTTP role matrix across pages, exports, fragments and assistant; generated hostile selectors; manual authorization mutants |
| Cookie mutation bypasses CSRF or role checks | Real HTTP absent/foreign token cases plus browser form journeys |
| Weather retry/failure destroys last good receipt; future receipt leaks into replay | Job/CLI with controlled provider failures and separate-session races; clock-bounded HTTP/store assertions; missingness properties |
| Parameter activation affects another farm or rewrites historic assessment | Real HTTP version/review/activation history and unchanged assessment snapshots; deterministic engine examples |
| Stale or concurrent plan edit loses decisions; resource overlap accepted | Separate committed database sessions, revision conflicts, adjacent/overlap boundary properties |
| Duplicate correction/completion applies stock effect twice or fails partially | Retry and barrier-coordinated concurrent HTTP confirmations, canonical source identity, forced transaction rollback and computed-stock assertions |
| Re-evaluation runs before commit or after failed transaction | Public confirmation/completion outcome and persisted assessment assertions across independent sessions |
| Browser form, focus, refresh or HTMX response breaks the authorized journey | Real-browser login/review/plan/replan/complete/correct paths and console/response checks |
| Schema upgrade or intermediate integration leaves parallel heads/data loss | One-head assertion, fresh migration, existing-database upgrade/round trip and metadata check |

### Pre-feature baseline

At `d0f44f7` product source (integration docs through `0b577c4`): non-data regression `uv run pytest -m 'not data' -q` reports **2656 passed, 3 skipped, 268 deselected in 311.40s**; Node tests **30 passed**; Ruff check/format **442 files pass**; Alembic head **inventory_0001**. Full randomized suite (seed **1025145244**) completed **2901 passed, 25 skipped, 1 failed in 1450.04s**. Exact inherited failure: `tests/test_conftest_isolation.py::test_row_is_gone_in_the_next_test`, `_require_writer("session")` asserted `session in _WROTE` but `_WROTE` was empty. This module explicitly requires writer-before-reader order. Minimized module-only reproduction with the same seed: **1 failed, 3 passed in 1.74s**; changing only `--randomly-dont-reorganize`: **4 passed in 1.51s**. No product changes were present. Final gates randomize ordinary tests with `--ignore=tests/test_conftest_isolation.py`, then execute those four lifecycle tests separately with `--randomly-dont-reorganize`; assertions remain unchanged and ordered evidence is reported separately. Feature coverage gates use their approved `-m "not data"` selection; full regression including available data runs separately without coverage instrumentation. Incomplete coverage/data diagnostic runs never count as passed. These are baseline results, not milestone acceptance evidence.

Verification dependencies installed only in local environment: pytest-cov, diff-cover, hypothesis, pytest-randomly. Mypy is unconfigured/unavailable; it is not reported passed. No mutation runner or repository smoke script is configured: final tier3 uses documented manual mutants and actual application execution. Built-in secrets gate self-test rejected **4/4** bad controls and accepted **2/2** benign controls.


### Browser auth regression — final aggregate blocker (2026-10-19)

A real visible Chromium journey on PR8 `cef01d7` with a disposable migrated database completed anonymous denial, wrong-token denial, operator form login, secure/HttpOnly/Strict cookie, operator audit access, logout cookie removal, post-logout denial and forged-tour denial. The overall browser check **failed** its zero-page-exception assertion: operator sign-out redirects to protected `/`, which returns JSON403 and produces native `InvalidStateError: Transition was aborted because of invalid state. ViewTransition opt-in disabled` with normal motion. The identical reduced-motion control produced no exception, while both denied private access. Eight public sign-in/health navigation loops did not reproduce it.

The public sign-in screen also polls notifications while logged out and displays a notification failure banner. Ticket02 owns a public sign-in logout landing and removal of private widgets/polling from logged-out bootstrap screens, with HTTP/browser regressions for the real landing and absence of unauthorized widget requests. Preserve anonymous403/no metadata behavior. Final aggregate/main merge remains blocked until the actual browser journey passes after this fix; no full browser pass is claimed for ticket01. Report/scripts: `/tmp/farmcore-access-browser-qa.md`, `/tmp/playwright-test-farmcore-private-access.js`, `/tmp/playwright-test-farmcore-logout-repro.js`; screenshot `/tmp/farmcore-access-browser-login-mobile.png`. The fixture server shut down and its disposable database was dropped.


### Preliminary composed validation — 2026-10-19

Before feature merges, exact frozen01/04 patches composed cleanly on aeb7ece in an isolated checkout, staged tree `53e48c630df30f58b08e051a7fc675ccced3ee3a`. Tier2 ordinary randomized non-data regression (seed19012026): **2726 passed,3 skipped,268 deselected in501.29s**, changed coverage100%, lint/secrets/dependency audit clean; unconfigured types/smoke and tier2 mutation skipped. Ordered lifecycle4passed1.62s and Node30passed separately. Gate JSON identifies HEADaeb7ece; the staged tree above, not that HEAD alone, is the tested composition. After PR7/8 merges, `git diff --quiet` confirms src/tests/pyproject.toml/uv.lock byte-identical to integration546b077. Full available-data regression on this composition is still running; not a pass or final12 acceptance. No02 code included. Report `/tmp/farmcore-combined-qa-gauntlet.json`.


Full available-data regression on the same composed source completed: `.venv/bin/pytest --ignore=tests/test_conftest_isolation.py --randomly-seed=19012026 -q` → **2972 passed,25 skipped in950.62s**. No ordinary case failed; all four unchanged ordered lifecycle cases passed separately above. Source/test/dependency equivalence to actual integration546b077 was verified before completion; this is strong01/04 regression evidence, not final02–12 acceptance. Log `/tmp/farmcore-combined-qa-full-data.log`.


### Inherited quantity-order assertion — diagnosed and corrected

The frozen01 baseline and02 actual-base gate both failed `tests/core/test_logs.py::test_confirm_merge_null_product_uses_measure_label_unit[rate-sulphur-kg/ha-True]` with the same copper/sulphur order difference. Root retained the02 JUnit trace; no other ordinary failure appeared in that gate (1764passed,3skipped,72deselected,312.48s). This result was not reported passing.

Ranked hypotheses: unordered database reload, wrong merge values, fixture leakage. A minimal copy of the exact four-case test added only real PostgreSQL `CLUSTER quantity USING (id DESC)` before expiration/reload on a disposable database. Original assertion: **3failed,1passed in1.01s**; all added-quantity cases returned identical tuples in reversed physical order. `Log.quantities` has no `order_by`; the adjacent product-ID merge test already normalizes tuple order. This confirms physical row order, with neither a changed merge value nor fixture leakage needed.

Only that existing assertion now compares sorted exact `(measure,label,unit,value)` tuples to sorted expected tuples. Multiplicity, quantity IDs/reassignment, merged target, total row count and event payload checks remain. No production, fixture, skip, selector, xfail or threshold change. Same physical-order probe **4passed in0.80s**; deliberate expected dose5→6 control still **3failed,1passed in0.86s**. Fresh independent reviewer confirmed no semantic weakening/missing check. Root tier1 verification (test-harness-only, real basef3461c5): **29passed in1.11s**, randomized seed3037958567, lint/secrets clean; types unconfigured, coverage/audit/execution/mutation skipped at tier1. Reports `/tmp/farmcore-quantity-order-{red.log,green.log,gate.json}` and `/tmp/farmcore-quantity-wrong-dose-red.log`. Subsequent02 full gate must include this correction and remain fully randomized.
