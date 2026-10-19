---
type: Work Ticket
title: "Install an externally packaged threshold-alert app"
description: "Third parties can build FarmCore apps."
status: blocked
blocked_by: ["14", "15"]
---

# 16 — Install an externally packaged threshold-alert app

**Source:** [Change Spec](../spec.md) R4, R6, D5. **Track:** D — Module ecosystem. **Tier:** 3.

## Outcome

Third parties can build FarmCore apps. Proven by a separately built `farmcore-threshold-alerts` package that installs through entry points, migrates its own Alembic branch, and runs on a bundle without crop protection.

## Scope

In: `farmcore.apps` entry-point discovery, `core_api` compatibility range, per-app Alembic branch (branch base with `down_revision=None`, `branch_labels=(app,)`, `depends_on` a core revision; app-prefixed revision IDs) loaded through `version_locations`; `farmcore migrate` upgrades `heads` instead of `head`; `core_api` checked by ticket 03's upgrade preflight; build-time bundle procedure (extra packages locked and baked into the image, never installed at runtime), author guide in CONTRIBUTING.md, the alert app (owner-configured frost/heat thresholds over forecast receipts) as an out-of-tree fixture package built in CI. Out: marketplace, runtime pip install.

## Touchpoints

core/registry.py discover(); core/manifest.py; migrations env.py version_locations; CONTRIBUTING.md; new examples/farmcore-threshold-alerts package.

## Blocked by

- [14 — Remove crop-protection assumptions from shared core](14-neutral-core-contract.md)
- [15 — Make module disablement effective across processes](15-module-lifecycle.md)

## Acceptance criteria

- [ ] The externally built wheel installs, migrates its branch, contributes route/task/widget/map layer, with no edits to core imports.
- [ ] Wrong `core_api`, missing dependency or branch not rooted on a supported core revision fails before migration.
- [ ] Alerts show threshold, timezone, receipt time and missing/stale status; thresholds off by default; no crop-damage claims; replay repeatable.
- [ ] Disable/re-enable keeps notices and creates no duplicates.
- [ ] Each notice links to the forecast receipt it came from.
- [ ] A self-hoster rebuilds an identical image from the documented bundle lock (same package hashes).

## Verification

- [ ] `uv run pytest tests/core/test_external_app.py examples/farmcore-threshold-alerts/tests -q`
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests`.
- [ ] `uv run alembic heads` shows the supported heads only; populated-legacy upgrade tested for schema changes.
- [ ] Tier-3 `verify`: written failure model first, adversarial/property cases, mutants killed and restored, real CLI/HTTP/worker/container smoke.
- [ ] Independent review against this ticket on a frozen SHA. See [execution rules](../tickets.md#execution-rules).

## Evidence

Not started.
