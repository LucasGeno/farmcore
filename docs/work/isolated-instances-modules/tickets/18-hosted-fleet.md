---
type: Work Ticket
title: "Operate hosted installations from a declarative inventory"
description: "The operator runs customer installations from one YAML file per installation."
status: deferred
blocked_by: ["03", "../../farmcore-revamp/tickets/03-authenticated-sharing.md"]
---

# 18 — Operate hosted installations from a declarative inventory

**Source:** [Change Spec](../spec.md) D7. **Track:** E — Hosting (deferred). **Tier:** 3.

**Deferred until:** First committed hosted customer, or Lucas operating more than two installations.

## Outcome

The operator runs customer installations from one YAML file per installation. `fleet apply` converges each to its declared release, domain, limits and state (active/suspended/archived); `fleet backup`, `fleet upgrade` and `fleet status` wrap tickets 02–03. Customers receive a one-use, expiring owner setup link.

## Scope

In: first evaluate Coolify/Dokploy/Kamal against D1 + the backup/upgrade contract and record the choice; otherwise a small `fleet` CLI in a separate package. Strict validation of names/domains (allowlist regex, server-generated project ids, no shell interpolation). Caddy on-demand TLS with an `ask` endpoint backed by the inventory. Suspend = stop runtime + pause effects, keep data and backups. Archive = final bundle + stopped, reversible. One-use owner setup link in the runtime (also usable by self-hosters). Out: customer portal, payments, deletion.

## Touchpoints

New `hosting/` package or chosen tool config; runtime owner-invite CLI; Caddy config; ops runbook.

## Blocked by

- [03 — Upgrade an installation through one migrator with a tested recovery path](03-safe-upgrades.md)
- [Revamp 03 — authenticated sharing](../../farmcore-revamp/tickets/03-authenticated-sharing.md)

## Acceptance criteria

- [ ] Re-running `apply` after a crash at any step converges to one installation; only resources tagged with that installation's id are ever touched.
- [ ] Hostile names, domains and paths cannot inject commands, escape directories or select another installation.
- [ ] Setup links expire, are single-use, never logged, and cannot claim another installation.
- [ ] Suspend/resume/archive are idempotent, block new mutations and effects, and keep recovery data.

## Verification

- [ ] `uv run pytest hosting/tests -q`
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests`.
- [ ] `uv run alembic heads` shows the supported heads only; populated-legacy upgrade tested for schema changes.
- [ ] Tier-3 `verify`: written failure model first, adversarial/property cases, mutants killed and restored, real CLI/HTTP/worker/container smoke.
- [ ] Independent review against this ticket on a frozen SHA. See [execution rules](../tickets.md#execution-rules).

## Evidence

Not started.
