# FarmCore domain language

FarmCore organizes farm records and module-owned assessments. Hosting responsibility is distinct from farm-data authority.

## Installations and people

**Customer**: A person or organization that owns one or more hosted installations. A customer relationship does not itself authorize reading farm records.
_Avoid_: Farm, tenant account.

**Installation**: One independently operated FarmCore deployment with its own identity, data and configuration. It can contain several farms.
_Avoid_: Farm, workspace when meaning a deployment.

**Hosted installation**: An installation operated on a customer's behalf by a hosting provider.

**Self-hosted installation**: An installation operated by its owner or chosen operator, independently of the hosting manager.

**Hosting manager**: A possible future system with its own customer portal and control data. Deferred; hosted installations start as operator fleet tooling.

**Fleet inventory**: The operator's declarative list of hosted installations (release, domain, limits, state) that fleet tooling converges to.

**Instance administrator**: A person authorized to configure an installation and its approved modules/users. Farm-record authority still follows memberships or explicit operator access.

**Operator**: The installation's privileged operational identity for bootstrap and break-glass administration. It is distinct from an ordinary farm member.

**Farm**: The unit of agricultural records, parcels and farm-scoped access.

**Farm member**: An authenticated person with an explicit role on a farm.

**Grant recipient**: An authenticated person allowed the active scopes of a recipient-bound data-sharing grant.

## Farm work and evidence

**Parcel**: A stable geographical field identity that can retain history across crop seasons.

**Crop season**: A dated crop context for a parcel; changing the current crop does not rewrite prior seasons.

**Record**: A confirmed agricultural fact with its quantities, actor and source provenance.

**Planned job**: Intended agricultural work with its own revision and assignments; it is not a completed record.
_Avoid_: Background task.

**Background task**: Durable software execution such as fetching weather or evaluating advice.
_Avoid_: Planned job.

**Assessment**: A module's typed computed result from identified inputs/configuration; it can be limited or unavailable.

**Parameter review**: An expert opinion about a named parameter/configuration version and its evidence.

**Recommendation review**: An authorized human opinion on a particular stored assessment.

**Forecast receipt**: A retained provider response whose availability and fetch times are known; its model issue time may be unknown.

## Modules

**Module**: An installable FarmCore capability with declared compatibility, dependencies, settings and permissions.

**Module profile**: The selected compatible module set and configuration revision of an installation.

**Release bundle**: A reproducible runtime and compatible module versions distributed together.

**Backup bundle**: One encrypted file holding an installation's database dump and a manifest of its identity, release and modules; restoring it creates a new installation.

**External effects**: Anything an installation sends outside itself (notifications, messages, deliveries). An installation-level switch can pause them; restored copies start paused.
