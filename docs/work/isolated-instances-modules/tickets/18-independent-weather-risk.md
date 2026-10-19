---
type: Work Ticket
title: "Run a weather-risk app without crop protection"
description: "Ship a separately packaged reference app with parcel/home/map contributions and bounded task: owner-configured heat/cold forecast threshold notices."
status: blocked
blocked_by: ["11","13","17"]
---

# 18 — Run a weather-risk app without crop protection

**Source:** [Approved Change Spec](../spec.md), requirements I7. **Tier:** 3.

## Outcome and scope

Ship a separately packaged reference app with parcel/home/map contributions and bounded task: owner-configured heat/cold forecast threshold notices. Default rules disabled. No crop damage probability, treatment, drought model or remote alert subscription.

## Touchpoints

New weather-risk addon package; forecast public receipt interface; parcel/home contribution contracts; module profile and author example.

## Blocked by

- [11 — Install a separately packaged compatible FarmCore app](11-addon-contract.md)
- [13 — Retain distinct crop seasons on a stable parcel](13-crop-seasons.md)
- [17 — Remove mandatory crop-protection assumptions from shared core](17-neutral-core-contract.md)

## Acceptance criteria

- [ ] Install/configure/activate on a profile without crop protection; direct HTTP, map/home and source-linked notice work through public interfaces.
- [ ] Exact threshold/timezone/replay/stale/missing-input examples show the forecast condition and receipt semantics without claiming crop safety/damage.
- [ ] Disable blocks direct/tools/task use, retains authorized history, and re-enable produces no duplicate notices. Exercise external package build, not only in-tree imports.

## Execution discipline

One implementation worker owns the bounded slice; other agents are not alone and must preserve peers' edits. Work only after blockers pass; inherited draft code is not a completed dependency. Use TDD through the spec-approved public CLI/HTTP/job/package seams: observed RED, minimum GREEN, review/refactor, one vertical behavior at a time. Mock only external infrastructure/providers, not internal collaborators. The integration coordinator alone authors serialized revisions; keep one linear supported release chain. No live customer data, provisioning, payments, production migrations or deployment. Independent QA reviews spec conformance and standards separately before merge. Current PRs retain their own gates.

## Verification

The named new test files are implementation deliverables, not existing test claims.

- [ ] `uv run pytest tests/addons/test_weather_risk.py tests/instances/test_weather_only_profile.py -q` — meaningful public contracts and semantic golden comparisons pass; retain relevant existing regression assertions.
- [ ] Tier-3 `verify` with real merge base: write failure model first; property/adversarial cases plus mutation or restored isolated manual mutants; required smoke through actual CLI/HTTP/worker/containers. Record failures before fixes and skips honestly.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass; update paths for a separately packaged hosting/addon layout and explicitly record scope.
- [ ] `uv run alembic heads` — one supported head; test upgrade of populated legacy data for schema changes. Release/profile compatibility and clean restored source hashes verified.
- [ ] Fresh independent reviewer checks every criterion on frozen source and records exact SHA/profile, commands, counts, baseline and remaining limits. Final integrated full/data/browser/Linux qualification belongs to20; focused checks do not claim it.

## Evidence

Not started. No product implementation or verification claimed by publishing this ticket.
