# API compatibility

Some FarmCore responses are read by other systems: a co-op's adviser, a FIWARE broker, an agent.
Their shape is a contract. When the data under one moves (spray products and doses moved from
`product`/`quantity` to the neutral catalog and its extensions in migration `core_0018`;
`core_0019` then dropped the two legacy tables and kept the id maps), the published keys and
ids keep their meaning. A change that would alter them ships as a new version
beside the old one, and the old one is deprecated with a date before it is removed.

## How a client picks a version

- Add `?v=<n>` to the request. Without it you get the default version.
- Every versioned response names itself: `FarmCore-Representation: <name>; v=<n>`.
- A deprecated version also sends `Deprecation: @<unix time>` (RFC 9745), a
  `Link: <...>; rel="deprecation"` to this page and, once a removal date is set,
  `Sunset: <HTTP date>` (RFC 8594).
- An unknown or malformed version is a `400` that lists the versions served. It never falls back
  silently.

The registry is `src/farmcore/core/representations.py`; the app that renders a representation
registers it.

## Representations

### `ngsi-ld` (NGSI-LD export)

`/share/ngsi-ld.json` (owner) and `/share/as/{grant}/ngsi-ld.json` (grantee).

- **v1** (default; deprecated since 2026-11-19 for one release; `Sunset: Fri, 19 Feb 2027
  23:59:59 GMT`, provisional until the maintainer confirms it; a release after that day
  removes v1 and makes `?v=2` the default). `AgriParcelOperation` entity ids are
  `urn:ngsi-ld:AgriParcelOperation:farmcore-<log>-<parcel>-<dose>` and `hasAgriProductType` is
  `urn:ngsi-ld:AgriProductType:farmcore-<product>`. For a dose and product recorded before
  `core_0018`, `<dose>` and `<product>` are the ids published since the first export (read
  through its frozen maps); one recorded since has none and takes its v2 ref.
- **v2** (`?v=2`): catalog-native refs everywhere: `<dose>` is `tq<typed quantity id>` and
  `<product>` is `ci<catalog item id>`, a namespace no v1 id uses. Description, quantity and
  every other property are the same as v1. These ids are not stable across a rollback: if an
  operator downgrades below `core_0018` and upgrades again, the moved rows get new catalog ids
  (v1's legacy ids stay the same). A downgrade below `core_0019` rebuilds the legacy tables
  from the maps under their legacy ids first.

### `advice` (shared advice)

`/share/as/{grant}/advice.json` (grantee).

- **v1** (default, current): the shape of 2026-11-19: one current row per parcel, the
  whitelisted keys only (`id`, `parcel_id`, `evaluated_at`, `decision`, `headline`,
  `reasons`, `next_window`, `protected_until`, `mm_since_spray`, `incubation_pct`, `flags`,
  `products`, `passes`, `treatments`, `model_validation`), `attribution`,
  `assessment_status`, `config_version` (`id`, `hash`, `scope`, `status`, `review` when
  reviewed, and `accuracy_claim: false`) and `current_inputs_status`. `products` lists crop
  protection's generic recommendations (`name`, `product_type`, `targets`) in engine order,
  never ranked and never a farm's product rows or ids, so the catalog move does not change it.
- `model_validation` (`{level, label, models, about}`) was added to v1 the day it was registered, an additive
  key: the validation level stamped when the advice was computed (see "Added keys" below).
- `headline` and the reasons' `verdict` are text for people. Advice computed since 2026-11-22
  words a downy result with no infection event as `No action: no infection event detected` and
  `no infection event detected` (before: `No action: no infection risk` and `no risk`); stored
  rows keep the words they were saved with. Read `decision`, not the wording.
- `flags` can hold `weather_gap_filled` (since 2026-11-22): some hours came from a lower-ranked
  weather source. Shared advice omits the per-hour source map
  (`weather_sources`, `filled_by`); its `attribution` list still credits the farm's weather sources.

## Inputs

### Spray drafts (`drafts:propose`, kind `spray`)

- **v2** (current): each dose names its product by `catalog_item_id`.
- **v1** (deprecated since 2026-11-19): a dose naming `product_id`, a legacy product id, is
  still accepted: it is resolved once through `core_0018`'s frozen map and stored as v2. An id
  the map does not hold, or a dose naming both keys, is refused (never guessed). New clients
  send v2. The agent tools carry no HTTP headers per call, so the MCP `propose_record` result
  says it instead: `facts.input_deprecated` (`since`, `note`, `link`) and a note in its text.

### Sensor readings (`weather:push`)

`POST /weather_push/observations` takes SensorThings-shaped JSON or CSV from a device token
([self-hosting](self-hosting.md#on-farm-sensors-optional)). It has one input version and is not
versioned yet.

### Parcel and crop proposals (`drafts:propose`)

The agent tools `propose_parcels` (inline GeoJSON) and `propose_crop_assignments` create
drafts that only the farmer confirms. They have one input version and are not versioned yet.

## Stored evidence

Assessment evidence (`/advisor/parcels/{id}/assessments/{assessment}.json`) is returned as it
was saved and never rewritten. Since `core_0018` its `input_snapshot` carries
`evidence_version: 2` and each dose as `{typed_quantity, catalog_item, crop_protection_item,
advisor_product}`; a snapshot saved before has no `evidence_version` and holds
`{quantity, product}`.

## Added keys

Every surface that shows an assessment also carries its `model_validation`: `level` (the
validation level key, such as `unvalidated`), `label` (its words), `models` (the model keys)
and `about` (`/models?kind=<kind>`). It appears on the advisor card `.json`, history and
review assessment JSON, `advisor/layer.geojson`, `fields/parcels.geojson` and the grantee
`advice.json`. It is the level stamped when the result
was computed, never upgraded later; rows stored before it read `unvalidated`. `/models?kind=`
and `/models/{key}` describe each model from its spec to any signed-in user, a grant recipient
included. It was added without a new version; a client that rejects unknown keys needs to accept
it.

The advisor card `.json` also carries `weather_filled` (per source and variable, the runs of
hours it filled) and `weather_gap_filled` (the runs lower-ranked sources filled). Since
2026-11-22 an assessment's `input_snapshot` can list `weather_sources` and each
`hourly_inputs` row's `filled_by` (variable → index into `weather_sources`); snapshots saved
before have neither.
ERA5 sources are named `copernicus-era5:farm<id>:<lat>,<lon>` since `core_0020`.

## Unchanged and not versioned

These carry no dose id, and no product id that the move changes; fixtures pin them, so the move
cannot change them:

- the spray register (`register.csv`, `register.json`, owner, `/share` and grantee). A
  `register.json` check key names its product (`dose:<product>`, `applications:<product>`,
  `interval:<product>`) by the same ref as NGSI-LD v1: the legacy product id for a product
  recorded before `core_0018`, `ci<catalog item id>` for one added since. Its bytes are pinned
  against the release before the move (`tests/core/test_crop_protection_move.py`);
- the assistant and MCP tool facts (`operations_last_spray`, `operations_recent_sprays`,
  `advisor_parcel_status`);
- `kpis.json`.

## Internal, changed in lockstep

Same-origin forms and pages (planning jobs, spray corrections, inventory) change with the code
that renders them and are not versioned. Their option values became catalog item ids with
`core_0018`. A planning completion retried with the same request key after the upgrade is a
409 ("Request key already used"): its fingerprint covers the product id, which changed.
