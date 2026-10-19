---
type: Work Ticket
title: "Make module disablement effective across processes"
description: "Disabling a module takes effect everywhere: web routes, mutations, assistant tools, bot and worker."
status: blocked
blocked_by: ["06", "07"]
---

# 15 — Make module disablement effective across processes

**Source:** [Change Spec](../spec.md) R4. **Track:** D — Module ecosystem. **Tier:** 3.

## Outcome

Disabling a module takes effect everywhere: web routes, mutations, assistant tools, bot and worker. In-flight work is fenced by a module-state generation. Historical data stays readable.

## Scope

In: module-state generation in the database; route/tool/task admission checks; extension of the publication fence and `may_dispatch`; settings schema validation; dependency checks before activation. Out: installing code at runtime.

## Touchpoints

core/registry.py; core/routes_apps.py; core/access.py; core/tools.py; core/tasks.py; notifications admission.

## Blocked by

- [06 — Move station ingest and advisor evaluation onto durable tasks](06-durable-job-adoption.md)
- [07 — Dispatch notifications through a transactional outbox](07-notification-outbox.md)

## Acceptance criteria

- [ ] UI, direct HTTP, assistant tools and a second process agree on a disabled module within one request/claim.
- [ ] An in-flight attempt started before disable cannot publish after it; queued outbox intents of the module are held.
- [ ] Missing/incompatible/cyclic dependencies and invalid settings fail with no partial activation or domain writes.
- [ ] Disable → re-enable retains history and reseeds nothing.

## Verification

- [ ] `uv run pytest tests/core/test_module_lifecycle.py tests/core/test_registry.py -q`
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests`.
- [ ] `uv run alembic heads` shows the supported heads only; populated-legacy upgrade tested for schema changes.
- [ ] Tier-3 `verify`: written failure model first, adversarial/property cases, mutants killed and restored, real CLI/HTTP/worker/container smoke.
- [ ] Independent review against this ticket on a frozen SHA. See [execution rules](../tickets.md#execution-rules).

## Evidence

Not started.
