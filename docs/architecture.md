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
  map panel sections, debug panels, assistant `tools`). Apps load in dependency order. The core
  and the page shell import no app: they render what the enabled apps declare. Required apps
  (Today, Map & parcels, Records) are always on; the others switch on and off on `/apps`
  (administrator only).
- **Canonical model**, farmOS-shaped: `farm`, `parcel` (PostGIS polygon + validated `props`
  JSONB), `asset`, `log` + `quantity` + `product`, `observation`, `advice`, `event`.
  Apps extend it with 1:1 side tables or `props`, never by injecting columns at runtime.
- **Provenance.** `ext_id(source, key)` makes re-imports idempotent; `log_source` lets one spray
  carry several sources (voice + machine file). An incoming spray merges with a log of the same
  farm whose parcels overlap and whose start is within 12 h, keeping the more precise values.
- **Clock and jobs.** A `Clock` with live and replay modes. The station feed, the advisor and
  the advice push are jobs declared by manifests; ISOXML files are imported by upload or CLI
  through the same `ext_id` / `log_source` pipeline. In replay, a step advances the clock and runs
  due jobs in manifest order; this is how the demo replays the 2024 season.
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
  replayed Météo-France station and Open-Meteo are the two providers today; the legacy
  `observation` and `forecast_value` rows are still written beside them until the contract step.
- **Migrations.** One linear Alembic chain; each revision is prefixed with its app
  (`<app>_<NNNN>_<slug>`).

## Apps

| App | Does |
|---|---|
| Set up | First-run location step: time zone and country derived offline, optional address search |
| Today | Home page assembled from app widgets |
| Map & parcels | Aerial map (MapLibre) with layers and panel sections contributed by apps |
| Records | Spray logs, ISOXML import, Reg. (EU) 2023/564 register with record checks |
| Crop protection | Downy/powdery mildew rules (3-10 rule, Goidanich incubation, Gubler–Thomas, wash-off, intervals): spray / wait / protected, with reasons |
| Planned jobs | Persistent spray jobs from stored advice: parcels, product, duration, revisions and cancellation; never completed work or stock movement |
| Weather | Météo-France station ingest, spray conditions, charts |
| Forecast | Saved forecast runs and live Open-Meteo receipts ranked into a spray plan |
| Sharing | Grants, revoke, access log, NGSI-LD export, grantee view |
| Season & impact | Model replay vs a spray calendar and the Agreste 2019 reference |
| Activity, Inventory, Notifications | Farm timeline, product stock, in-app alerts |
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
to six recent turns from the same farm/chat (15-minute expiry). If the call fails, the local
regex parser takes over. Every call is logged with its exact payload (administrator view of
`/chat/`; visitors see a sanitised summary).

The web assistant uses the model only for the administrator: it sends the question, the
available read-only tool declarations and up to three earlier questions; the model picks one
tool, and the server renders fresh facts through `ScopedRepository`. There is no SQL, shell,
browsing or new write path. Anonymous and tour visitors get a local keyword router and no model
call. Spray reports from the web panel use the same slot-filling workflow as Telegram: the
model extracts fields, code decides what is missing, previews the checks and requires an
explicit confirm.

## Stack

Python 3.12, FastAPI, SQLAlchemy 2 + GeoAlchemy2, Alembic, Pydantic, Jinja + HTMX + Tailwind,
MapLibre GL, PostgreSQL 17 + PostGIS, APScheduler (live mode), Procrastinate (durable live tasks), python-telegram-bot,
faster-whisper, Docker. Front-end libraries and fonts are bundled
([inventory](../src/farmcore/core/static/vendor/README.md)); map tiles come from IGN and
OpenStreetMap. Licences: [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## Deliberately simple, with upgrade paths

- One linear migration chain (per-app branches later).
- An in-process scheduler for most jobs; live forecast fetching is already a durable task
  (Procrastinate, with a plain `SKIP LOCKED` table as the documented exit).
- A plain `observation` table (a TimescaleDB hypertable has the same schema).
- One administrator per installation: grants show the consent model but are not yet
  authenticated multi-tenant access.
- Assistant context is one expiring draft plus a few recent turns, kept in process: no vector
  store, long-term memory or multi-step agent loop.


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

A manifest job with a `task` (`JobSpec.task`) is durable live work; today only
`forecast.fetch_live`. In live mode its job function only enqueues, in the caller's transaction
(a rolled-back request queues nothing): request handlers and the worker's tick never fetch.
`farmcore worker` (the Compose `worker` service) plans due work every 15 seconds, runs the
queued tasks (four at once by default, so one farm's slow provider does not hold up another's)
and retries the tasks of a worker whose heartbeat stopped, at most three runs in all. Replay
never enqueues: `step_to` stays in-process and deterministic.

The queue is [Procrastinate](https://procrastinate.readthedocs.io/) (MIT, tables in `public`,
schema applied from a checksummed vendored copy by revision `core_0009`); only
`core/tasks.py` imports it. What may be written is decided by FarmCore's own fence, not by the
library's job status: each run takes a new attempt in `task_attempt`, and the transaction that
writes the result first checks, under row locks, that the attempt is still the current one for
its task and farm, before its deadline, that the farm exists and that the clock is live in the
generation the attempt began in. A retry, a live/replay switch (`clock_state.generation`) and a
restore (which loads no queue table and starts a new generation) therefore reject every older
attempt. No transaction is open while a task waits on the network.

### Drafts

Every proposed write is meant to wait in one `draft` table (`core/drafts.py`, revision
`core_0010`) until the farmer confirms it on `/drafts/<id>`. A draft holds its farm, the
proposing actor (`identity:<id>`, `operator`, `token:<id>` for an agent, `chat:<id>`), a kind
from an app's `draft_kinds`, the payload that kind validated, provenance (source type `voice`, `file`,
`isoxml`, `manual` or `agent`; file name and SHA-256; registry reference), a status (open, confirmed, cancelled, expired), a revision and
a wall-clock expiry (seven days by default, an assumed product choice).

- Only the proposing principal sees a draft; an agent's draft is reviewed by the owner who
  issued its token (stored on the draft when it is proposed; the token must be for that farm).
  Anyone else, and any other farm, gets a 404. An agent token never confirms, cancels or edits.
- Confirm runs the kind's app-owned confirm service under the farm's operations lock and a row
  lock: retried and concurrent confirms record once and return the same record. A stale revision
  or an expired draft is refused with the reason. Cancel is idempotent.
- The Records app's kinds (`spray`, `irrigation`, `observation`) write through `core/logs.py`
  (a pending log created and confirmed in one transaction, so the merge rule applies as in
  chat) and keep the draft id, both actors and the provenance on the record's `log_source`.
- `drafts.expire` (every 15 minutes, wall time) marks open drafts past their expiry. A restored
  copy loads drafts as farm data; drafts that expired meanwhile are refused at once.

Not yet moved onto drafts: the chat and web-assistant capture still keep their pre-confirm state
as a pending `log` and their clarifications in `farm.props["clarifications"]`. The follow-up
"Move chat and assistant capture onto drafts" removes both, so there is one pre-confirm state;
until then no other entry path commits a pending `log`.
