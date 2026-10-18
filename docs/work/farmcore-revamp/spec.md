---
type: Change Spec
title: FarmCore private, reviewed and actionable farm workflows
status: approved
authorization: User requested milestones 1–4, specification, tickets and autonomous implementation on 2026-10-18.
---

# FarmCore revamp — milestones 1–4

## Problem
FarmCore explains vineyard spray decisions but a network-reachable installation exposes farm reads anonymously. Advice, imported forecast plans and confirmed records are not yet a persistent, reviewed work loop. Farmers need private data, current inputs, accountable agronomic review and a feasible plan they can complete or correct.

## Goals
1. Private farms with authenticated owners, advisors and grant recipients; reliable live weather.
2. Reproducible advice with reviewed parameter versions and separate human recommendation reviews.
3. Persistent spray jobs, resource-aware scheduling and feasible alternatives.
4. Confirmed observations and irrigations, corrections and traceable plan completion.

## Non-goals
Soil trafficability/compaction, irrigation recommendations, new crops, CAP filing, social networks, blockchain, financial savings claims, unrestricted agents, model training/export of private conversations and production deployment are excluded. Recording irrigation is included; calculating irrigation advice is not.

## Vocabulary
- Assessment: deterministic model output from known inputs and a configuration version.
- Parameter review: an expert decision about a named agronomic parameter or configuration version, scoped to crop/region/farm as applicable.
- Recommendation review: a human opinion about one stored assessment; it does not rewrite the model.
- Planned job: intended work, with resource assignments, duration, eligibility and status; it is not a completed log.
- Completed operation: farmer-confirmed fact with provenance and corrections history.
- Scenario: side-effect-free alternative to saved inputs, never an operation.
- Installation administrator: bootstrap/operator identity; distinct from a farm membership.

## Constraints
Keep FastAPI, PostgreSQL/PostGIS, Jinja/HTMX, manifest apps, scoped repository and one linear Alembic chain. Existing rules remain typed Python calculations; editable reviewed parameters become validated versioned data, not executable expressions or a wholesale CSV rule engine. Store shared state transactionally. Keep model optional and calculations outside it. Consent/session expiry uses wall time; assessments use farm clock. Do not copy unlicensed competitor code. Preserve local demo/replay through explicit demo mode; tour is never authorization to private data. No deployment or credential publication.

## Requirements
### M1 — Private access and reliable inputs
- Private access is the safe default. Installation administrator can bootstrap owner accounts locally; no public registration or external email dependency.
- Store local credentials securely (salted password hash), opaque server-side sessions as hashed tokens, logout/revocation, bounded expiry, rate-limited login and CSRF protections on cookie-authenticated mutations. Existing installation admin token is a documented operator/break-glass identity, not a normal advisor login.
- Farm owners can read/write their farms and manage memberships/grants; advisors read assigned farms and submit authorized reviews, never arbitrary operation writes; recipients read only active recipient-bound grant scopes. Installation-level settings remain installation-admin-only. Farm selection, switchers, exports, fragments and assistant tools apply identical farm isolation.
- Anonymous, expired/revoked session and foreign-farm accesses return an authentication/authorization response without data or metadata leaks. Numeric grantee URLs alone confer no access. Unscoped model-call audit is installation-admin-only until explicitly farm-scoped.
- Public demo access requires explicit demo mode and explicitly designated demo farms. A tour cookie never elevates access. Existing non-network development behavior is available only by explicit configuration. Update setup/security documentation accordingly.
- Scheduled weather ingestion stores immutable issued/fetched/available times, source/units, requested location and coverage. Failed/duplicate fetches do not destroy last good data. Future-issued or unavailable runs cannot influence replay; reanalysis and observations are never labelled issued forecasts.
- Freshness and missing inputs are visible, and insufficient inputs produce limited/unavailable assessments rather than a reassuring default.
### M2 — Reproducible and reviewed advice
- Persist validated parameter versions seeded from current sourced/assumed defaults, bounded overrides, author, evidence, review and activation history. Creation, review and activation are explicit; activating a version affects future evaluation only. Scope activation so one farm's override cannot silently affect others.
- Store configuration identity and input provenance with each new assessment. Historical advice remains reproducible and unchanged; legacy assessments retain an explicit legacy-version label.
- Authorized experts approve, modify, reject or mark parameters unsure. An approved parameter is not proof of independently validated model accuracy. Rejected/unsupported inputs are explicitly explained, never silently treated as no risk.
- Owner requests a check on a specific assessment; assigned advisor records an opinion and note. Farmer sees reviewer/time and the distinction between model output and human review. Stale assessment reviews cannot silently carry to new runs.
- Preserve deterministic facts and template fallback. Add a small golden-question set testing unsupported claims, missing inputs and cross-farm requests. Never insert unrelated examples or train from private feedback automatically.
### M3 — Persistent feasible planning
- Create/edit/cancel a planned spray job from stored advice, recording assessment/version, parcels, product, duration/capacity assumptions and assignments. Planned jobs never appear in completed registers or decrement stock.
- Availability is farm-scoped server state; schedules survive refresh, restart and another authorized device. Jobs have revision checks against stale edits.
- Combine existing hourly product/weather eligibility with operator and machine availability. Reject overlapping assignments atomically. A changed forecast or unavailable resource identifies affected jobs without overwriting farmer-confirmed decisions.
- Offer another eligible parcel/job, available time or configured machine when feasible. Do not infer agronomic eligibility from weather alone, invent soil suitability, recommend reduced dose, or present unsupported costs/probabilities. Infeasible jobs stay visible with reasons and unknown inputs.
### M4 — Confirmed records, corrections and completion
- Extend existing confirmed spray workflow to field observations and irrigation facts. Resolve parcel/date/unit ambiguity before preview; each confirmed mutation is farm/member bound and idempotent. For irrigation, retain entered amount/unit and explicitly distinguish gross/net when provided; never assume efficiency silently.
- Correct confirmed facts through a new audited revision; retain original values, reason, actor and timestamps. Cancellation or replayed confirmation changes nothing. Inventory updates follow the confirmed correction delta once; observations/irrigations never consume spray stock.
- Completing a planned job creates/links a confirmed operation in one transaction. Double taps, retries and concurrent completion cannot double-consume stock or create duplicate operations. Canonical source-merging rules still apply.
- Save the assessment, review, plan revision and immutable available weather receipt independently. Actual work may differ from the plan and records show that difference. Re-evaluation runs only after successful commit.

## Acceptance Criteria
- [ ] M1: public/private/demo, farm-member, grant-recipient and admin role matrix passes across pages, exports, fragments and assistant.
- [ ] M1: expiry/revoke/CSRF/foreign selector tests pass; denied accesses expose no farm or global audit details.
- [ ] M1: live input retries preserve good runs; stale/missing data is explicit; replay cannot see future data.
- [ ] M2: one parameter change yields a new scoped version; old assessment evidence remains unchanged.
- [ ] M2: expert review and a request/check/response journey work end to end; parameter review and recommendation review remain distinct.
- [ ] M3: plan persistence, revision conflicts, resource exclusion and alternatives are demonstrated without recording planned work as done.
- [ ] M4: draft/confirm/cancel/correct/complete flows pass retry, rollback and cross-farm tests; stock and advice update once.
- [ ] Full regression, migration, frontend and security verification passes with fresh evidence and independent QA.

## Testing Decisions
Approved public seams selected under the user's explicit autonomous implementation authorization:
1. HTTP requests against the real application and throwaway migrated PostGIS database: authentication, selectors, grants, parameter/recommendation reviews, job planning and confirmation/correction/completion. Catches persistence, permissions, rollback and concurrency; does not establish agronomic accuracy or browser layout. Follow existing tests/core, tests/apps/share, tests/apps/forecast and tests/apps/chat patterns.
2. Existing pure advisor evaluation and proposed pure schedule assessment interfaces: fixed worked examples, missing-data invariants, immutable baseline/scenarios and resource eligibility. Catches numerical/logical behavior; misses live provider quality. Mock only external weather/model/time boundaries, not internal services.
3. Existing job/CLI execution using controlled external fetch responses: ingestion and replay availability; catches source semantics/retries but not provider uptime.
4. Browser end-to-end journeys for login, request review, create/replan/complete job and correct record, backed by the real application. Catches interaction/CSRF and state updates; misses agronomic validity.
TDD is required per vertical slice: observed RED, minimum GREEN, focused verification, refactor during review. Tier-3 verification applies to auth, concurrent mutation and public contracts. Independent QA must review spec conformance and implementation standards separately before integration.

## Review disposition — Claude comparison
Accept expert review, explicit missing-input reasons, parameter versioning, deterministic explanation/fallback and evaluation fixtures. Adapt CSV/YAML suggestion: current complex disease computations remain typed; store reviewed parameters with bounds and provenance. FarmCore already persists pending logs and clarification context; extend, do not replace. Existing BSV replay is not field accuracy evidence; regional symptoms and station infection timing are different quantities. New statistics require appropriate independent labels, not invented accuracy. Defer feedback-to-fine-tuning, LoRA/export_sft, 10-km neighbour alerts and extra UI stacks. Claims about winners' judging motivations are inference; code/test counts in the pasted review are historical and not acceptance criteria.

## Risks
Auth changes intentionally alter anonymous access; explicit demo fixtures preserve public showcase coverage. Cross-farm leaks can bypass scoped helpers through manual queries: QA enumerates these. Migrations must be serialized onto one chain. Calendar/rain/units and midnight boundaries need worked examples. Parameter review cannot substitute for farmer/agronomist field validation. Concurrent corrections and inventory require real transactional tests. No promise of disease reduction or savings from synthetic/replay results.

## Dependencies
Local PostGIS test server; authenticated GitHub for PRs; user-approved milestones 1–4. External weather calls isolated/cached and credentials never committed.

## Unresolved Items
None blocking initial implementation. External provider production commercial terms, agronomist recruitment and production cutover remain later operational decisions; implementers document assumptions rather than invent external integrations.
