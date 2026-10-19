---
type: Change Spec
title: "Isolated installations and modular farm workflows"
status: approved
authorization: "Lucas approved isolated hosted/self-hosted instances and modular architecture, and requested specification and implementation tickets on 2026-10-19."
---

# Isolated installations and modular farm workflows

## Problem
FarmCore can explain and record vineyard spray decisions, but independently hosted customers need separate data, configuration and upgrades. Self-hosters need the same usable product without a hosting-service dependency. Existing manifest apps provide a useful foundation; shared product, asset and assessment restrictions still encode a particular fungicide workflow. Expanding these restrictions ad hoc would couple unrelated agricultural capabilities.

## Goals
- One open-source runtime and reproducible module bundles for hosted and self-hosted installations.
- Isolated, independently configurable customer installations with recoverable provisioning, upgrades and portability.
- A separate optional hosting manager, with customer administration distinct from farm membership and operator access.
- Reliable background execution and explicit module lifecycle behavior.
- A reusable farm model and parcel workspace, proven by independent weather-risk and fertilization-record apps.

## Non-goals
Production deployment, accepting payments, choosing a merchant or cloud vendor, automatic deletion of customer data, unrestricted hosted code uploads, Kubernetes, framework replacement, a service for every app, training on private farm data, pump/machinery control and new irrigation/pest/drought/nutrient recommendation algorithms. Existing milestones 1–4 remain a separate approved initiative; this spec does not mark them complete or replace their QA. Irrigation facts already in that work are preserved, not rebuilt as a duplicate capture system.

## Vocabulary
Use the [domain glossary](../../../CONTEXT.md). A Customer can own more than one Installation; an Installation contains Farms. Hosted and self-hosted describe operating responsibility, not feature editions. An installed Module is available to an Installation; farm-specific configuration and user permissions determine its use. A Planned job is agricultural work; a Background task is software execution. A confirmed Record is a fact; an Assessment is a typed module result, not evidence of independent agronomic accuracy.

## Constraints
Retain FastAPI, PostgreSQL/PostGIS, typed Python agronomic calculations, manifest composition and the existing confirmation/provenance model. Keep one supported linear migration chain per runtime release bundle; only the integration coordinator authors and serializes revisions. The separate hosting manager has its own control database and independently managed migration history; it must not introduce competing heads into the runtime chain or populate the control database with farm tables. This clarifies how the existing single-runtime-chain rule applies to a new separately deployed system. Independently migratable arbitrary runtime module mixes are deferred. Customer customization is validated configuration or a reproducible reviewed bundle, not an untracked customer branch.

The implementation remains in the repository-local work graph. The accepted architecture note in the vault contains durable rationale, not duplicated tickets or task status. Current access and weather PRs keep their existing review/CI gates. Core model, worker and module changes wait for the [current revamp integration QA](../farmcore-revamp/tickets/12-integration-qa.md); portable packaging and backup work may proceed before the full revamp once their listed privacy/identity prerequisites pass, on an isolated branch. No existing checkout or deployed installation is repurposed as a test tenant.

## Requirements

### I1 — One runtime, independent installations
Each Installation has a stable identity, application runtime, database authority, private file namespace, secrets, module profile, operational clock context and pinned release. One installation may contain several farms with normal farm/member/grant isolation. Hosted installations may share physical infrastructure; a customer runtime cannot connect to another installation's database, read its files or affect its configuration. Initially use distinct deployment projects with private networks/volumes and explicit resource limits. A shared database server is acceptable only with separate databases and least-privilege roles; a shared application process or shared tenant tables is not the initial model.

The same image and module bundle runs self-hosted without contacting a hosting manager, subscription endpoint or mandatory AI service. Configuration supplies location, units, timezone, farms, connectors and optional apps. Demo data is opt-in; hosted demos are separate installations. Base URLs, cookies, caches, worker leases and object/file keys must be installation-scoped. Runtime database credentials cannot provision databases or access host infrastructure.

### I2 — Hosting manager and customer administration
An optional separate hosting-manager deployment owns its own identity store, operational metadata and narrowly privileged provisioning channel. Its customer portal cannot execute arbitrary host commands or browse farm records. A privileged lifecycle agent may create databases, take backups and manage deployments; these are explicit audited infrastructure operations, not a claim that the machine operator cannot access hosted data.

An authorized operator can allocate a customer installation through a public management command. Provisioning is idempotent and durable: retries and crashes produce one installation, with visible provisioning/ready/failed states and recoverable cleanup of newly allocated resources only. Untrusted names, domains or paths cannot select another installation's resources or become executable command text. Domain mappings use an allowlisted installation mapping and verified ownership before custom-domain activation.

The customer receives an expiring one-use owner bootstrap and can configure their installation, members, approved modules and connectors. A hosting Customer, an Instance administrator, a Farm owner/advisor/recipient and the installation Operator are different authorities. Instance administration does not silently confer farm-record access; normal access still uses memberships/grants. Support/break-glass access is explicit, installation-specific and audited. Self-hosters bootstrap locally with the same runtime identity mechanisms. Initial hosted allocation is operator-approved; merchant checkout is a later adapter and must not be represented as implemented payment processing.

### I3 — Backup, restore, upgrades and suspension
A portable recovery bundle includes database data, private files, a release/module manifest and integrity checks at one consistent recovery point. Quiesce writes or provide an equivalent consistent checkpoint/immutable-file snapshot; matching independent hashes alone is insufficient. Restored database references must resolve to the corresponding file versions under concurrent writes. The bundle is access-controlled and encrypted in storage/transfer. Platform-wide credentials and operator secrets are not exported to the customer; connector secrets use a documented per-installation recovery/re-entry procedure. Restoring to a new installation preserves record/provenance identity, rotates installation secrets and invalidates sessions; it cannot retain the source's domain binding, scheduled deliveries or hosting-agent authority. Activating a restored clone requires an explicit owner-controlled transition so two copies do not send duplicate external effects.

Before an upgrade, verify compatibility, backup and restore readiness; run one designated migrator and prevent concurrent app/worker migrations. Publish health and failure states. Reverting an image is not a database rollback: incompatible schema failures recover through a tested restore path. One failed upgrade/restore leaves other installations available.

Suspension stops customer work and scheduled side effects while retaining protected recovery data. Resume is idempotent and respects current module/clock state. Retirement is reversible archive/export in this scope; permanent data deletion and automatic deletion for nonpayment are excluded. Retention and recovery ownership are visible to the customer.

### I4 — Durable, bounded background execution
Persist background tasks per installation/farm/job, with claim leases, attempt identity, retry state, bounded deadlines and cancellation/mode-generation fencing. A crashed worker can be replaced; expired or disabled attempts cannot later commit stale results. Retried confirmed writes remain idempotent. Network work does not hold an installation-wide transaction or global job lock. A stalled provider for one farm/job cannot prevent unrelated eligible work from being claimed; bounded DNS residue and provider saturation remain visible failures, never silent success.

Preserve replay's as-of inputs, deterministic ordering and farm restrictions. A switch between live and replay invalidates obsolete attempts before publication. Foreground request handlers can enqueue durable work but do not run an unbounded live fetch.

Record committed external side effects in the same database transaction as their cause, then dispatch with retry and deduplication. Internal operation/stock writes occur once through their domain identity. External delivery uses provider idempotency/receipts where supported; ambiguous delivery is visible for reconciliation rather than advertised as exactly-once transport.

### I5 — Versioned module profiles and lifecycle
Each reviewed module declares stable identity/version, supported core API, dependencies, validated settings scope, permissions, models/migrations, background tasks, UI/map contributions and tools. The release manifest pins compatible module code; an installation records its selected profile and revision. Missing/incompatible/cyclic dependencies fail before activation or business writes.

Installation, activation and disablement are explicit. Acknowledged disablement blocks new entry points, mutations, assistant tools and task claims, including another running process; in-flight work is fenced/rechecked before publishing. Records, source evidence, exports and historical assessments remain readable through authorized retention/compatibility paths. Disabling is not data deletion. Re-enable applies a supported profile without reseeding duplicate records. Changes to installed code/schema use a controlled release/restart; activation configuration must not pretend that hiding a navigation item disables execution.

Third-party authors can publish separately packaged modules through a documented discovery/contract interface. Hosted customers choose reviewed bundles; module permissions are application policy, not a sandbox for arbitrary Python. Self-hosters may build compatible custom bundles. Local extensions are reproducible and carry their own tests/licensing metadata. Secrets never appear in manifest examples.

### I6 — Reusable farm model with compatible migration
Keep farm/parcel identity stable and add explicit crop seasons so a parcel's historical crop context does not change when the next season is configured. Missing crop/phenology information stays unknown. Observed facts, planned work and computed assessments remain distinct.

A generic product and quantity representation supports typed mass, volume, area and compatible rate values, original units and provenance. Crop-protection attributes such as contact/penetrant behavior and downy/powdery targets belong to that module's typed extension. Each app owns its domain payload, validation, merge policy and assessment decisions; do not replace them with unrestricted JSON or a generic executable rule language.

Use expand–migrate–contract. First add compatible catalog/quantity/assessment forms alongside existing data. Migrate records/import/capture, inventory and crop-protection consumers in separately verifiable groups. Remove shared fungicide restrictions only after all internal consumers migrate and compatibility tests prove historical records, parameter versions, reviewed assessments, register exports and source deduplication unchanged. Existing spray decisions retain their meaning; they are not renamed into irrigation or nutrient decisions. Stable public representations retain an explicit versioned adapter/deprecation policy.

The parcel map, home page and record diary use enabled module contributions. A field remains navigable without crop-protection apps; optional agronomic apps cannot be imported as mandatory core dependencies. Empty/unknown states explain unavailable capabilities rather than showing a safe/no-risk conclusion.

### I7 — Independent proof apps and broader workflows
Prove the module contract with a separately packaged weather-risk app installed without crop protection. It shows source-linked hourly temperature threshold crossings for owner-configured heat/cold thresholds, with timezone, forecast receipt time and missing/stale status. Defaults are disabled until configured. The notice states the observed forecast condition; it does not claim crop damage probability, agronomic safety or a drought model. Disable/re-enable preserves existing notices without duplicate tasks or disclosures.

Deliver a fertilization-record app using shared parcel/season/catalog/quantity and confirmation/correction infrastructure. The owner can preview, confirm, cancel and correct a record; entered units and nutrient composition provenance remain explicit. Compatible dimensional conversions are deterministic; absent composition/area/density cannot be invented. Planned work does not consume inventory. Confirmed stock-bearing applications and audited correction deltas affect inventory once, including retries and concurrent requests. No nutrient-rate recommendation or regulatory filing is included.

Existing irrigation facts and field observations remain usable. Future insect scouting, water balance, drought and nutrient recommendations have their own bounded Change Specs and field-validation requirements; they do not silently inherit mildew rules.

## Acceptance Criteria
- [ ] I1: two installations from the same image/profile can run simultaneously with distinct identities/data/files/secrets; cross-installation HTTP, cookie, database and file tests deny access.
- [ ] I1: a disconnected self-hosted installation can administer users/farms, configure modules and confirm records without the hosting manager or AI.
- [ ] I2: repeated/crashed provisioning allocates one recoverable installation; owner bootstrap expires/is single use; portal/customer/admin/farm/operator authority tests pass.
- [ ] I2: hostile domain/name/path inputs cannot route to another installation, escape allocated resources or execute commands.
- [ ] I3: backup from hosted A restores into independent self-hosted C with matching record/source/module evidence, invalid sessions/new secrets, and no unsolicited external effects; B remains unaffected.
- [ ] I3: migration/upgrade failure and suspend/resume recover as documented without duplicate tasks or data loss; retirement retains protected recovery data.
- [ ] I4: kill/lease-expiry/retry/slow-provider/mode-switch tests prove prompt unrelated progress and reject stale publication; committed effects are dispatchable after worker restart.
- [ ] I5: an external reviewed app can install/activate/disable/re-enable through supported contracts; direct routes/tools/tasks observe disabled state across processes; incompatible profiles fail safely.
- [ ] I6: staged migration preserves legacy spray evidence/exports/stock/source identity and a stable parcel can carry distinct crop seasons.
- [ ] I7: weather-risk runs without crop protection; threshold/missing/stale/replay behavior is honest, repeatable and source-linked.
- [ ] I7: fertilization preview/cancel/confirm/correct passes units, cross-farm, rollback, retry and concurrent stock tests without nutrient recommendations.
- [ ] Final independent spec/standards review, full regression, browser, migration, Linux container and two-installation recovery checks pass with exact evidence and explicit skips.

## Testing Decisions
1. Highest public seam: real management CLI plus HTTP against disposable Linux Compose installations and migrated PostGIS databases. Exercise provisioning/retry/bootstrap, roles, configuration, two-installation isolation, backup/restore/upgrade and self-host independence. Catch persisted contracts and operational resource separation; miss real cloud outages, billing and untrusted-container escape resistance. Existing deployment, auth, schema and registry tests provide prior art; add an installation fixture using server-allocated disposable project names, never deployed clones.
2. Existing job execution/worker command against real separate database sessions and controlled external HTTP/DNS/provider boundaries: stalled calls, crash/lease recovery, stale publication, replay mode and outbox failure. Follow tests/core/test_jobs.py, clock tests and the revamp live-weather public-job contracts after integration. Catch transaction/concurrency/liveness behavior; miss provider uptime.
3. Real application HTTP/CLI and reviewed package discovery against migrated databases: module lifecycle, compatibility, legacy data, pure unit validation and domain workflows. Follow tests/core/test_registry.py, test_manifest_fields.py, test_schema.py and operations/inventory/advisor public contracts. Compare frozen semantic golden evidence, not generated IDs or implementation details; do not weaken existing assertions to migrate.
4. Browser journeys against the real app for public sign-in, bootstrap, configure modules, parcel map/diary, confirm/correct and hosted/self-hosted parity. Cover desktop/mobile and normal/reduced motion without masking console errors or private polling. These checks do not prove agronomic accuracy.

TDD: one observed failing public behavior before minimum GREEN per vertical slice. Mock only external infrastructure/provider/payment boundaries, not internal collaborators. Tier 3 applies to isolation, auth, data loss, concurrency, migrations and public contracts: written failure model, property tests, adversarial tests, actual execution and mutation/manual mutants. Skipped layers are not passes. Focused gates use the real merge base; final full regression and dataset/replay checks remain required. Preserve the documented ordered lifecycle test exception while randomizing ordinary tests. Specs/docs alone do not require product tests.

## Dependencies
[Current milestones 1–4 spec](../farmcore-revamp/spec.md), especially member identities/grants, reviewed assessment evidence, confirmed correction/completion contracts and final integration QA. Linux container test infrastructure and disposable PostGIS; no production cloud credentials needed for local lifecycle tests. Payment provider, live commercial weather entitlement and production hosting cutover require later operational decisions.

## Risks
Isolation increases per-customer resource cost; bound allocations and measure before sharing runtimes. The hosting lifecycle agent is privileged and needs a narrow audited interface. Backups contain private data; portable does not mean public. Addon compatibility and data migration can break historical records; staged adapters and semantic golden fixtures are mandatory. A module state change can race workers; transaction fencing must apply at publication, not only task start. Generic quantities cannot silently infer agronomic meaning. Container/process isolation is not a guarantee against a hostile kernel exploit; unrestricted hosted custom code is excluded.

## Unresolved Items
No blocker to packaging, recovery or contract work. Lucas owns later merchant-provider selection, pricing/retention policy and production hosting budget/capacity. New scientific recommendation models require a separate approved scope and appropriate input/field-validation evidence. These decisions do not authorize payment processing, actual customer provisioning or deployment now.

## Reference rationale
Patterns, not copied implementation: [Odoo manifests](https://www.odoo.com/documentation/19.0/it/developer/reference/addons/module.html), [Frappe sites](https://docs.frappe.io/framework/user/en/basics/sites), [Press hosting manager](https://github.com/frappe/press), [farmOS model](https://farmos.org/model/), [OpenAgri deployment](https://github.com/agstack/OpenAgri-Bootstrap-Deployment). Sources inspected 2026-10-19. Retain the current stack rather than migrate frameworks; share physical infrastructure while keeping customer runtimes independent. The milestone-1–4 non-goals remain binding for that initiative; this separate subsequent spec authorizes the listed architecture/recording extensions, not new agronomic models.
