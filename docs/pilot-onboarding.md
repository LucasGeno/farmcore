# Operator-assisted private pilots

For the first one or two growers, Lucas prepares each installation and its parcels locally;
the grower receives an HTTPS login. There is no public signup, invitation email, billing or
hosting control plane. Use fictional data for team demonstrations. A private pilot is not the
public showcase, and the showcase's availability does not establish a private pilot's readiness.

## Prepare one installation

Follow [self-hosting](self-hosting.md) for prerequisites, independent secrets, backups, TLS and
upgrades. Use a separate Compose project, env file, port and hostname per installation. Keep
all env files and farmer files outside version control; do not paste their contents in issues.
Example project names below are placeholders, not existing deployments.

```sh
cp .env.example pilot-a.env
# Set unique POSTGRES_PASSWORD, RUNTIME_DATABASE_PASSWORD, ADMIN_TOKEN,
# BACKUP_RECIPIENT, PUBLIC_BASE_URL=https://pilot-a.example.org and FARMCORE_PORT=8002.
# For a live grower: CLOCK_MODE=live. Keep DEMO_MODE=false and DEMO_FARM_IDS=[].
# Configure the HTTPS proxy separately; do not expose the app port publicly.
dc() { docker compose -p pilot-a --env-file pilot-a.env "$@"; }
dc up -d --build --wait --wait-timeout 180
curl -fsS http://127.0.0.1:8002/health
```

Health must include an `installation_id`, release, schema heads and `state: ok`. Record the
ID in the operator's private inventory. A legacy response containing only `db: ok` is not this
release's readiness check. Verify the backup recipient belongs to the intended owner and keep
the decryption key off the server; make and copy a first encrypted backup off-host before handoff.

```sh
dc exec app farmcore farms create "Pilot vineyard"
dc exec app farmcore farms list
# Use the actual ID printed above, here illustrated as 1:
dc exec app farmcore users create grower --farm-id 1
```

The hidden, confirmed password prompt requires 15–256 characters. Agree a unique password
privately with the grower; send neither the operator token nor database credentials. If using
a secret-manager pipe, pass `exec -T ... --password-stdin`; never put a password in arguments.
There is no first-login password-change page or emailed reset link. Lucas handles recovery via
`users set-password`, which revokes sessions. Access remains governed by explicit memberships.

## Add initial parcels

Prepare a UTF-8 GeoJSON FeatureCollection with longitude/latitude WGS84 (EPSG:4326) coordinates.
Each feature needs `properties.alias`, `name`, positive `area_ha`, and a valid nonempty 2D
Polygon or MultiPolygon. Optional properties: `iacs_id`, `eppo_crop`, `variety`, `crop_label`,
`planted_year`. Unknown properties are refused, including farm IDs. No CRS declarations;
reproject GIS exports to WGS84 before importing. Aliases must be unique within the file and
farm. The command accepts at most 1,000 features and 5 MB per file. Area is the operator-supplied
hectare value, not a computed or certified cadastral area.

For a fictional training parcel, use [examples/pilot-parcels.geojson](../examples/pilot-parcels.geojson).
Its coordinates and area are illustrative, not measured vineyard boundaries. For a real pilot,
the grower supplies or approves the geometry and metadata; keep that file private.

```sh
# Copy into this project's app only; stdin is not used for parcel files.
dc cp /private/path/parcels.geojson app:/tmp/pilot-parcels.geojson
dc exec app farmcore parcels import /tmp/pilot-parcels.geojson --farm-id 1
dc exec app rm /tmp/pilot-parcels.geojson
```

Import is atomic: one bad feature or conflicting existing alias rejects the whole batch.
Repeating the same file skips unchanged parcels. Existing parcels are never overwritten;
changes to an existing alias require a separately reviewed correction path. Do not rename
aliases to evade a conflict: that would create additional parcels. Numeric IDs and GeoJSON
feature IDs do not select a farm; only `--farm-id` does. This is an operator-local command,
not an owner web-upload feature.

## Handoff check

1. Open the installation's HTTPS `/login` in a fresh browser. Anonymous parcel reads must fail.
2. Sign in as the grower, then open `/fields/?view=list&farm=1` and the map. Check the agreed
   names, areas and shapes. Empty weather/advice is expected until the pilot's data sources and
   crop/model context are configured and reviewed; importing a polygon does not validate advice.
3. With a disposable second farm on a test installation, verify the grower cannot select it.
   Never use another grower's actual records as a test fixture.
4. Create pilot B with its own project/env/hostname/port. Compare health installation IDs: they
   must differ. Login on A must not authorize B. Verify both independently before sharing access.
5. Make the backup: `dc run --rm --no-deps -e BACKUP_ONCE=1 backup`; copy the resulting encrypted
   bundle off-host using the self-hosting procedure. Confirm its restore on a disposable install.

Matteo/Vasiliki demonstrations should use separate fictional installations/accounts. Provision
their own logins; do not share the operator token. For a richer archived example, the existing
`seed --scene B` and replay workflow loads explicitly fictional/open-data demo content into a
dedicated demonstration installation. Seeding alone does not enable anonymous access. Avoid
mixing seed data with a grower's private pilot.

Lucas still owns hosting/TLS, account provisioning/recovery, approved parcel preparation,
source configuration, backups/restore checks and upgrades. Growers can sign in, inspect their
assigned farms and use the capabilities permitted by their role. Browser parcel drawing,
self-service signup and first-login setup are not shipped by this workflow.

## Reproduce the onboarding regression

Use the disposable dev database described in CONTRIBUTING.md, then run:

```sh
uv run pytest -q tests/apps/fields/test_parcel_import.py tests/instances/test_pilot_onboarding.py
```

The installation test starts its own disposable PostGIS container and uses real migrated
runtime roles, the CLI and HTTPS HTTP requests. It checks two installation IDs, initial parcel
visibility, anonymous/cross-farm denial and rejection of the other installation's cookies.
It does not configure a real reverse proxy, browser UI, mail delivery or production hosting.
