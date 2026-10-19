---
type: Change Spec
title: "Isolated installations and modular farm workflows"
status: approved
authorization: "Lucas approved isolated hosted/self-hosted installations and a modular architecture on 2026-10-19. Revision 2 (same day, after critical review) narrows scope to self-host-first portability, adopts a Postgres task library, decouples the dependency graph and defers the hosting manager behind a demand trigger; Lucas asked for this reshaping."
revision: 2
---

# Isolated installations and modular farm workflows

## Problem

FarmCore's promise is open, farmer-controlled farm data: a farmer can run it themselves, or have it run for them, and leave at any time with everything. Three things stand in the way.

1. **Portability is informal.** Self-hosting works through one Compose file, but there is no tested way to run several independent installations from one release, move an installation to a new host, or upgrade one safely.
2. **Background work is fragile.** Jobs run on an in-process APScheduler under one global advisory lock. A slow weather provider stalls every farm, and a crash loses in-flight work.
3. **The core is a fungicide app.** `Product` hard-codes contact/penetrant and downy/powdery. Any non-spray workflow, like fertilization, would either fake those fields or fork the model.

FarmCore is at 0.1 with no paying customers. This spec therefore builds what both self-hosters and a future hosted offer need. Hosting-only machinery waits until a real customer needs it.

## Goals

- One open-source release that runs as several fully isolated installations, self-hosted or operated by someone else. The artifact is the same either way.
- A tested backup → restore → upgrade path, so leaving a host or recovering from a failed upgrade is routine.
- Durable, per-farm background work that survives crashes and cannot publish stale results.
- A neutral farm model (catalog, quantities, crop seasons), proven by an in-tree fertilization app and an externally packaged alert app.

## Non-goals

- A customer portal, self-service signup, payments or a separate control-plane database. These are deferred behind a trigger; see Deferred.
- Kubernetes, a framework change, or a service per app.
- Shared-process or shared-table multi-tenancy across customers.
- Permanent or automatic deletion of customer data.
- Running hosted code uploads.
- New agronomic recommendation models (irrigation, nutrient rates, drought, pests). Pump or machinery control. Training on private farm data.

Milestones 1–4 ([revamp spec](../farmcore-revamp/spec.md)) remain a separate initiative. This spec neither completes nor replaces their QA. Irrigation and observation records from that work are preserved, not rebuilt.

## Vocabulary

See the [domain glossary](../../../CONTEXT.md). In short:
- An **Installation** is one deployment: its own database, secrets and configuration. It holds one or more Farms.
- *Hosted* and *self-hosted* describe who operates an installation, not which features it has.
- A **Planned job** is agricultural work. A **Background task** is software execution.
- A **Record** is a confirmed fact. An **Assessment** is a module's computed result, not proof of agronomic accuracy.

## Decisions

Each decision names the alternatives considered and why they lost.

### D1 — One Compose project per installation
Every installation is the stock `docker-compose.yml` project with a unique project name: its own app container, Postgres, network, volumes and secrets. A hosted installation is exactly a self-hosted one that the operator runs.

- *Rejected: a shared process with one database per site* (the Frappe/Odoo multi-site model). It is cheaper per customer, but one routing bug leaks data across customers, and every customer must upgrade in lockstep.
- *Rejected: one shared hosted installation with many farms.* Per-farm access already exists after revamp02/03, but export, upgrade timing and configuration would all be shared, which contradicts "leave with everything".
- *Revisit when:* the measured idle cost per installation (ticket 01) times the expected customer count exceeds the hosting budget. The first option to try then is a shared Postgres server with one database and one least-privilege role per installation; app containers stay separate.

### D2 — Private bytes live in Postgres
Today no private file outlives a request: voice notes are transcribed from a temporary directory and ISOXML uploads are parsed in memory. Rule from now on: any persisted private bytes (retained uploads, evidence attachments) go into a content-addressed table in the installation database.

A consistent backup is then one `pg_dump`, with no separate cross-store snapshot. `DATA_DIR` stays for open-data caches, which can be fetched again and are not backed up.
- *Rejected: object storage plus a coordinated snapshot.* Real complexity for a problem FarmCore does not have. Revisit if a feature needs large media (more than about 50 MB per object).

### D3 — Adopt a Postgres task library, own only the fencing
Live-mode background work moves to a Postgres-backed queue. The first candidate is **Procrastinate** (MIT, psycopg3 connectors, retries, locks, cancellation, periodic tasks, stalled-job retry). Two things are unverified: transactional enqueue from SQLAlchemy's underlying psycopg3 connection, and running sync tasks under its async worker. The spike must prove both. The fallback is a minimal in-house `SKIP LOCKED` table. Ticket 04 is a time-boxed spike that decides between them against written criteria.

FarmCore owns only what no library knows: the **publication fence**, stored in a FarmCore-owned attempt row rather than the library's job-status tables. Before committing a result, a task checks inside the same transaction that its attempt is still current, its farm is still eligible, the clock mode is unchanged and its module is still enabled.

Replay mode keeps the deterministic in-process `step_to`; the queue is only for live work. Determinism and liveness are different requirements.
- *Rejected: hand-rolled leases, attempt tokens and retry state* (the original tickets 07–08). That is roughly two Tier-3 tickets of concurrency code a maintained library already ships.
- *Rejected: Redis-based queues (Celery, RQ, Dramatiq).* They add a stateful service and lose transactional enqueue.

### D4 — External effects go through a transactional outbox
Today the only outbound effect is the Telegram advice push (`chat.push_advice`). It is cursor-based and at-most-once; in-app notifications are presentation-only. The source transaction writes a delivery intent to an `outbox` table. A background task dispatches it, deduplicating by intent ID and recording provider receipts. An ambiguous delivery is shown for reconciliation, never reported as exactly-once.

The installation-level `external_effects` switch (`enabled`/`paused`) is checked at dispatch. While paused, the Telegram bot does not long-poll, because two pollers on one token conflict. A restored copy starts `paused`. The switch arrives with backup/restore (ticket 02), before the outbox exists.

### D5 — Modules are Python packages with entry points; schemas use Alembic branches
- **Discovery.** In-tree apps stay where they are. External apps register through the `farmcore.apps` entry-point group and ship a `Manifest` that declares `core_api` (a compatible range).
- **Schemas.** An external app ships its own Alembic branch in its package, loaded through `version_locations`. The branch base has `down_revision=None`, `branch_labels=("<app>",)` and `depends_on` a core revision, and revision IDs carry the app prefix. Core keeps one linear chain. A release bundle is a locked set of packages (`uv.lock`), and `farmcore migrate` upgrades `heads` (today it upgrades `head`). External packages are baked into the image at build time, never installed at runtime.

  This resolves a contradiction in revision 1, which let modules declare migrations while forbidding any branch outside the core chain.
- **Hosted installations** only run bundles the operator has reviewed and built. Manifest permissions are application policy, not a sandbox.
- *Rejected: schema-less external apps writing into core `props` JSONB.* It hides typed data from validation and constraints.

### D6 — Neutral domain by expand, migrate, contract
- **Expand.** Add a neutral catalog item, typed quantities and crop seasons alongside existing data.
- **Migrate.** Move consumers one group at a time: records, inventory, assessments.
- **Contract.** Only then remove the fungicide restrictions from shared core. Crop-protection attributes move to that app's typed extension table.

Unit conversion uses `pint`, with an explicit FarmCore unit registry and `Decimal` arithmetic (`non_int_type=Decimal`), so stock never passes through floats. Quantities follow the farmOS shape: measure, value, unit and label, plus the original entered value and unit. Nothing is converted without a known factor (density, area or composition).
- *Rejected: a generic JSON payload or a rule DSL per app.* Each app owns its typed payload and validation.

### D7 — Hosted operations start as operator tooling, not a platform
When hosting begins, the operator runs installations from a declarative inventory: one YAML file per installation in a private ops repository. A `fleet` command reconciles it: create the project and secrets, apply the pinned release, run backups, set suspended or active, and print the current state.

Declarative reconciliation makes provisioning idempotent and crash-safe by construction. Re-running `apply` converges, so no durable provisioning state machine is needed. Custom domains use Caddy on-demand TLS gated by an `ask` check against the inventory.
- *Rejected for now: a separate hosting manager with its own identity store, control database and customer portal* (the Frappe Press model). It is a product in itself.
- *Considered: Coolify, Dokploy or Kamal.* Acceptable substitutes if they can express D1 and the backup/upgrade contract. Ticket 18 evaluates them before writing the `fleet` command.

## Requirements

### R1 — Portable, isolated installations
- Two installations from one image run side by side on one host. They share no project name, port, network, volume, database role, secret, cookie or cache.
- One installation's runtime cannot reach another's database. Each installation has an owner/migrator role (schema and PostGIS extension) and a DML-only runtime role.
- Configuration supplies base URL, timezone, units, farms, connectors and optional apps.
- An installation works fully offline from any hosting service, subscription endpoint or AI provider. Demo data is opt-in.
- Each installation has a stable `installation_id` generated at first start. It is never derived from `farm_id` and never chosen by a user.
- The idle resource cost (RAM, disk, CPU) of one installation is measured and published in the self-hosting docs.

### R2 — Backup, restore and upgrade
- **Backup bundle.** A backup is a single file:
  - a `pg_dump` (custom format)
  - a manifest with installation ID, release and module versions, Alembic heads, timestamp and SHA-256 of the dump
  - encryption with `age` to a recipient key the owner controls

  Platform and operator secrets are never included. Connector secrets are re-entered after a restore, by a documented procedure.
- **Restore checks.** Restore decrypts and verifies the whole bundle before `pg_restore` starts. It checks the manifest and hash, rejects incompatible releases, and refuses a wrong key or a truncated or modified bundle before touching the target.
- **Restoring into a new installation:**
  - keeps record, source and provenance identity
  - generates a new installation ID and new secrets
  - invalidates all sessions
  - starts with `external_effects = paused`, so two copies never both send. Only the owner re-enables effects.
- **Upgrade:**
  - check bundle compatibility
  - take a fresh backup
  - run migrations once from one designated process (the `migrate` one-shot service). App and worker wait for it and never migrate themselves.
  - report health

  Rolling back the image does not roll back the schema. A failed migration recovers by restoring the pre-upgrade backup, and that path is tested.
- **Isolation of failures.** A failed upgrade or restore of one installation leaves the others serving.

### R3 — Durable, fenced background work
- Live background tasks are persisted with farm and job identity, a bounded deadline and bounded retries.
- A crashed worker's tasks are retried, and a stale attempt cannot commit (D3 fence).
- A slow provider for one farm does not block other farms' tasks. No network call happens inside a long-held transaction or a global lock.
- Replay keeps its as-of inputs, deterministic order and farm restrictions.
- Switching between live and replay invalidates in-flight live attempts before they publish.
- Request handlers enqueue work. They never run an unbounded live fetch.
- Notifications and external deliveries follow D4. Internal record and stock writes happen once, through their domain identity.

### R4 — Module lifecycle and external apps
- Each module declares identity, version, `core_api`, dependencies, settings schema, scopes, models and migrations, tasks, UI contributions and tools. Missing, incompatible or cyclic dependencies fail before activation or any write.
- **Disabling a module** blocks its routes, mutations, assistant tools and task claims in every process (web, bot, worker), checked through a module-state generation number. In-flight work is fenced at publication.
- Records, exports and historical assessments stay readable. Disabling never deletes data, and re-enabling never reseeds duplicates.
- Installing code or schema is a release plus restart. Hiding a nav item is never "disable".
- External authors can build a compatible package against a documented contract. A self-hoster can reproduce a custom bundle from its lock file.

### R5 — Neutral farm model
- **Crop seasons.** A parcel keeps a stable identity and gains dated crop seasons. Editing the current season never rewrites earlier records or assessments. Unknown crop or phenology stays unknown.
- **Catalog items and quantities.** Catalog items and typed quantities (mass, volume, area, count and compatible rates) keep the entered value and unit plus provenance. Crop-protection attributes live in the crop-protection extension.
- **Stable meaning.** Existing spray decisions, register exports, stock and source deduplication keep their meaning, proven by frozen semantic golden fixtures. Public representations that change get a versioned adapter.
- **Navigation without crop protection.** Map, home and diary render from enabled module contributions. A parcel stays navigable with crop protection disabled. Unavailable capabilities say so; they never imply "no risk".

### R6 — Proof apps
- **Fertilization (in-tree, optional).** The owner previews, confirms, cancels and corrects a fertilization record on a parcel and season, with the entered units and nutrient-composition provenance kept explicit.
  - Planned work never consumes stock. A confirmed application and its correction deltas change stock exactly once under retry and concurrency.
  - No rate recommendation and no regulatory filing.
- **Threshold alerts (external package).** A separately built package, installed on a bundle without crop protection, notifies when owner-configured frost or heat thresholds are crossed in the forecast.
  - The notice shows timezone, forecast receipt time and missing/stale status.
  - Thresholds are off until configured.
  - It states the forecast condition only, never crop damage or safety.
  - Disabling and re-enabling keeps past notices and creates no duplicates.

## Acceptance criteria

- [ ] R1: two installations from one image run concurrently. Cross-installation HTTP, cookie and database probes are denied, and stopping one leaves the other healthy. Idle cost is measured and documented.
- [ ] R1: a disconnected installation (with revamp02 identities; verified in ticket 17) can bootstrap an owner, manage farms and members, configure modules and confirm records with no hosting service or AI.
- [ ] R2: a backup of A restores into a new C with matching semantic records, sources and module evidence, new secrets, invalid sessions and paused effects. B is unaffected.
- [ ] R2: injected pre-upgrade, migration and post-start failures recover through the documented restore. Concurrent upgrade attempts run one migration.
- [ ] R3: kill, deadline, retry, slow-provider and mode-switch tests show unrelated farms progressing and stale publication rejected. Outbox intents survive a worker restart and never dispatch while paused.
- [ ] R4: an externally built app installs, migrates its own branch, activates, disables and re-enables. Disabled state is honoured by HTTP, tools and worker in separate processes, and incompatible bundles fail before migration.
- [ ] R5: the staged migration preserves legacy spray evidence, exports, stock and source identity, and one parcel carries distinct crop seasons.
- [ ] R6: fertilization passes units, cross-farm, rollback, retry and concurrent-stock tests. Threshold alerts run without crop protection, honestly and repeatably, with source links.
- [ ] Final qualification (ticket 17): full regression, browser, migration, Linux container and the two-installation backup/restore/upgrade drill pass, with exact evidence and honest skips.

## Testing approach

1. **Installation lifecycle:** real CLI plus HTTP against disposable Compose projects. Each test gets server-generated project names and its own Postgres. Never touch an existing checkout or deployment. Covers isolation, backup/restore and upgrade. It does not cover cloud outages or container-escape resistance.
2. **Background work:** real worker processes and separate database sessions, with controlled HTTP/DNS stubs at the provider boundary only. Covers crash, deadline, stale publication, mode switch and outbox. Build on `tests/core/test_jobs.py` and the revamp live-weather contracts.
3. **Domain and modules:** HTTP/CLI against migrated databases, checked against frozen semantic golden fixtures (meaning, not generated IDs). Build on `test_registry.py`, `test_manifest_fields.py`, `test_schema.py` and the operations/inventory/advisor contracts. Never weaken an existing assertion to make a migration pass.
4. **Browser:** sign-in, bootstrap, modules, map/diary and confirm/correct, on desktop and mobile with normal and reduced motion.

TDD per vertical slice: observe a failing public behaviour, then write the minimum to make it green. Mock only external infrastructure and providers.

**Tier 3 is for isolation, auth, data loss, concurrency, migrations and stock.** It means a written failure model, adversarial and property tests, and manual or automated mutants. Other slices (docs, packaging guides, read-only UI) use the normal tier. A skipped layer is reported, never counted as a pass.

## Deferred, with triggers

| Item | Trigger to start |
|---|---|
| Hosted fleet tooling (ticket 18) | First committed hosted customer, or Lucas runs more than two installations himself |
| Hosting manager, customer portal and self-service signup | More than about 10 hosted installations, or manual onboarding becomes a measured bottleneck |
| Payments or merchant adapter | Pricing decided and a portal exists |
| Shared Postgres server (D1 revisit) | Measured cost × planned installations exceeds budget |
| Object storage (D2 revisit) | A feature needs large private media |
| Independently migratable arbitrary module mixes | A real external author needs them |

## Open questions for Lucas (non-blocking)

- Hosting jurisdiction and processor terms: EU data residency and a GDPR data-processing agreement template. This is needed before ticket 18, not before.
- Whether frost alerts are the right external proof app, or a partner has a better candidate.

## Dependencies

- [Milestones 1–4](../farmcore-revamp/spec.md). Member identities (revamp02) are needed before restore and session invalidation can be tested. Integration QA (revamp12) gates core, worker and domain changes, to avoid conflicts over file ownership.
- Linux container test infrastructure and a disposable PostGIS. No production credentials are needed.

## Risks

- **Per-installation cost.** Measure it in ticket 01 before promising prices.
- **Backups contain private farm data.** "Portable" never means "public"; bundles are encrypted to an owner key.
- **Library risk (D3).** The spike must check maintenance health and the exit path; the fallback is the in-house table.
- **Migration risk.** Staged migrations and golden fixtures are mandatory, and a contract step lands only after every consumer has moved.
- **Container isolation** does not protect against a hostile kernel exploit. Hosted code uploads stay excluded.

## Reference rationale

These references supply patterns; no code is copied:
- [Odoo manifests](https://www.odoo.com/documentation/19.0/developer/reference/backend/module.html)
- [Frappe sites](https://docs.frappe.io/framework/user/en/basics/sites)
- [Frappe Press](https://github.com/frappe/press), a model to defer
- [farmOS data model](https://farmos.org/model/)
- [Procrastinate](https://procrastinate.readthedocs.io/)
- [Alembic branches](https://alembic.sqlalchemy.org/en/latest/branches.html)
- [age](https://age-encryption.org/)
- [pint](https://pint.readthedocs.io/)

Sources were inspected on 2026-10-19.
