# Changelog

Notable changes for operators and app authors. Newest first.

## Unreleased

- With `MCP_OAUTH_ENABLED`, the MCP 401 challenge now names `scope`: the read scopes an
  authorization request naming none gets (never `advice:read` or `drafts:propose`). Clients
  such as Claude Code and Codex then ask for least privilege instead of every supported scope.
  Consent is unchanged.
- A client whose metadata document stays gone (404, 410 and other 4xx but 408/429, a non-public
  address, a redirect, a non-JSON, compressed or oversized answer) now loses its connections too,
  once `MCP_OAUTH_CIMD_GRACE` (whole seconds or ISO 8601, default 7 days; 0: at once) has passed
  since the first such failure; until then it keeps working on its last good document. A
  successful fetch resets the clock; timeouts, 5xx, 408, 429 and network errors never count.
  Migration `core_0024` adds `oauth_client.failing_since`.
- Procrastinate 3.12.0 (was 3.10.0). Migration `core_0023` applies the library's own 3.11.0 and
  3.12.0 queue migrations (vendored, checksummed) in the usual single migrate transaction; its
  two `CONCURRENTLY` index steps run as plain index builds, which briefly lock the task queue
  while the upgrade runs with the app, worker and bot stopped. Downgrade restores the 3.10.0
  queue and refuses while a job uses the new `mutex` lock mode.
- When a client's metadata document turns invalid or names another client, FarmCore now ends
  every connection of that client at once (grant and tokens revoked, an event on Sharing), not
  only its next refresh (CIMD §6.1). The Connect page no longer says agents that sign in are
  "coming": with `MCP_OAUTH_ENABLED` it shows the Claude Code and Codex commands, else how the
  operator switches sign-in on. Checked with real Claude Code 2.1.330 and Codex 0.161.0 clients.

- The Rossi 2008 model is now on the About pages (`/models/advisor.downy_rossi`), linked from its
  badge on the parcel card; it still never feeds the stored spray decision. With missing weather
  hours and no infection computed, the card says primary infections are unknown instead of "none
  so far". Each trace step names the source and tier of temperature, humidity and rain
  separately. App authors: `Manifest.model_specs` lists models an app shows without storing
  their result; they are checked at load like an assessment kind's models.

- The parcel card shows the Rossi et al. 2008 downy mildew primary infection model beside the
  3-10 rule: infection hours and the expected symptoms window, the primary inoculum season and
  its validation level (unvalidated). It runs on the merged observed series since 1 January;
  missing hours make it "Limited" and pause every stage. Each infection carries a trace of the
  stages that led to it, with each input's source and tier and whether wetness was measured or
  estimated. The spray decision still uses the 3-10 rule. Implemented from
  the paper and checked against the viticolR oracle fixtures; no migration.

- The advisor reads one merged hourly weather series per parcel (`core/weather/merge.py`),
  ranked from the parcel's centroid: on-farm sensors first, then agricultural networks, national
  services and models, each provider declaring its tier on `Capabilities.tier` (stored on its
  receipts). The latest forecast fetched by now fills future hours nothing observed. Only
  receipts fetched by the clock time are read, so a replay never sees a later backfill. A farm
  with provider weather but no station asset is evaluated. An hour filled from a source other
  than the best one flags the advice `weather_gap_filled`; the input snapshot records each
  hour's source per variable (`hourly_inputs[].filled_by`) and the parcel JSON every source's
  hours (`weather_filled`). With a single source nothing changes. A provider serving a WIGOS
  station already held writes to that station's row. App authors: declare `tier` on your
  provider's `Capabilities`; an undeclared provider ranks after every tier.

- The 3-10, Goidanich and Gubler–Thomas rules are ModelSpec modules in
  `apps/advisor/engine/mildew/` that own their parameters and sources; `AdvisorConfig` reads its
  defaults from them. No threshold, output or config digest changes. `GOIDANICH_TABLE` and
  `GoidanichTable` are still importable from `engine.config`; `specs.DOIS` moved to
  `engine.mildew.DOIS`.
- App authors: an `AssessmentKind` now declares its own new `read_scope` (required,
  `<name>:read`): not a core scope, not any app's table scope, and not another kind's. Crop
  protection's `advisor.spray` keeps `advice:read`, the one compatibility exception, so no grant
  or token gains or loses access. The scoped repository reads an `assessment` row only with its
  kind's scope, and refuses `execute()` statements, and `list()`/`get()` predicates or orderings,
  that name the `assessment` table anywhere: read envelopes with `list()` or `get()`. It also
  refuses opaque SQL anywhere in a statement or predicate (`text()`, `literal_column()`, a
  `VALUES` payload), and, for a grant view, any statement or predicate naming a weather value
  table, EXISTS included. A new kind's scope is never granted by default.
- Grant views: withheld weather is indistinguishable from none. The grantee page and
  `observations.json` no longer list each withheld source; `withheld` is now one generic
  sentence shown whenever the grant reads weather, and the shared sources list every
  redistributable receipt.
- Migration `core_0019` drops the legacy `product` and `quantity` tables that `core_0018`
  froze; the maps and the tables' id sequences stay. Downgrading below it rebuilds both from the
  maps and the catalog rows under their legacy ids, with their sequences as they were, and then
  `core_0018`'s rules apply: a rollback returns every record intact or refuses, naming them. It
  refuses while a plant-protection item or a measured dose has no legacy id, or while a spray
  has a correction confirmed since the upgrade that replaced a moved dose (only its receipt is
  left). Never delete rows to get past a refusal: restore the pre-upgrade backup to roll back
  records changed since.
- NGSI-LD v1 has a Sunset date: 19 Feb 2027 (provisional), sent as `Sunset` (RFC 8594).
- App authors: core no longer defines `Product`, `Quantity`, `PRODUCT_TYPES`, `PRODUCT_TARGETS`
  or `ADVICE_DECISIONS`; the `Advice` model and its decisions moved to
  `farmcore.apps.advisor.advice` (the `advice` table is unchanged). New manifest fields:
  `activity_products` (the activity products column), `demo_counts` (demo control's counts) and
  `query_values` (query values an app's page accepts, checked by return links).

- Spray products and doses move onto the catalog tables (migration `core_0018`): each product
  becomes a `catalog_item` with operations' `crop_protection_item` (label facts) and crop
  protection's `advisor_product` (contact/penetrant, targets); each dose a `typed_quantity` that
  keeps what was entered (`entered_measure`). `product` and `quantity` are kept, frozen (the
  runtime role can no longer write them), for the rollback; `legacy_product_map` and
  `legacy_quantity_map` resolve ids kept in older JSON. Operators: run `farmcore migrate` on a
  copy of the database first. It refuses, changing nothing and listing every row by id, when a
  product name is blank or over 200 characters, a dose is NaN or infinite or has a blank unit or
  unknown measure, a product's type or targets are outside contact/penetrant and downy/powdery,
  a farm's shortlist is not a list of whole-number ids, an extension row already exists, or a
  record other than a spray would hold more than one amount (a pre-#92 legacy quantity beside
  another or beside its entered amount). Fix those rows and run it again. Downgrading below it
  is refused while a spray recorded, changed or removed since the upgrade (or a product, stock
  row, planning job or shortlist entry naming a new product) has no legacy row to return to;
  restore the pre-upgrade backup to roll those back, never delete them.
  `farmcore migrate` and `farmcore upgrade` now print a migration's refusal in full (ids and
  counts, no row values).
- NGSI-LD export: v1 (the default) is deprecated since 2026-11-19 and sends `Deprecation`
  (RFC 9745) and a `Link` to `docs/api-compatibility.md`; its ids are unchanged. Ask for `?v=2`
  for catalog-native ids (`tq<id>`, `ci<id>`). The Sunset date comes with the release that drops
  the legacy tables.
- `drafts:propose` spray proposals: `doses[].product_id` (v1, a legacy product id) is deprecated;
  send `catalog_item_id`. A v1 id is resolved through the frozen map and never guessed; the MCP
  `propose_record` result flags it (`facts.input_deprecated`).
- Assessment evidence saved from now on carries `evidence_version: 2` (doses as typed quantities
  with their catalog item and extensions); stored evidence is returned as it was saved.
- A NaN or infinite spray dose is now refused when it is written (every capture path already
  refused one).
- Register checks (`register.json` and the register page) keep their keys: a product recorded
  before the move is still named by its legacy id; a product added since by `ci<catalog item
  id>`. A stocked plant-protection product is never offered as a fertiliser.
- Every agronomic model declares a `ModelSpec` (`core/model_spec.py`): method and citation,
  EPPO codes, BBCH window, inputs, sourced parameters, validation level and provenance. App
  authors: `AssessmentKind.models` is now required. A loaded kind that names no `ModelSpec`, or
  a spec missing a declaration, fails the start with `ModelSpecInvalid`.
- Advice JSON (the card's `.json`, grantee `advice.json`, review assessment JSON, the
  assistant/MCP `advisor_today`, `advisor_parcel_status` and review facts, the fields
  `parcels.geojson`) gains `model_validation`: `{level, label, models, about}`. The level is
  stamped on the assessment envelope (`config_version.model_validation`) when the advice is
  computed. Advice stored before this release reads as `unvalidated`. A badge with the level sits
  beside every advice result in the UI.
- New pages `GET /models` (`?kind=`) and `GET /models/{key}`: "About this model", generated from
  the spec. Any signed-in principal, a grant recipient included, may read them (no farm data).
- Citation corrections in the advisor config: the 3-10 rule is via Velasquez-Camacho et al. 2023
  (not "Gessi et al. 2022"), and the Gubler–Thomas, Arrêté, Genève, Goidanich, copper and sulphur
  sources are corrected or qualified. New advice reasons carry the corrected text.
- With `MCP_OAUTH_ENABLED`, agents register themselves: an https `client_id` that is the URL of
  their Client ID Metadata Document (fetched with SSRF fences and cached), or open Dynamic
  Client Registration at `/oauth/register` (RFC 7591, public clients, rate-limited). Loopback
  redirect URIs may change port, as Claude Code and Codex need. The consent page marks names
  agents gave themselves as self-reported and says each scope in plain words.
  `MCP_OAUTH_CLIENTS` stays optional. Migration `core_0016`.
- Grantee `advice.json` now returns current, licence-gated advice per parcel, instead of up to
  50 stored evaluations. Grant/token headlines show the decision (or withholding reason), with
  derived-source credits in a separate `attribution` list. Weather inputs, reason notes and
  incubation percentages are null for grants/tokens; withheld projections drop derived props.
  Sharing's register CSV link now uses `/share/register.csv` and omits saved weather because
  snapshots lack receipt licence provenance. Owner advice and register exports are unchanged.

- Agent tokens and OAuth connections that hold `advice:read` can now read the farm's stored spray
  advice through the advisor tools (it was a no-op before). Existing ones start doing so on
  upgrade: review Sharing and remove `advice:read` from any you do not want to read advice. A
  new OAuth connection no longer gets it by default; the owner ticks it on the consent page.
  Advice is withheld while the farm holds weather whose licence bars redistribution, and when
  it is out of date.
- Sharing gains a "Connect your agent" page (`/share/connect`): the MCP URL, token creation, and
  copy-paste setup for Codex (and Claude Desktop over stdio, shown to the operator only). The
  Expert view probes the URL without a token and says whether it is up, and whether it is
  loopback-only (a tunnel or hosting is needed).
- `farmcore mcp --http` serves the farm's tools to remote agents over MCP Streamable HTTP at
  `/mcp`, with the agent tokens issued on Sharing as bearer tokens. Off unless
  `MCP_HTTP_ENABLED=true`; loopback unless `MCP_HTTP_HOST` is set; Origin and Host allowlists,
  per-token and per-address rate limits and a body limit (`MCP_HTTP_*`, see `.env.example`).
  A token's "Last used" on Sharing now moves at most once a minute.
- New app `weather_era5` fills the season's history for a farm's pin from ERA5 through the
  Copernicus CDS (free account, `CDSAPI_KEY`; CC BY 4.0), stored as model data under a grid-point
  station, and weather source selection now offers it as the backfill. Live mode only.
- Switching an app off on `/apps` or with `farmcore apps` takes effect at once in every
  process (web, Telegram bot, worker), not at the next start: its pages and writes return 404
  (a fragment already on an open page shows "could not load" until reload), its tools and draft
  kinds disappear, the bot answers that the assistant is off when Chat is off, its background
  work is neither planned nor published and its outbox messages wait until it is switched on
  again. Manifests gain `core_api`, `requires` and `settings`; a durable job is named
  `<app>.<job>`. Migration `core_0014`.
