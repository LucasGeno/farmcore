---
type: Work Ticket
title: "Run two isolated installations from one release"
description: "Two installations built from the same image run side by side on one host and cannot see each other."
status: blocked
blocked_by: ["../../farmcore-revamp/tickets/01-private-access-default.md"]
---

# 01 — Run two isolated installations from one release

**Source:** [Change Spec](../spec.md) R1, D1. **Track:** A — Portable installation. **Tier:** 3.

## Outcome

Two installations built from the same image run side by side on one host and cannot see each other. Each gets a stable server-generated `installation_id`, its own Compose project, database, role, network, volumes, secrets and cookies. The idle cost of one installation is measured and published so hosting prices rest on numbers.

## Scope

In: Compose project naming, `.env.example`, installation identity at first start, split database roles (an owner/migrator role that owns the schema and PostGIS extension; a DML-only runtime role with default privileges), cookie names/paths scoped by installation, a one-shot `migrate` service that app, bot and later worker wait for (remove `farmcore migrate` from the image CMD), configuration of units, farms, connectors and optional apps through env/CLI, demo data opt-in only, `docs/self-hosting.md` section "Running several installations" with measured RAM/disk/CPU. Out: hosting tooling, portal, payments, backups.

## Touchpoints

Dockerfile; docker-compose.yml; .env.example; settings.py; core/startup.py; core/commands; docs/self-hosting.md. Do not reuse `farm_id` as installation identity.

## Blocked by

- [Revamp 01 — private access by default](../../farmcore-revamp/tickets/01-private-access-default.md)

## Acceptance criteria

- [ ] Two disposable projects from one image run concurrently with no port/name/volume/cache/cookie collisions; stopping A leaves B healthy. Today's hard-coded `name: farmcore`, shared `farmcore:local` tag, `./backups` bind mount and shared `.env` are made per-project.
- [ ] A's runtime credentials cannot connect to B's database; the runtime role cannot create databases, roles, extensions or DDL. Inspect actual networks, mounts and roles, not UI.
- [ ] `installation_id` is generated once, survives restart, is never user-chosen and appears in `/health`.
- [ ] App and bot never run migrations themselves; only the `migrate` one-shot does (concurrency locking is ticket 03).
- [ ] A fresh installation without demo data starts empty and is configured through documented env/CLI (timezone, units, connectors, optional apps).
- [ ] Installation works with no network access to any hosting service, subscription endpoint or AI provider.
- [ ] Measured idle cost of one installation (with and without the Whisper model loaded) is in the self-hosting docs, with the method.

## Verification

- [ ] `uv run pytest tests/instances/test_isolation.py tests/instances/test_self_host.py -q`
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests`.
- [ ] `uv run alembic heads` shows the supported heads only; populated-legacy upgrade tested for schema changes.
- [ ] Tier-3 `verify`: written failure model first, adversarial/property cases, mutants killed and restored, real CLI/HTTP/worker/container smoke.
- [ ] Independent review against this ticket on a frozen SHA. See [execution rules](../tickets.md#execution-rules).

## Evidence

Not started.
