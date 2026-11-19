# Changelog

Notable changes for operators and app authors. Newest first.

## Unreleased

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
