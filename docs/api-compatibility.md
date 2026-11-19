# API compatibility

Some FarmCore responses are read by other systems: a co-op's adviser, a FIWARE broker, an agent.
Their shape is a contract. When the data under one moves (ticket 14 moves spray products and
doses from `product`/`quantity` to the neutral catalog and crop protection's extension), the
published keys and ids keep their meaning. A change that would alter them ships as a new version
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

- **v1** (default, current). `AgriParcelOperation` entity ids are
  `urn:ngsi-ld:AgriParcelOperation:farmcore-<log>-<parcel>-<dose>` and `hasAgriProductType` is
  `urn:ngsi-ld:AgriProductType:farmcore-<product>`, where `<dose>` and `<product>` are the ids
  published since the first export. v1 keeps naming the same dose and product after they move
  tables.
- **Planned v2** (with the move): catalog-native ids. v1 is then deprecated for one release,
  and its Sunset date is set by the contract step that drops the legacy tables.

### `advice` (shared advice)

`/share/as/{grant}/advice.json` (grantee).

- **v1** (default, current): the shape of 2026-11-19 (#121): one current row per parcel, the
  whitelisted keys only, `attribution`, `assessment_status`, `config_version` and
  `current_inputs_status`. `products` lists crop protection's generic recommendations
  (`name`, `product_type`, `targets`), never a farm's product rows or ids, so the catalog move
  does not change it.

## Unchanged and not versioned

These carry no product or dose id; frozen fixtures pin them, so the move cannot change them:

- the spray register (`register.csv`, `register.json`, owner, `/share` and grantee);
- the assistant and MCP tool facts (`operations_last_spray`, `operations_recent_sprays`,
  `advisor_parcel_status`);
- `kpis.json`.

## Internal, changed in lockstep

Same-origin forms and pages (planning jobs, spray corrections, inventory) change with the code
that renders them and are not versioned.
