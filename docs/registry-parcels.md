# Public CAP parcel claims

The onboarding CAP map fetches only a small bounding box near the saved farm pin.
France uses IGN APICarto RPG v2; Spain joins FEGA SIGPAC `recintos` and
`cultivo_declarado` by the public recinto code. Recorded fixtures and licence
citations live in `tests/apps/onboarding/fixtures/registries/README.md`.

## References and dates

Confirmed claims set `Parcel.iacs_id` to the public `source_ref`. They register a
canonical Source with its attribution, `geodata` kind and licence, plus an ExtId
whose source is `ign-rpg-YYYY` or `sigpac-current` and whose key is
`farm:<farm_id>:<source_ref>`. ExtId `(source, key)` uniqueness spans the whole
installation: the farm prefix allows two farms to claim the same public reference
without collision or cross-farm updates. Seed data's bare keys remain unchanged.

RPG rows retain the published year. Check the APICarto catalogue each spring and
bump `YEAR` in `registries/fr_rpg.py`, refreshing recorded fixtures when the new
year becomes available. The latest documented year checked on 2026-11-19 was 2024.

SIGPAC publishes no reliable campaign year in these responses. Its year stays
null; each row keeps the recinto response's `timeStamp` as `retrieved_at`, shown
in review. This is the provider response timestamp, not an inferred campaign.
`sigpac-current` deliberately identifies the live public recinto across campaigns:
reclaiming the same reference does not replace a previously confirmed boundary,
even when the provider redraws it. Updating existing parcel boundaries/history is
a separate workflow. A retrieval date does not change this identity policy.

## Fetches and claims

GET features completes its read transaction before outbound HTTP. Geometry
normalization, validation, database work and overlap review run in the threadpool.
The server signs validated candidates for fifteen minutes, binding them to the
farm and actor. POST claim makes no provider requests: the normal repository locks
the farm, rechecks owner authority, verifies the snapshot and revalidates against
the current farm country before proposing a draft. Confirmation stays explicit.
Registry draft provenance uses `source_type="registry"`.

The signing key lives only in the web process. Restarting it invalidates open maps;
reload to continue. This follows FarmCore's single-web-process architecture;
multiple web workers would require a shared signing key before enabling this flow.

Connections go directly to two fixed HTTPS hosts, without redirects or pagination
links. `trust_env=False` intentionally ignores environment proxies and environment
CA overrides so they cannot reroute registry traffic. Self-hosters requiring such
proxies must currently use file import. `Accept-Encoding: identity` asks for
uncompressed responses; unexpected compression is refused before decoding. Each
response has a 2 MB cap and the lookup has an eight-second total network deadline.
Failures, invalid geometry and oversized windows offer file import.
