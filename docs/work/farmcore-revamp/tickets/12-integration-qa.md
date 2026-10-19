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
