---
type: Work Ticket
title: "Run two isolated installations from one release"
description: "Deliver a reproducible self-hosted release/profile with independently named deployments, runtime/database credentials, private networks/files/secrets, host-scoped cookies and bounded resources."
status: blocked
blocked_by: ["../../farmcore-revamp/tickets/01-private-access-default.md"]
---

# 01 — Run two isolated installations from one release

**Source:** [Approved Change Spec](../spec.md), requirements I1. **Tier:** 3.

## Outcome and scope

Deliver a reproducible self-hosted release/profile with independently named deployments, runtime/database credentials, private networks/files/secrets, host-scoped cookies and bounded resources. Use the same artifact for hosted allocation. Exclude hosting portal, payment and production rollout.

## Touchpoints

Dockerfile; docker-compose.yml; .env.example; settings/startup; CLI; docs/self-hosting.md. Introduce an installation-identity/config contract without reusing farm_id as installation identity.

## Blocked by

- [Current revamp private access](../../farmcore-revamp/tickets/01-private-access-default.md). Packaging preparation may be isolated; a release cannot qualify on the legacy anonymous-access default.

## Acceptance criteria

- [ ] Two disposable deployment projects from the same image run concurrently without port/name/volume/cache collisions; opt-in demos stay separate.
- [ ] An A runtime is denied B database/files; B remains available when A is stopped. Inspect actual network, mounts and database roles, not only UI selectors.
- [ ] Self-hosted startup/health and local configuration work without a hosting manager, subscription or AI endpoint; secrets are runtime inputs and absent from tracked artifacts.

## Execution discipline

One implementation worker owns the bounded slice; other agents are not alone and must preserve peers' edits. Work only after blockers pass; inherited draft code is not a completed dependency. Use TDD through the spec-approved public CLI/HTTP/job/package seams: observed RED, minimum GREEN, review/refactor, one vertical behavior at a time. Mock only external infrastructure/providers, not internal collaborators. The integration coordinator alone authors serialized revisions; keep one linear supported release chain. No live customer data, provisioning, payments, production migrations or deployment. Independent QA reviews spec conformance and standards separately before merge. Current PRs retain their own gates.

## Verification

The named new test files are implementation deliverables, not existing test claims.

- [ ] `uv run pytest tests/instances/test_isolation.py tests/instances/test_self_host.py -q` — meaningful public contracts and semantic golden comparisons pass; retain relevant existing regression assertions.
- [ ] Tier-3 `verify` with real merge base: write failure model first; property/adversarial cases plus mutation or restored isolated manual mutants; required smoke through actual CLI/HTTP/worker/containers. Record failures before fixes and skips honestly.
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass; update paths for a separately packaged hosting/addon layout and explicitly record scope.
- [ ] `uv run alembic heads` — one supported head; test upgrade of populated legacy data for schema changes. Release/profile compatibility and clean restored source hashes verified.
- [ ] Fresh independent reviewer checks every criterion on frozen source and records exact SHA/profile, commands, counts, baseline and remaining limits. Final integrated full/data/browser/Linux qualification belongs to20; focused checks do not claim it.

## Evidence

Not started. No product implementation or verification claimed by publishing this ticket.
