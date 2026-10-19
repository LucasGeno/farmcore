---
type: Work Ticket
title: "Back up and restore an installation into an independent copy"
description: "An owner can export their installation as one encrypted file and restore it as a new, independent installation, self-hosted anywhere."
status: blocked
blocked_by: ["01", "../../farmcore-revamp/tickets/02-member-sessions.md"]
---

# 02 — Back up and restore an installation into an independent copy

**Source:** [Change Spec](../spec.md) R2, D2, D4. **Track:** A — Portable installation. **Tier:** 3.

## Outcome

An owner can export their installation as one encrypted file and restore it as a new, independent installation, self-hosted anywhere. The copy keeps every record and its provenance, gets new secrets, invalidates sessions and starts with external effects paused, so two copies never both send.

## Scope

In: `farmcore backup` / `farmcore restore` CLI; bundle = pg_dump (custom format) + JSON manifest (installation id, release, module versions, Alembic heads, created_at, SHA-256) encrypted with `age` to an owner recipient key; installation setting `external_effects` (`enabled`/`paused`) that gates today's only outbound effect, the `chat.push_advice` Telegram job and sender, and keeps a paused copy's bot from long polling (two pollers on one token conflict); restore = decrypt and verify the whole bundle to a temporary file first, then `pg_restore --no-owner --no-acl` as the owner role and re-grant the runtime role; restore-time secret rotation and session invalidation; a test asserting no private bytes are persisted outside Postgres (D2). Replace or wrap the existing daily `backup` service so it emits the same bundle. Out: hosting-side scheduling, cloud storage, deletion.

## Touchpoints

core/commands (new backup.py/restore.py); docker-compose.yml backup service; settings/installation state; apps/chat/push.py and the bot entry point (effects gate only); docs/self-hosting.md "Backups and restore". Use `age` (or `pyrage`) — no custom cryptography.

## Blocked by

- [01 — Run two isolated installations from one release](01-portable-installations.md)
- [Revamp 02 — member identities and sessions](../../farmcore-revamp/tickets/02-member-sessions.md)

## Acceptance criteria

- [ ] Backup of A restored into new C: semantic golden comparison of records, sources and module evidence matches; B keeps serving throughout.
- [ ] C has a new `installation_id`, new secrets, zero valid prior sessions, and `external_effects = paused` until the owner enables it: no advice push reaches A's Telegram chats and C's bot does not poll.
- [ ] Wrong key, modified manifest, truncated bundle and incompatible release are rejected before `pg_restore` starts; the source and prior bundles are untouched.
- [ ] Bundles never contain platform/operator secrets; connector re-entry is documented.
- [ ] A guard test fails if any route or job persists private bytes outside the database.

## Verification

- [ ] `uv run pytest tests/instances/test_backup_restore.py -q`
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests`.
- [ ] `uv run alembic heads` shows the supported heads only; populated-legacy upgrade tested for schema changes.
- [ ] Tier-3 `verify`: written failure model first, adversarial/property cases, mutants killed and restored, real CLI/HTTP/worker/container smoke.
- [ ] Independent review against this ticket on a frozen SHA. See [execution rules](../tickets.md#execution-rules).

## Evidence

Not started.
