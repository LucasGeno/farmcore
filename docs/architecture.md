# Architecture

FarmCore is a modular monolith in Python: one FastAPI process, one PostgreSQL + PostGIS
database, a small core and installable apps, in the style of Odoo.

```text
 Connectors (each feed is a job on one clock)                       Telegram
   Météo-France hourly station ─┐                                     │ voice / text
   Open-Meteo forecast runs    ─┤  normalise · ext_id · log_source    ▼
   ISOXML TaskData upload      ─┘──────────────┐          Whisper on the server → transcript
                                               ▼                      ▼
   CORE   farm · parcel · asset · log · quantity · product   Extractor: Gemini (optional, EU)
          observation · advice · event (timeline)            or local regex → draft → farmer ✅
          data_grant · access_log · app registry · clock
                     │ ScopedRepository (farm ∩ grant scopes) → access_log per read
   ┌───────┬─────────┼──────────┬─────────────────┬──────────┬────────┬──────────┐
 Today  Map &     Records    Crop protection    Weather   Sharing  Season &   Assistant
        parcels   (register)  (spray advisor)                       impact    (web + Telegram)
                     │
   Out: Reg. (EU) 2023/564 register (CSV / JSON) · FIWARE NGSI-LD AgriParcelOperation
```

## Core

- **App registry.** Each app ships a manifest (`core/manifest.py`): `name`, `depends`, `scopes`,
  `models`, `jobs`, `routes`, plus what it adds to the UI (`nav`, Today `widgets`, `map_layers`,
  map panel sections, debug panels, assistant `tools`). Apps load in dependency order, and after
  their optional `after` apps when present. The core and the page shell import no app: they
  render what the enabled apps declare. Required apps
  (Today, Map & parcels, Records) are always on; the others switch on and off on `/apps`
  (administrator only) or with `farmcore apps` ([module lifecycle](#module-lifecycle)).
- **Canonical model**, farmOS-shaped: `farm`, `parcel` (PostGIS polygon + validated `props`
  JSONB), `asset`, `log` + `quantity` + `product`, `observation`, `advice`, `event`.
  Apps extend it with 1:1 side tables or `props`, never by injecting columns at runtime.
- **Provenance.** `ext_id(source, key)` makes re-imports idempotent; `log_source` lets one spray
  carry several sources (voice + machine file). An incoming spray merges with a log of the same
  farm whose parcels overlap and whose start is within 12 h, keeping the more precise values.
- **Clock and jobs.** A `Clock` with live and replay modes. The station feed, the advisor and
  the advice push are jobs declared by manifests; ISOXML files are imported by upload or CLI
  through the same `ext_id` / `log_source` pipeline. In replay, a step advances the clock and runs
  due jobs in manifest order; this is how the demo replays the 2024 season. In live mode no job
  runs in the web process: `farmcore worker` runs them ([durable live tasks](#durable-live-tasks)).
- **Access.** Farm data is read through `ScopedRepository(farm, grant)`: it checks the farm and
  consent scopes (`fields:read`, `logs:read`, `register:read`, …) and writes an `access_log` row
  for each read. Third-party grants are read-only, cannot read the audit log, and are revocable;
  the farmer sees reads under each grant on `/share/`. There is one administrator per
  installation (the `ADMIN_TOKEN` holder) and no per-user accounts: see
  [SECURITY.md](../SECURITY.md) for what anonymous visitors can see.
- **Read-only tour.** `/tour` sets a browser cookie that refuses every non-GET request before
  routing, even with administrator credentials, for sharing a demo safely. Every page shows a
  tour banner; the only requests let through are the ones that leave the tour (`POST /tour/exit`
  and the two sign-outs), each with its usual Origin/CSRF checks.
- **Farm location.** A farm's coordinates, time zone and country live in its `props`. The time
  zone comes from the offline `timezonefinder` data and the country from bundled Natural Earth
  boundaries; sea and border points ask the owner. Farm-scoped times use the farm's zone, with
  the installation `display_tz` as the fallback.
- **Weather providers.** A weather source implements one contract (`core/weather/contract.py`)
  and is declared by its app (`Manifest.weather_providers`); an installation offers the
  providers of its enabled apps. Providers write one normalised schema (`weather_receipt`,
  `weather_station`, `weather_observation`, `weather_forecast`: CF variables, licence and
  attribution per receipt), which the station and forecast views and the advisor read. The
  replayed Météo-France station, live Météo-France observations (`weather_meteofrance`, DPObs
  and the DPClim archive, under the same station), ERA5 history through Copernicus
  (`weather_era5`: a model's grid point, flagged `model-reanalysis`) and Open-Meteo are the
  providers today; the legacy
  `observation` and `forecast_value` rows are still written beside them until the contract step.
  Saving the farm pin chooses its sources (`core/weather/selection.py`): the country's station
  providers, stations within 50 km ranked by great-circle distance with any station more than
  300 m above or below the farm down-ranked (the farm elevation is the owner's, never derived),
  every forecast provider, and backfill providers (`Capabilities.backfill`, ERA5 once its app
  exists). Discovery runs outside the farm's locks, at most 5 s per provider, and asks a
  live-only provider (`Capabilities.live_only`) only in live clock mode; a provider that is not
  configured or fails is noted. The choice and its reasons live in
  `farm.props["weather_sources"]`; in Expert view the owner overrides them on the location step.
- **Migrations.** One linear Alembic chain; each revision is prefixed with its app
  (`<app>_<NNNN>_<slug>`).

## Apps

| App | Does |
|---|---|
| Set up | First-run location step: time zone and country derived offline, optional address search, weather sources chosen from the pin |
| Today | Home page assembled from app widgets |
| Map & parcels | Aerial map (MapLibre) with layers and panel sections contributed by apps |
| Records | Spray logs, ISOXML import, Reg. (EU) 2023/564 register with record checks |
| Crop protection | Downy/powdery mildew rules (3-10 rule, Goidanich incubation, Gubler–Thomas, wash-off, intervals): spray / wait / protected, with reasons |
| Planned jobs | Persistent spray jobs from stored advice: parcels, product, duration, revisions and cancellation; never completed work or stock movement |
| Weather | Météo-France station ingest, spray conditions, charts |
| Météo-France live | Optional: live hourly observations of the farm's Météo-France station (DPObs, gaps from the DPClim archive) with the installation's portal credentials |
| Forecast | Saved forecast runs and live Open-Meteo receipts ranked into a spray plan |
| Sharing | Grants, revoke, access log, NGSI-LD export, grantee view |
| Season & impact | Model replay vs a spray calendar and the Agreste 2019 reference |
| Activity, Inventory, Notifications | Farm timeline, product and fertiliser stock, in-app alerts |
| Fertilization | Optional: fertiliser applications as drafts the owner confirms or corrects, with the declared composition and its source; never infers a composition, density or area |
| Assistant | Web panel and Telegram: voice/text → draft spray → ✅; answers questions from read-only app tools |
| Spanish archive (`demo_es`) | Optional: SIGPAC parcels and an InfoRiego station archive from local files; no advice |

The advisor is a pure calculation over observations and logs. Every threshold lives in
`apps/advisor/engine/config.py` with its source, or an "assumed" label shown in the UI.

## AI boundary

AI is optional and off by default (`EXTRACTOR=regex`). Audio is always transcribed on the
server (faster-whisper). With `EXTRACTOR=vertex`, the transcript, parcel aliases, a product
shortlist and the date go to Vertex AI `gemini-2.5-flash` on a regional EU endpoint
(`europe-west4` by default). The model returns a draft entry; the rules decide the advice and
the farmer confirms every entry. A clarification additionally sends the unfinished draft and up
to six recent turns from the same farm/chat (kept on the open draft, 15-minute expiry). If the call fails, the local
regex parser takes over. Every call is logged with its exact payload (administrator view of
`/chat/`; visitors see a sanitised summary).

The web assistant uses the model only for the administrator: it sends the question, the
available read-only tool declarations and up to three earlier questions; the model picks one
tool, and the server renders fresh facts through `ScopedRepository`. There is no SQL, shell,
browsing or new write path. Anonymous and tour visitors get a local keyword router and no model
call. Spray reports from the web panel use the same slot-filling workflow as Telegram: the
model extracts fields, code decides what is missing, previews the checks and requires an
explicit confirm.

Your own agent (Claude Desktop, Codex, a local model) can use the same tools over MCP:
`farmcore mcp` serves them on stdio, as the agent token in `FARMCORE_TOKEN` (issued on
Sharing). It offers the tools of the apps that are on now within the token's scopes (a
`propose` tool also needs `drafts:propose`), checks arguments against each tool's JSON Schema,
re-checks the token on every request and writes an `access_log` row per call (`mcp:<tool>`,
actor `token:<id>`; the client's own name is never the actor). Details: `core/mcp.py`.
Stdio is an installation-operator transport: the token bounds the agent's MCP tool calls, not
the OS user or a shell-capable agent that can read the process's `DATABASE_URL`, which reaches
every farm. Run it as `docker compose exec -T -e FARMCORE_TOKEN app farmcore mcp` (over `ssh`
for a remote host) so `DATABASE_URL` stays in the container, and never hand stdio to anyone but
the operator ([SECURITY.md](../SECURITY.md#mcp-over-stdio-is-an-operator-transport)).
Remote agents use `farmcore mcp --http`: Streamable HTTP at `/mcp`, off unless
`MCP_HTTP_ENABLED`, on loopback unless `MCP_HTTP_HOST` is set, stateless with JSON responses.
Each request carries its own bearer token in `Authorization` (never the URL), passes `Host` and
`Origin` allowlists, per-address and per-token rate limits and a body limit, and every list and
call re-checks that token. Details: `core/mcp_http.py`,
[SECURITY.md](../SECURITY.md#mcp-over-http-is-for-remote-agents).
With `MCP_OAUTH_ENABLED` the web app is also the OAuth 2.1 authorization server for `/mcp`
(`core/oauth`): the farm owner approves an agent on a consent page, and the agent gets
short-lived tokens bound to the MCP endpoint's URL, refreshed by rotation; the connection is a
grant on Sharing ([SECURITY.md](../SECURITY.md#oauth-for-mcp-lets-an-owner-approve-an-agent)).
Agents register themselves: by an https `client_id` that is their metadata document's URL
(fetched with SSRF fences, cached) or by Dynamic Client Registration (`core/oauth/clients.py`).

## Stack

Python 3.12, FastAPI, SQLAlchemy 2 + GeoAlchemy2, Alembic, Pydantic, Jinja + HTMX + Tailwind,
MapLibre GL, PostgreSQL 17 + PostGIS, Procrastinate (live background work), python-telegram-bot,
faster-whisper, Docker. Front-end libraries and fonts are bundled
([inventory](../src/farmcore/core/static/vendor/README.md)); map tiles come from IGN and
OpenStreetMap. Licences: [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## Deliberately simple, with upgrade paths

- One linear migration chain (per-app branches later).
- Live background work runs on one Postgres-backed queue (Procrastinate, with a plain
  `SKIP LOCKED` table as the documented exit); no broker, no in-process scheduler.
- A plain `observation` table (a TimescaleDB hypertable has the same schema).
- One administrator per installation: grants show the consent model but are not yet
  authenticated multi-tenant access.
- Assistant context is one expiring draft plus a few recent turns, kept on that draft: no
  vector store, long-term memory or multi-step agent loop.


### Live weather receipts

In live clock mode `forecast.fetch_live` is durable work ([below](#durable-live-tasks)): it
queues one fetch task per due farm, at most five per tick, least recently attempted first, and
`farmcore worker` runs them, each with a ten-second total HTTP deadline (headers and body) and
six UTC forecast days. Successful and failed attempts share an hour-long database cache. `forecast_fetch_timeout_seconds` configures the total
per-farm deadline (default 10 seconds, bounded to 0.05–30); expiration cancels and closes
the HTTP request, and the failure is recorded in the farm's forecast health. Native OS DNS lookups cannot be killed
safely: at most two may continue after their HTTP request times out. The forecast's two
process-shared daemon resolver workers start lazily; two admission slots cover both queued
and running lookups and remain occupied until the native work finishes (or canceled queued
work is skipped). Saturation fails immediately rather than growing
threads/queues. Late DNS completion never restarts a canceled HTTP request. Requests use the farm's `props.forecast_location`
(`latitude`, `longitude` in degrees), or its first geolocated weather station; a farm
without either reports an unavailable refresh. Requests go to `OPEN_METEO_BASE_URL` (default
the free public API) and carry `OPEN_METEO_API_KEY` as `apikey` when set; an installation can
use the customer API or a self-hosted server instead ([self-hosting](self-hosting.md#6-open-meteo-weather-data-optional)).
Each receipt records `endpoint_kind` (`public`, `customer` or `self-hosted`) and the keyless
request URL, never the key. The free API is non-commercial only; commercial installations use
the customer API or self-host. No request runs in replay mode.

The [current forecast API](https://open-meteo.com/en/docs) returns a changing model blend,
without a provider issue timestamp. These responses are immutable **receipts**, never
labelled as issued model runs: `provider_issued_at` is unknown, `as_issued` is false,
and the legacy non-null `issued_at` column acts only as receipt identity for this source.
`fetched_at` and `available_at` are the wall time after the response arrived. Both requested
coordinates and returned grid coordinates, source URL, units and per-variable coverage
remain attached to the receipt. Duplicate response content retains the original receipt.
Failures preserve prior valid data. Home, Plan and Data sources mark receipts older than
two hours stale and show missing input coverage. Forecast days missing required rain/wind
hours (or temperature for sulphur) remain unavailable for spray ranking.

Rain remains millimetres over the preceding hour; wind is km/h and temperature Celsius.
Replay reads only receipts/runs issued and available by its clock; it can replay saved
receipts after their actual receipt time, while offline as-issued demo runs remain usable.
Live receipts never become station observations or retrospective disease-model evidence.

`farmcore forecast fetch` archives single runs from `OPEN_METEO_SINGLE_RUNS_BASE_URL` (default
the public Single Runs API) with the same optional key; saved files record the keyless request
URL and `endpoint_kind`. A self-hosted Open-Meteo server serves the per-run files its
`data_run/` store holds, so archive the runs an installation needs; the hosted Single Runs
service keeps the full archive.

### Durable live tasks

A manifest job with a `task` (`JobSpec.task`) is durable live work, one task per farm:
`forecast.fetch_live`, `stations.ingest_meteofrance` and `advisor.evaluate`. In live mode its job
function only enqueues, in the caller's transaction (a rolled-back request queues nothing):
request handlers and the worker's tick never fetch, ingest or evaluate. A farm is due when no
attempt published within the job's interval in the current clock generation, so a restarted
worker forgets nothing and switching replay → live makes every farm due at once.
`farmcore worker` (the Compose `worker` service) plans due work every 15 seconds, runs the
queued tasks (four at once by default, so one farm's slow provider does not hold up another's)
and retries the tasks of a worker whose heartbeat stopped, at most three runs in all.

`jobs.run_now` for given farms (after a chat confirm, a correction or a completed job) queues
their task instead of evaluating in the request; the chat confirm waits up to three seconds
for the new advice, then answers anyway. The jobs without a task (`drafts.expire`,
`chat.push_advice`, `catalog_fr_ephy.import`) run on the worker too, under the step lock, when
their interval has passed since their last live run (kept in `clock_state`; a job is first due
one interval after a worker first sees it). Replay never enqueues: `step_to` and `run_now` stay
in-process and deterministic.

The queue is [Procrastinate](https://procrastinate.readthedocs.io/) (MIT, tables in `public`,
schema applied from a checksummed vendored copy by revision `core_0009`); only
`core/tasks.py` imports it. What may be written is decided by FarmCore's own fence, not by the
library's job status: each run takes a new attempt in `task_attempt`, and the transaction that
writes the result first checks, under row locks, that the attempt is still the current one for
its task and farm, before its deadline, that the farm exists and that the clock is live in the
generation the attempt began in. A retry, a live/replay switch (`clock_state.generation`) and a
restore (which loads no queue table and starts a new generation) therefore reject every older
attempt. No transaction is open while a task waits on the network. The deadline is judged
when the check runs, so a task that waited on a farm lock past its deadline writes nothing. A
task that fails holds its farm until one interval after it began, so a repeating failure is
retried once per interval.

Known limits:

- A worker paused or frozen inside a task keeps that task's locks. For the advisor these are
  the farm's operations lock and row lock, so every write to that farm, web requests included,
  waits. Task transactions end once idle for the task's deadline (one minute by default), which
  bounds the wait to about that, not to when the worker resumes.
- The jobs without a task run under the global step lock, network calls included (the weekly
  catalog download): a replay step waits for them. The Telegram push makes no network call
  there; it only records outbox intents.
- Their schedule is saved once all due jobs of a tick have run: a worker that dies mid-tick runs
  that tick's earlier jobs again on restart (the push moves its cursor with its intents, and each
  intent has a unique dedupe key, so it records nothing twice).

### Outbox

Deliveries outside the installation go through `core/outbox.py`. The cause's transaction
records an intent (`outbox` table; today `chat.push_advice` writes one Telegram digest per
bound chat together with its cursor), so a rollback leaves none and a committed one outlives
any worker. The worker dispatches them every tick in a loop of its own, in either clock mode,
outside the step lock; `farmcore replay` dispatches once after its step. Apps declare the
providers (`Manifest.delivery_providers`). Each send is claimed in a committed transaction
first (`SKIP LOCKED`), so no intent is sent twice once the provider accepted it: a refusal the
provider answered is retried (three claims at most), a send with no answer or whose worker died
becomes `ambiguous` and is listed by `farmcore outbox` until the operator settles it
(`farmcore outbox resolve ID sent|resend|drop`). Nothing is claimed while external effects are
paused (`outbox.may_dispatch`); intents expire on the wall clock (a digest after six hours), so
enabling never sends a stale backlog. A restore marks the intents it inherits ambiguous.

Known limits:

- Settled intents (`sent`, `failed`, `expired`) are kept: the table grows by one row per chat
  per digest until a purge exists.
- Ambiguous deliveries are shown by `farmcore outbox` and a worker warning only; there is no
  admin page for them yet.
- `farmcore replay` dispatches every farm's pending intents, including those a demo step for
  one farm recorded, not only what its own step recorded; the worker would send them anyway.
- Digests are wall-clock bound: an installation paused for more than six hours never sends the
  digests recorded meanwhile (the cards still show current advice), and enabling sooner sends
  each held digest, oldest first. A product choice, not an agronomic threshold.
- Replay steps record digests about replayed advice exactly as they did before the outbox, and
  the worker sends them whenever effects are enabled and a bot token is set.

### Drafts

Every proposed write is meant to wait in one `draft` table (`core/drafts.py`, revision
`core_0010`) until the farmer confirms it on `/drafts/<id>`. A draft holds its farm, the
proposing actor (`identity:<id>`, `operator`, `token:<id>` for an agent, `chat:<id>`), a kind
from an app's `draft_kinds`, the payload that kind validated, provenance (source type `voice`, `file`,
`isoxml`, `manual` or `agent`; file name and SHA-256; registry reference), a status (open, confirmed, cancelled, expired), a revision and
a wall-clock expiry (seven days by default, an assumed product choice).

- Only the proposing principal sees a draft; an agent's draft is reviewed by the owner who
  issued its token (stored on the draft when it is proposed; the token must be for that farm),
  and a Telegram chat's draft also by the owners of the farm the chat is linked to: the
  operator, and any member whose owner membership is active, checked at each review.
  Anyone else, and any other farm, gets a 404. An agent token never confirms, cancels or edits.
- Confirm runs the kind's app-owned confirm service under the farm's operations lock and a row
  lock: retried and concurrent confirms record once and return the same record. A stale revision
  or an expired draft is refused with the reason. Cancel is idempotent.
- The Records app's kinds (`spray`, `irrigation`, `observation`) write through `core/logs.py`
  (a pending log created and confirmed in one transaction, so the merge rule applies on every
  path) and keep the draft id, both actors and the provenance on the record's `log_source`. A
  confirmed spray saves its own station weather there, whichever path confirmed it.
- An irrigation's amount has one reader, its one entered `typed_quantity` (core_0017 backfilled
  irrigations confirmed before capture wrote one). `log.props["irrigation"]` keeps a copy with the
  gross/net basis; `operations.facts.record_irrigation` writes both in one savepoint, for capture
  and any future correction.
- Chat and the web assistant propose here too (actor `chat:<id>`, or the signed-in member), so
  a draft is the only pre-confirm state and no committed `log` is pending. An incomplete
  capture is an open draft that names what it still misses (parcel, date, product, dose,
  amount, unit, basis, text) and is never previewed with a guessed value; each clarification
  turn revises it, and it expires 15 minutes after its last turn. Telegram's ✅ / ❌ carry the
  draft id and the revision the farmer saw, and confirm through the same service as
  `/drafts/<id>`; the web panel's draft answer links there.
- Fertilization's kinds (`fertilization`, `fertilization-correction`) write one `fertilise` log
  whose entered amount names the fertiliser's catalog item; Inventory counts done records of that
  item (`inventory_0002`: a stock row names a product or a catalog item), so stock is computed,
  never decremented, and moves once. A draft that lacks a composition, a density (litres) or an
  area (a per-hectare amount on a parcel of unknown area) names it and cannot be confirmed. A
  correction is refused if the record changed since it was proposed, and keeps an
  `operation_correction` receipt with the original values, reason and actors.
- `drafts.expire` (every 15 minutes, wall time) marks open drafts past their expiry. A restored
  copy loads drafts as farm data; drafts that expired meanwhile are refused at once.

### Assessments

A module's computed result is saved in one shared envelope (`core/assessments.py`, revision
`core_0012`): farm and optional parcel, evaluation time, status (`evaluable`, `limited`,
`unavailable`), the parameter version (or, without one, the configuration itself), the input
snapshot and the typed result. The result belongs to a kind an app declares
(`Manifest.assessment_kinds`: key, version and a Pydantic model); the kind's model validates it
when it is saved and types it when it is read, and an envelope is never read as another kind. The
database refuses any update to a saved envelope (it is immutable; deletable only with its advice
or by a demo reset), so a review or a replay always refers to the evidence that was evaluated.

Crop protection saves kind `advisor.spray` (spray / wait / protected / none with its reasons,
products and windows). Its `advice` row stays the public representation every reader uses and
points at its envelope (`advice.assessment_id`, same farm, parcel and time); advice saved before
`core_0012` keeps its evidence in `advice.props` and has no envelope.

### Module lifecycle

An app is **on** when its `app_module` row is installed (no row counts as installed) and every
dependency it was loaded with is on (`registry.live_active`). Every process reads that live state,
so switching an app off takes effect within one request or worker tick, without a restart:

- web: every app router is gated (404 while off, pages and writes alike); nav, widgets, map
  layers and the assistant's tools follow the same rule;
- Telegram bot: with Chat off it answers allowed users with a fixed line before any transcription,
  extraction or draft work;
- drafts: a switched-off app's draft kinds can be neither proposed, revised nor confirmed, from
  any process (the bot included);
- worker: it loads every discovered app and, each tick, plans and runs only the apps that are on;
  a task attempt does not begin for an app that is off;
- in-flight work: a trigger adds one to `app_module.generation` on every change of state (revision
  `core_0014`). An attempt keeps the generation it began in and publishes only while its app is
  still installed in that generation, under a share lock on the row, so a disable (or a disable
  and re-enable) waits for a publication in progress and fences every later one. A durable job's
  name is `<app>.<job>`, which is how the fence finds its app;
- outbox: intents recorded by an app that is off are held (they stay pending and expire on time)
  and are sent if it is switched on before they expire.

Switching off never deletes data; switching on never runs a seed. A switch is refused while
an installed app depends on the app (off) or a dependency is off (on), and when the app's
stored settings (`app_module.settings`) fail its settings model (`Manifest.settings`). An app
built for another `core_api`, or whose `requires` names a dependency version it cannot use, or a
dependency cycle, fails at start before any write, and before any migration (`migrations/env.py`
discovers the apps first).

Known limits:

- An app that was off when a web process started mounts its routes at that process's next start;
  the worker picks it up at once.
- If the state cannot be read, the web shell and nav count every loaded app as on, so the page
  frame still renders, but an app's own routes answer 503; the worker's fence fails closed.
- The Telegram bot keeps polling while Chat is off and answers every allowed message with one
  fixed line; nothing is transcribed, extracted, drafted or confirmed.
- Settings are validated when an app is switched on, not at start: an upgrade whose settings
  model rejects the stored settings leaves the app on until it is switched off and on again.
  There is no settings editor yet.
- The task fence and the outbox check the app's own row only. A switch never leaves a dependant on
  while its dependency is off; a dependency switched off by hand in SQL stops planning (live
  rule) but not an attempt or intent already admitted.
- Two concurrent switches (on for an app, off for its dependency) read states without a lock and
  can both pass; the dependant then counts as off everywhere.

Public CAP map claims, canonical source references, snapshot signing and provider
maintenance: [registry parcels](registry-parcels.md).

### Bundles without crop protection

Crop protection (`advisor`) is optional. A bundle may leave it out, or an administrator may switch
it off, and every other app still loads, starts and navigates. Only these apps extend crop
protection and need it (`depends`): Planned jobs (`planning`), expert reviews (`review`) and
Season & impact (`kpi`, a replay of its advice). Without it:

- Forecast keeps its receipts and the five-day forecast weather; the spray window, its parcel
  section and Plan say they need crop protection ("not an all-clear"), as does the assistant,
  which no longer offers the plan tool.
- Notifications list station and data issues only; no advice or spray-window items.
- The map colours parcels by crop protection's decisions only while it is on; otherwise every
  parcel reads "no advice". A recipient's `advice.json` answers 409 in plain words (crop
  protection is not installed) rather than an empty list.
- Chat logs records as before; its advice digest has no advice events to send. With crop
  protection present, chat loads after it (`Manifest.after`), so a replay step's digest carries
  that step's evaluation.
- Hourly weather rows (`HourlyObs`, `core/weather/hourly.py`) belong to core, so reading a
  forecast never imports crop protection. Code that grades spray windows imports the advisor
  only on routes that first check it is on.

`tests/core/test_registry.py` imports every module of every other app, the CLI and the web app in
a process where the advisor package does not exist; `tests/core/test_bundle_without_crop_protection.py`
walks such a bundle over HTTP with the crop-protection packages blocked in-process.

### Shared advice and weather provenance limits

Shared advice and advisor tokens use the same farm-wide, timeless receipt gate. A farm with
no weather receipts currently passes that gate: missing receipts are treated as redistributable,
including legacy observations. This is a known limit, not proof of a licence; unknown-licence
receipts themselves prohibit redistribution. Exact enforcement needs contributing receipt IDs
on each evaluation.

Derived credits use the provider's `Licence.derived_attribution`, retained in the receipt's
`request.derived_attribution` metadata, falling back to `WeatherReceipt.attribution` for existing
receipts and providers without separate wording. Credits currently include every redistributable
receipt on the farm, even sources not used by that evaluation; receipt lineage is needed to
narrow them. Shared advice and token facts expose those credits as an `attribution` list.

Sharing's CSV omits all saved weather because capture snapshots lack receipt provenance.
The omission is explained on Sharing and in the response header, but the downloaded CSV itself
contains no omission note; a recipient receiving only the file will not see that explanation.
