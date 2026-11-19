# Changelog

Notable changes for operators and app authors. Newest first.

## Unreleased

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
