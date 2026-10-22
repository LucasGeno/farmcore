---
type: Work Ticket
title: "Versioned agronomic parameters are reviewable"
description: "Milestones 1–4: versioned agronomic parameters are reviewable."
status: in_progress
blocked_by: ["02"]
---

# 05 — Versioned agronomic parameters are reviewable

**Source:** [Approved Change Spec](../spec.md)

## Outcome and scope

Versioned agronomic parameters are reviewable. Touchpoints: advisor config/evaluation and new review app; app manifest; migration; evaluation snapshots. Implement this outcome end to end; other tickets and spec non-goals are out of scope. Keep a single linear migration chain. Implementation workers propose schema changes; only the designated coordinator authors/serializes revision files. During access tickets 01–03, the access chat coordinator owns revisions exclusively; other chats request schema changes from the integration coordinator.

## Blocked by

- [02](./02-member-sessions.md)

## Acceptance criteria

- [x] Seed current parameters unchanged with source/assumed status and bounds; no generic executable CSV rule engine.
- [x] Expert proposes/reviews/activates version with farm/crop/region scope and immutable history.
- [x] New advice stores effective version and input provenance; old advice remains unchanged and legacy is labelled.
- [x] Missing inputs and rejected/not-evaluable states are explicit; reviewer approval never claims field accuracy.

## Execution discipline

Use tdd through the spec's approved public seams. Observe one expected RED before minimum GREEN per behavior, then refactor during review. Do not mock internal collaborators or weaken tests. Review and verify before done. Record evidence below, not in a second tracker. User authorizes autonomous implementation, branch commits, PR creation, QA and safe integration; no repeated approval question. Never deploy or force-push main.

## Verification

- [x] `uv run pytest tests/apps/advisor tests/apps/kpi tests/core -m "not data"` — focused relevant regression passes. Adapt test directories only when real repo names differ; record exact commands.
- [x] `uv run ruff check src tests` and `uv run ruff format --check src tests` — pass.
- [x] `uv run alembic heads` — one head after integration.
- [x] Auth/concurrency/public contracts: verify tier 3 with written failure model, adversarial cases and mutation or documented manual mutant fallback. Other behavior: tier 2, plus ticket-specific checks.
- [ ] Fresh reviewer checks every acceptance criterion; no done claim on missing/skipped evidence.

## Evidence

Implementation started against verified frozen ticket 02 commit
`563b11ffffcc910a707793b7b4c8fedbcb2961aa`. The integration coordinator subsequently
cleared ticket 02 after exact CI and post-merge verification of merge `dca8477`;
source/tests/dependencies match the frozen commit used here. Ticket 06 remains
paused until ticket 05's integration disposition.

Schema-only handoff `993687c` defines the review app's parameter version, review
and activation models. The integration coordinator exclusively authors the
serialized migration; no revision is authored by this stream. Historical Advice
rows receive no backfill. A baseline is explicitly unreviewed, and review approval
is a human opinion rather than evidence of field accuracy.

Policy-only handoff `713b7f1` introduces `parameters:read` and restricts the three
parameter tables to that scope. It intentionally excludes unfinished routes and
services. Advice-only consent must never expose raw parameter history, full
configuration/input snapshots or freeform review evidence. Generic review
identity is limited to ID/verdict/status; assigned owner/advisor review views
retain reviewer/time/note. Runtime and public consent verification remain pending.

Working TDD observations (not final verification):

- Untouched engine baseline: `271 passed, 5 deselected in 1.02s`.
- Empty-input engine RED: missing `Advice.assessment_status`; minimum GREEN:
  `1 passed in 0.05s`.
- Null rain, null temperature, stopped feed and incomplete powdery history RED:
  `4 failed, 1 passed, 64 deselected in 0.10s`; focused engine GREEN:
  `277 passed, 5 deselected in 0.70s`.
- Catalog HTTP RED re-observed with a valid farm and live owner grant: 404 instead
  of 200; minimum GREEN: `1 passed in 0.87s`.
- Proposal/review/activation public journey RED: proposal returned 404 instead
  of 201 (`1 failed in 1.22s`). Lifecycle subsequently passed against the serialized DDL; see current working evidence below.
- Empty station/persisted assessment and detached-station current-view HTTP RED:
  missing assessment status (`2 failed in 1.41s`). Detached-station guard then
  passed its focused regression; empty-station evaluation was subsequently exercised against the serialized DDL.

Serialized foundation `e89dcef248bd82f11ab5e2cddc0058cf93732599` is now consumed;
its sole Alembic head is `review_0001`. Root verified the foundation independently;
those results do not replace this feature's verification.

Further working evidence (not a frozen-source gate):

- Version lifecycle plus engine/runtime cases: `65 passed in 8.46s`; a 12 mm
  override changes future assessments only, and later rejection limits future
  assessments without changing saved configurations or reasons.
- Real first-evaluation versus pending owner HTTP mutation: removing the new job
  locks yielded `1 failed in 3.29s` (job committed while mutation pending); exact
  source restoration yielded `1 passed in 3.30s`.
- Relevant missing-input controls include older contact rain-cover gaps, expired
  contact/valid rainfast penetrant controls, limited incubation without precise
  projections, nonfinite inputs, duplicate hours and future-row independence.
  Engine working run: `290 passed, 5 deselected in 3.69s`.
- Broad collection completed before fixes: `79 failed, 1583 passed, 144 deselected
  in 310.67s`. Failures are being classified; renderer fixtures require actual
  supporting station histories, while shared forecast missing-station controls
  remain unchanged. Independent diagnosis found a real calendar coupling:
  agronomy missingness incorrectly hid the after-hours `passed` state. Weather
  grading itself passed five public controls.
- A real migrated-database projection probe measured 4,020,864 bytes of full
  private props versus 1,455 bytes of current props, with median SQL around
  70 ms versus 2 ms. Public responses remained identical. Total HTTP timings
  did not establish a speedup; only the SQL transfer/decode reduction is claimed.

Working RED/GREEN notes were kept outside the repository during implementation.

### First frozen-source review

Feature source/tests/scripts/config: 609 files, digest
`5407a2cf6125c818cb1fef8666ba707d21e2124083d899e676d3b273946b30e5`,
HEAD shared prerequisite `8e51c6505436ab0a686c8f04dfb325ebab6759b0` plus feature WIP.
Both fresh, independent read-only reviews collected all findings before fixes:

- Spec P2: duplicate weather hour can mask a missing older contact-cover rain hour,
  returning evaluable/protected at the pure engine seam under unchanged defaults.
  Station loading deduplicates, so no HTTP exploit is claimed.
- Standards P2: limited incubation suppresses its expected deadline, but priority
  still asserts before-alert/window-before-end with no known window. Real
  job-to-HTTP RED: `1 failed in 4.53s`; restoring only missing temperature
  establishes the complete-data control.
- Independent real migrated job/HTTP positive-risk control: `1 passed in 3.62s`;
  known positive risk remains visible under partial inputs. No extra concealment
  finding survived the controls.

Root's bounded priority disposition: preserve elapsed-supported severity and
explicit unknown timing/window; existing Urgent remains only when the observed
lower bound already proves alert passed. Missingness creates no invented deadline
or new scientific/severity rule. Full-data worked controls remain unchanged.

Visible Chromium, real disposable migrated database: owner invalid bounds →
unreviewed 12 mm proposal → advisor opinion/activation through CSRF forms →
foreign-farm denial. Eight recorded checks passed; 1440/375 px with normal/reduced
motion had no horizontal overflow or JavaScript errors. Desktop/mobile screenshots
inspected. Own server stopped and database dropped. Refresh after final code edits
before treating this as final evidence.

Composition follow-up after ticket 03 runtime inheritance: exercise a nonempty actual
grant export for old props-less advice. Its shared serializer must preserve explicit
legacy assessment/version labels produced by `advice_json`, without raw input,
configuration or current private facts. Current foundation does not include that
recipient runtime; existing operator export probes are not recipient authorization
proof. Coordinate the bounded serializer change with ticket 03 if an actual HTTP
RED establishes the gap.

Both findings were fixed by the original implementer before handover; their
disposition is recorded in the completion section below.

### Tier 3 failure model

Written before final verification; every mode needs evidence or an explicit limit:

- Cross-farm identifiers, stale membership, disabled identities and missing CSRF:
  actual HTTP role/consent matrix, live authorization under the farm transaction
  lock, explicit resource policies, positive owner/advisor controls.
- Advice-only grants exposing private parameter history or embedded raw weather:
  safe serializers, distinct `parameters:read`, denied raw queries/audit checks,
  and positive separately consented weather controls with ticket 03.
- Concurrent first evaluation, activation and owner mutation: separate committed
  sessions at the actual job and HTTP seams, canonical advisory → farm-row locks,
  consistent effective version within one farm evaluation and unchanged other farms.
- Invalid keys/types, booleans-as-numbers, non-finite values, huge integers,
  date/year/order conflicts and unsupported scope: public POST validation plus
  generated boundary cases; denied inputs must leave no partial versions/events.
- Edited history or later activation changing old advice: append-only PostgreSQL
  enforcement, exact before/after saved snapshots, explicit legacy labels, and an
  exact historical assessment read distinct from a transient current projection.
- Missing/null/stopped/duplicate-hour weather, incomplete powdery history and gaps
  earlier in a spray's rain-cover period: pure engine worked controls/properties
  and persisted HTTP, assistant, map, Today, planning and notification consumers.
  Known positive infection risk survives; unsupported absence/protection/incubation
  does not. Availability follows each output's actual input dependencies.
- Incomplete provenance or precision loss: independently asserted station/hourly
  rows and nulls, source semantics, selected spray/quantity/source identities,
  exact numeric evidence, clocks/timezones and full typed configuration snapshots.
- An override borrowing its baseline citation, or approval implying accuracy:
  unchanged baseline metadata, separate modified-value evidence, explicit human
  opinion/reviewer/time and no field-accuracy claim.
- Broken reset/replay or inaccessible expert forms: existing bounded demo reset
  regressions and real desktop/mobile browser journeys including reduced motion.

Final QA freezes production source, collects Spec and Standards findings before
fixes, and verifies exact restoration after adversarial/manual mutant controls.
Changed-line coverage uses the actual merge base and remains 100%; missing tools
are reported as skips with the required fallback, never upgraded to passes.

### Completion handover — 2026-10-21

Rebuilt on `codex/farmcore-revamp` at `69d0ef6` plus the prerequisite pinned-CSS
commit, as branch `feat/reviewed-parameters`. No Alembic revision was authored; the
single head remains `review_0001`.

Open findings from the first frozen-source review, both fixed before handover and
re-checked by restoring the defect (manual mutant, then exact restoration):

- Spec P2, duplicate hour masking a missing contact-cover rain hour: `rain_since`
  counts only unambiguous on-grid hours. Restoring a row-count check fails
  `test_unknown_contact_cover_rain_cannot_guarantee_protection`.
- Standards P2, limited incubation implying a known window: priority states unknown
  stage/window timing under limited coverage and keeps Urgent only when the observed
  lower bound already reached the alert stage. Removing that branch fails
  `test_limited_incubation_priority_never_invents_stage_or_window_timing`.

Ticket 04 handover (historical observations): a farm with no station, an empty
station, or an insufficient series gets an explicitly unavailable/limited assessment
and headline, never a reassuring default. Forcing current availability to
`evaluable` fails `test_consumers_show_saved_advice_as_unavailable_without_station_readings`;
removing the review membership check fails the review authorization tests.

Broad-run failures: the 79 failures recorded during implementation were resolved
before handover (renderer fixtures given real station histories; one real bug, the
after-hours calendar `passed` state hidden by agronomy missingness, fixed in
planning). The rebuilt branch's first default run had 4 failures, all classified as
tests to update for the intended change (fixtures without a station now show the
unavailable view): three French assistant tool cases and the crop-protection
production-section check. They now use complete synthetic readings. The full run
(`--all`) then failed 6 data-marked tests: five assistant-tool contract tests, whose
literal fact allowlist rejected the new version facts (a real over-exposure, fixed by
sending only the version id and review status), and one real-parcels layer fixture
without a station (test update). No pre-existing failures.

Tests: +34 added, 3 changed, 0 deleted from the base (the handover had +59; 25
layered copies were merged into tables or deleted). Net growth over 8 is deliberate:
four acceptance criteria, the tier 3 auth/consent/concurrency failure model, and
eight consumers (Today, calm view, farm map, map layer, fields layer/list, plan,
assistant) each of which previously rendered a reassuring default.

- Engine missingness (9, `tests/apps/advisor/engine/test_invariants.py`): empty,
  incomplete, duplicated and stopped series; powdery history; positive risk kept;
  old gaps limiting only their own reason; contact-cover rain gaps and controls.
- Persisted job/HTTP (10, `tests/apps/advisor/test_review_evidence.py`,
  `test_models.py`): unavailable job, legacy label, exact input snapshot with rejected
  raw values, exact historical read, bounded identifiers, one version per farm run,
  limited priority, assistant and consumer tables.
- Review app (15, `tests/apps/review/`): unreviewed sourced catalog, lifecycle with
  future-only activation, rejected inputs write nothing, cross-farm isolation, active
  parameters used by About/plan, forms with CSRF, scope change, member/anonymous/demo
  authorization, consent scopes, and three real-transaction concurrency cases.

Known limits: shared principal resolution (ticket 02) owns disabled-identity
rejection; no committed browser test (the earlier Chromium journey predates the
final edits and was not repeated); the per-row input snapshot stores the full
season's observations, so advice-row size grows with the season; current
availability runs one engine evaluation per read.

Status stays `in_progress` until the PR is reviewed by a fresh reviewer and merged.

Final runs after the last edit:

- Focused advisor/review/forecast/notifications/tour: 768 passed, 119 deselected
  (before pruning; pruned files re-run green).
- `scripts/check.sh` (before the last two fixes): ruff lint and format clean; **2760 passed, 3 skipped, 281
  deselected in 1069.25s**. Skips: optional SIGPAC/InfoRiego data.
- `scripts/check.sh --all` (before the last two fixes): **6 failed, 3013 passed, 25
  skipped in 2938.48s**; all skips are optional SIGPAC/InfoRiego data. The six were
  fixed and their files re-run with `-m ''`: advisor tools, assistant and evidence
  **214 passed**; fields layer **8 passed**. Affected areas re-run with the default
  selection after the fix: **956 passed, 3 skipped, 193 deselected**.
- Node: 32 passed. `uv run alembic heads`: `review_0001 (head)`.


### Rebase and storage measurement — 2026-10-22

Rebased onto `main` at `fba0749`; the pinned CSS took main's copy and was rebuilt with
`scripts/build_css.sh` (38382 bytes, digest in the vendor README). `latest_advice` and
current availability now use the configured display timezone. `scripts/check.sh` on the
rebased head: ruff clean; **2773 passed, 3 skipped, 281 deselected in 497.24s** (skips:
optional SIGPAC/InfoRiego data).

Scene B seeded on a fresh migrated database and replayed daily at 06:00 from 10 Apr to
8 Jul 2024 (90 steps, 900 advice rows, 10 parcels): stored `props` average 128,998
bytes per row (max 190,080; 1.06 MB average as JSON text, of which the full
observation rows are 0.84 MB and the hourly series 0.20 MB). The advice table is
120 MB after the season, almost all TOAST; the same rows without the evidence
average 568 bytes. The live job evaluates hourly, so one 10-parcel farm would store
about 2.8 GB per season.

Measured alternatives on the same rows, no migration needed for either:

- Drop the duplicated full observation rows and the per-row configuration (the
  immutable version row already holds it): 33,184 bytes per row, 31 MB per season.
  Keeps the exact hourly series the engine read.
- Keep references only: station, evaluated window, observation count and highest id,
  a SHA-256 digest of the hourly series, the rejected or missing hours, and the
  spray logs, quantities and sources; configuration by version id and hash: 1,977
  bytes per row, 2.6 MB per season. Observations can be upserted later, so a digest
  detects a changed input but cannot rebuild it.

Storing one series per farm run instead of per parcel needs a new table, so a core
migration.

### Advice storage: option A implemented — 2026-10-22

Each new advice row keeps the exact hourly series the engine read (`hourly_inputs`,
rejected values kept raw) and names its immutable parameter version by id and hash.
Station observation rows and the configuration copy are no longer stored per row.
Rows saved in the earlier shape keep reading their own configuration copy; the exact
assessment read resolves the version row otherwise. No migration.
`test_stored_series_and_version_reproduce_the_assessment` re-evaluates a saved row from
its stored series and version (hash checked) after a later station correction and
gets the same decision and reasons.

Re-measured on a fresh scene B database, replayed daily 10–14 Apr plus one step at
8 Jul: stored `props` 16,732–18,093 bytes per row in April (previously about 70 KB)
and 49,247 bytes at season end (previously up to 190 KB). That is roughly 30 MB per
10-parcel season at one evaluation a day, and about 0.7 GB at the hourly live cadence.

Follow-ups, not in this ticket:

- Option B: keep references only (station, evaluated window, observation count and
  highest id, a SHA-256 digest of the series, rejected or missing hours) for about
  2 KB per row. Station upserts can change an observation later, so the digest would
  detect a changed input but not rebuild it; decide whether that is acceptable.
- Store the hourly series once per farm evaluation instead of once per parcel. This
  needs a new table, so **it needs a core migration** from the integration coordinator.

`scripts/check.sh` after this change: ruff clean; **2774 passed, 3 skipped, 281 deselected
in 456.04s**. Focused advisor/review: 504 passed, 118 deselected.

### Review changes — 2026-10-22

Rebased onto `main` at `d401fc5`; pinned CSS rebuilt (38472 bytes, digest in the vendor
README). No migration; single head `review_0001`.

Blockers:

- A limited or unavailable parcel is reported as `assessment limited` or
  `assessment unavailable` in change notices and the Telegram digest, with its
  headline, never as `none` (`test_unavailable_assessment_is_pushed_as_such_never_as_none`).
- Advice records the digest of the configuration that ran. The saved-assessment read
  accepts the named version only when its configuration has that digest; otherwise the
  assessment is `unverifiable` and limited. Planting a read of the latest instead of the
  named version fails `test_saved_config_is_the_rows_own_version_and_a_hash_mismatch_is_unverifiable`.
- The assistant's version facts are exactly `{id, status}`; returning the full identity
  fails the assistant test.

Should-fix:

- Availability is computed once per request and passed to `latest_advice`. When every
  parcel's latest advice was evaluated within one job interval and saved its status,
  those rows are used without re-evaluating; legacy or stale advice is still rechecked.
  Scene B replayed to 15 Sep 06:00, 10 parcels, median of 15 requests:

  | Route | Before | After (fresh advice) | After (advice 3 h old) |
  |---|---|---|---|
  | `/advisor/today` | 235 ms | 24 ms | 125 ms |
  | `/advisor/map/farm` | 236 ms | 27 ms | 123 ms |
  | `/advisor/layer.geojson` | 243 ms | 36 ms | 125 ms |
  | `/forecast/plan` | 246 ms | 24 ms | 132 ms |
  | `/notifications/` | 239 ms | 27 ms | 133 ms |

  Limit: between two hourly evaluations, readings that disappear are only reflected at
  the next evaluation. The stale path still loads the whole season once.
- The map layer colours a limited or unavailable parcel with no action as uncertain; a
  limited parcel with observed risk keeps its action colour and says so.
- The bell names limited parcels while the farm is available.
- Proposals, opinions and activations use the farm (replay) clock; each step records
  who did it.
- The reproduce test rebuilds spray inputs from the stored spray logs, quantities and
  products and keeps a rejected non-finite reading raw.
- SECURITY.md documents advisor-parameter authority, including self-approval by a single
  member, and the recorded author of each step.

Tests now: +31 added, 3 changed, 0 deleted from `main`.

`scripts/check.sh` on this head: ruff clean; **2833 passed, 3 skipped, 281 deselected in
431.36s** (skips: optional SIGPAC/InfoRiego data). Focused advisor/review/forecast/
notifications/push/tour: 717 passed, 119 deselected.

### Re-review changes — 2026-10-22

- The Crop protection list and the parcel card show "Assessment limited" or
  "Assessment unavailable" instead of "No action"; the consumer table gained
  `/advisor/` and card rows, which fail without the fix.
- `test_current_availability_trusts_only_fresh_saved_assessments` covers the fresh-advice
  shortcut. Planted faults all fail it: ignoring stored unavailable rows, removing the
  shortcut, and ignoring the age limit.

Tests now: +32 added, 3 changed, 0 deleted from `main`. Focused advisor/review/forecast/
notifications/push/tour: 723 passed, 119 deselected. `scripts/check.sh`: ruff clean;
**2839 passed, 3 skipped, 281 deselected in 438.18s** (skips: optional SIGPAC/InfoRiego
data).
