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
  routing, even with administrator credentials, for sharing a demo safely.
- **Migrations.** One linear Alembic chain; each revision is prefixed with its app
  (`<app>_<NNNN>_<slug>`).

## Apps

| App | Does |
|---|---|
| Today | Home page assembled from app widgets |
| Map & parcels | Aerial map (MapLibre) with layers and panel sections contributed by apps |
| Records | Spray logs, ISOXML import, Reg. (EU) 2023/564 register with record checks |
| Crop protection | Downy/powdery mildew rules (3-10 rule, Goidanich incubation, Gubler–Thomas, wash-off, intervals): spray / wait / protected, with reasons |
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
MapLibre GL, PostgreSQL 17 + PostGIS, APScheduler (live mode), python-telegram-bot,
faster-whisper, Docker. Front-end libraries and fonts are bundled
([inventory](../src/farmcore/core/static/vendor/README.md)); map tiles come from IGN and
OpenStreetMap. Licences: [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## Deliberately simple, with upgrade paths

- One linear migration chain (per-app branches later).
- An in-process scheduler (a `job` table with `SKIP LOCKED` workers for real farms).
- A plain `observation` table (a TimescaleDB hypertable has the same schema).
- One administrator per installation: grants show the consent model but are not yet
  authenticated multi-tenant access.
- Assistant context is one expiring draft plus a few recent turns, kept in process: no vector
  store, long-term memory or multi-step agent loop.


### Live weather receipts

The installed forecast app schedules `forecast.fetch_live` hourly in live clock mode.
Each tick requests at most five farms, least recently attempted first, with a ten-second
total HTTP deadline per farm (headers and body) and six UTC forecast days. Successful and failed attempts share
an hour-long database cache. `forecast_fetch_timeout_seconds` configures the total
per-farm deadline (default 10 seconds, bounded to 0.05–30); expiration cancels and closes
the HTTP request before the next farm proceeds. Native OS DNS lookups cannot be killed
safely: at most two may continue after their HTTP request times out. The forecast's two
process-shared daemon resolver workers start lazily; two admission slots cover both queued
and running lookups and remain occupied until the native work finishes (or canceled queued
work is skipped). Saturation fails immediately rather than holding the job lock or growing
threads/queues. Late DNS completion never restarts a canceled HTTP request. Requests use the farm's `props.forecast_location`
(`latitude`, `longitude` in degrees), or its first geolocated weather station; a farm
without either reports an unavailable refresh. No provider credentials are needed.
Operators should check [Open-Meteo's usage terms](https://open-meteo.com/en/terms) for their
installation before enabling live mode. No request runs in replay mode.

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
