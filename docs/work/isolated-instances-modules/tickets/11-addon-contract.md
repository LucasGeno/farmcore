---
type: Work Ticket
title: "Install a separately packaged compatible FarmCore app"
description: "Publish a small stable extension interface and reproducible module-bundle/profile manifest."
status: blocked
blocked_by: ["10"]
---

# 11 — Install a separately packaged compatible FarmCore app

**Source:** [Approved Change Spec](../spec.md), requirements I5. **Tier:** 3.

## Outcome and scope

Publish a small stable extension interface and reproducible module-bundle/profile manifest. Demonstrate external package discovery with explicit compatibility, permissions, typed settings and migration ownership. No marketplace, arbitrary hosted pip install or independent migration branches.

## Touchpoints

core/manifest.py; core/registry.py discovery; package entry points/build metadata; extension author guide; external fixture distribution and bundle lock.

## Blocked by

- [10 — Make module activation and disablement effective across processes](10-module-lifecycle.md)

## Acceptance criteria

- [ ] A separately built fixture package installs through the documented interface and contributes a route/task/widget/map capability without editing core imports.
- [ ] Wrong core/module versions or missing dependencies are rejected before activation/migration; profile build identifies exact code and required reviewed schema chain.
- [ ] Hosted profiles accept reviewed builds; permission declarations are not described as Python sandboxing. A self-hoster can reproduce a custom compatible bundle from documented metadata.

## Execution discipline

One implementation worker owns the bounded slice; other agents are not alone and must preserve peers' edits. Work only after blockers pass; inherited draft code is not a completed dependency. Use TDD through the spec-approved public CLI/HTTP/job/package seams: observed RED, minimum GREEN, review/refactor, one vertical behavior at a time. Mock only external infrastructure/providers, not internal collaborators. The integration coordinator alone authors serialized revisions; keep one linear supported release chain. No live customer data, provisioning, payments, production migrations or deployment. Independent QA reviews spec conformance and standards separately before merge. Current PRs retain their own gates.

## Verification

The named new test files are implementation deliverables, not existing test claims.

- [ ] `uv run pytest tests/core/test_external_addon.py tests/core/test_manifest_fields.py -q` — meaningful public contracts and semantic golden comparisons pass; retain relevant existing regression assertions.
- [ ] Tier-3 `verify` with real merge base: write failure model first; property/adversarial cases plus mutation or restored isolated manual mutants; required smoke through actual CLI/HTTP/worker/containers. Record failures before fixes and skips honestly.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass; update paths for a separately packaged hosting/addon layout and explicitly record scope.
- [ ] `uv run alembic heads` — one supported head; test upgrade of populated legacy data for schema changes. Release/profile compatibility and clean restored source hashes verified.
- [ ] Fresh independent reviewer checks every criterion on frozen source and records exact SHA/profile, commands, counts, baseline and remaining limits. Final integrated full/data/browser/Linux qualification belongs to20; focused checks do not claim it.

## Evidence

Not started. No product implementation or verification claimed by publishing this ticket.
