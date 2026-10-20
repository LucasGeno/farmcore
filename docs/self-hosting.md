# Self-hosting

`docker-compose.yml` is one installation: five services, one image, one Compose project.

| Service | What |
|---|---|
| `db` | PostgreSQL 17 + PostGIS; no published port |
| `migrate` | One-shot `farmcore migrate` as the database owner, then exits; see below |
| `app` | The web app on `127.0.0.1:8000`; starts after `migrate` exits 0, never migrates |
| `bot` | Telegram long-polling bot; only with `--profile telegram`; also waits for `migrate` |
| `backup` | Daily `farmcore backup`: one encrypted bundle into the `backups` volume, kept 7 days |

Each installation runs its own PostgreSQL server, and FarmCore expects that: servers shared
with other software or other installations are not supported for now. The server has two
roles that matter. `farmcore` (password `POSTGRES_PASSWORD`) is its bootstrap superuser (the
image's `POSTGRES_USER`); it owns the `farmcore` database, its schema and the PostGIS
extension. Only `migrate` and `backup` hold its password, and `migrate` refuses to run as any
role that is not a superuser. `farmcore_app` (password `RUNTIME_DATABASE_PASSWORD`) is what the
app, the bot and `docker compose exec app farmcore ...` use. After every `migrate` it holds
exactly:

- `CONNECT` on the `farmcore` database, and `USAGE` on its `public` schema;
- `SELECT`, `INSERT`, `UPDATE`, `DELETE` on the tables and views there, except read-only
  `alembic_version` and the PostGIS extension's own (`spatial_ref_sys`, `geometry_columns`,
  `geography_columns`); `USAGE`, `SELECT` on the sequences;
- the same, by default, on tables and sequences `farmcore` creates there later.

It is a member of no role, has no role-level settings, and is not a superuser, cannot create
databases or roles, replicate or bypass row security, has no connection limit and no expiry.
So, unless an operator grants rights to `PUBLIC` (every role) by hand, it cannot create
extensions, schemas, tables or temporary tables, alter, drop or truncate tables, write
`alembic_version`, or become another role. `migrate` resets `PUBLIC` only in these places:
all rights on the `farmcore` database; `CREATE` on the `public` schema; writes and `TRUNCATE`
on the read-only tables above; and the default `CONNECT` and `TEMPORARY` rights on every other
database on the server except `template0` (which allows no connections). That is a
server-wide change, fine on a dedicated server. Anything else granted to `PUBLIC` (say
`TRUNCATE` on an ordinary table, or `CREATE` on another schema) also reaches `farmcore_app`
and survives `migrate`: do not grant to `PUBLIC`. A database created on the server after
`migrate` gets PostgreSQL's default `PUBLIC` rights until the next `migrate`.

If `farmcore_app` already exists, `migrate` resets it before granting the above: it refuses
a role that owns any object or is named in a row security policy (the reset would drop it
from the policy silently); runs `DROP OWNED BY farmcore_app` (which, for a role that owns
nothing, only revokes) in `postgres` and in `farmcore`, covering those two databases and the
shared objects (databases, tablespaces, parameters), default privileges included; then
revokes, as their grantor, any grant another role made there; revokes every role membership,
whoever granted it; and resets role settings and attributes. It fails if a grant or
membership is left. The `postgres` pass commits first and gives `CONNECT` on `farmcore` back
at once, so a failure later in `migrate` never locks out the running app. Privileges on
objects inside other databases (`template1`, `template_postgis`) are not reset, since
`migrate` does not connect there; `farmcore_app` cannot connect to them. `migrate` needs
PostgreSQL 16 or later (the compose file runs 17).

One right PostgreSQL gives every role and cannot take away: creating large objects
(`lo_create`). FarmCore uses none; the runtime role could store data that way, inside this
installation's database only, which it can already do in rows. A large object it owns makes
the next `migrate` refuse until it is reassigned to the owner.

`migrate` upgrades the schema to head, gives the installation its identity on first run, and
creates or resets `farmcore_app` every time. As the runtime role (`docker compose exec app
farmcore migrate`) it fails with an explanation; use `docker compose run --rm migrate`. The
password reaches PostgreSQL only as a SCRAM verifier.

The identity, `installation_id`, is a random UUID the server generates once; nobody chooses
it. It is stored as a setting of the database (not a table, so no schema change), survives
restarts and upgrades, and is reported by `/health`. It names the installation, never a farm:
one installation holds many farms. Whatever carries database settings keeps it: a copy of the
`pgdata` volume, `pg_dumpall`, or `pg_dump --create` restored with `pg_restore --create`.
Whatever does not gets a new one at the next `migrate`: `CREATE DATABASE ... TEMPLATE`, or a
plain `pg_dump` restored into a new database. A bundle's archive records the database's
settings, the id among them, but `farmcore restore` never applies them and always gives the
copy a new id (see "Backups and restore"): a restored bundle is a new installation, never the
original moved. A copy that keeps the id (a copied volume, `pg_dumpall`, `pg_restore
--create`) is the same installation: never run it next to the original on one host, or the
two share cookie names.

Read [SECURITY.md](../SECURITY.md) before exposing an installation to a network.

## 1. Configure

```sh
cp .env.example .env
```

Set at least:

- `POSTGRES_PASSWORD`: `openssl rand -hex 32`
- `RUNTIME_DATABASE_PASSWORD`: `openssl rand -hex 32` (a different value)
- `ADMIN_TOKEN`: `openssl rand -hex 24` (at least 32 characters, no whitespace)
- `PUBLIC_BASE_URL`: the HTTPS address users open

The compose file always sets `FARMCORE_PUBLIC=1`: the app refuses to start without a strong
`ADMIN_TOKEN`, and the interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are off.
Private farm reads require a local member session or the separate operator token, including
in local development. Anonymous requests fail closed; health and sign-in remain available.

On arm64 hosts set `POSTGIS_IMAGE=imresamu/postgis:17-3.5`.

## 2. Start

```sh
docker compose up -d --build
docker compose ps                                   # app healthy; db, backup up
curl -fsS http://127.0.0.1:8000/health              # {"db":"ok","installation_id":"…"}
```

A new installation is empty: no farm, no member, no demo data. Set it up from the CLI:

```sh
docker compose exec app farmcore farms create "Château Example"   # prints: farm 1 created
docker compose exec app farmcore farms list
docker compose exec app farmcore users create farmer --farm-id 1   # see below
docker compose exec app farmcore apps                               # optional apps and state
docker compose exec app farmcore apps --uninstall assistant --uninstall kpi
docker compose restart app                 # app switches apply at start
docker compose --profile telegram restart bot  # if the bot runs
```

`restart` keeps a container's environment. After changing `.env`, run
`docker compose up -d` (with `--profile telegram` if the bot runs): Compose recreates the
containers whose settings changed.

`farmcore apps` refuses to switch off a required app or an app another installed app needs, as
the `/apps` page does. Everything else is environment, read at start (`.env`, see
[.env.example](../.env.example)):

| What | Variables |
|---|---|
| Base URL | `PUBLIC_BASE_URL` |
| Time zone | `DISPLAY_TZ` (default `Europe/Paris`); `CLOCK_MODE=live` for real time |
| Units | Metric (ha, L/ha, kg/ha, mm, °C) throughout; not configurable yet |
| Connectors | Telegram: `TELEGRAM_*`; Gemini: `EXTRACTOR`, `VERTEX_*`; voice: `WHISPER_MODEL` |
| Demo | `DEMO_MODE`, `DEMO_FARM_IDS` (both off by default) |

Nothing in an installation calls a hosting service, a subscription endpoint or an AI provider
unless you configure a connector: with the defaults (`EXTRACTOR=regex`, no Telegram, replay
clock) the server runs with no outbound network at all. Live weather (`CLOCK_MODE=live`)
fetches public forecasts. The browser still loads map tiles itself, from `data.geopf.fr` (IGN
aerial imagery) and `tile.openstreetmap.org`: without them the map shows parcels on a blank
background.

Optional demo data (the open-data Saint-Émilion estate, replaying 2024), never loaded unless
you run:

```sh
docker compose exec app farmcore seed --scene B
docker compose exec app farmcore replay --to "2024-04-10 06:00"
```

The administrator signs in at `/admin/sign-in` with `ADMIN_TOKEN`; the demo controls are at
`/demo/`. For a public showcase, explicitly set `DEMO_MODE=true` and `DEMO_FARM_IDS=[1,2]`
using the intended demo farm IDs. Then share `/tour` for a read-only walkthrough. Seeding
alone does not publish farms; a tour cookie never authorizes private data.

## Local owner accounts

After migration, identify the intended farm ID locally, then provision its owner:

```sh
docker compose exec app farmcore users create farmer --farm-id 1
```

Use the actual farm ID; no farm is inferred from its name, a grant name, or the demo default.
The command prompts for a hidden password and confirmation (15–256 characters). Do not put a
password in shell arguments or environment variables. For unattended provisioning,
`--password-stdin` accepts standard input; use your secret manager's stdin pipe.

Open the installation's HTTPS `/login` page. The owner can open **Farm members** to assign or
revoke owner/advisor roles for accounts created locally with `farmcore users create <login>`.
An advisor can read assigned farms but cannot import operations or change installation settings.
Revocation and disabled accounts lose authority on the next request. The last enabled owner
cannot be removed through the browser; the local operator can recover access with the CLI.

```sh
docker compose exec app farmcore users revoke-sessions farmer
docker compose exec app farmcore users disable farmer
docker compose exec app farmcore users set-password farmer
```

Password replacement revokes existing sessions and re-enables the identity. The installation
operator remains a separate break-glass identity at `/admin/sign-in`. Shared web voice/draft
capture and global model-call audit remain operator-only in this phase. Ordinary members use
the farm-scoped read-only assistant and existing owner-authorized machine import workflow.
Sharing UI/grant recipients remain operator-only until recipient binding is implemented.

## 3. Reverse proxy and TLS

Serve FarmCore over HTTPS only: member and admin cookies are `Secure`. Keep the app bound to
`127.0.0.1` (`FARMCORE_BIND`) and proxy to it, for example with Caddy:

```caddyfile
farm.example.org {
    reverse_proxy 127.0.0.1:8000
}
```

If the proxy runs in another container or host, set `FORWARDED_ALLOW_IPS` to its address so
uvicorn trusts its `X-Forwarded-*` headers. Limit request body size and rate at the proxy.

## 4. Telegram bot (optional)

1. Create a bot with [@BotFather](https://t.me/BotFather); set `TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_BOT_USERNAME`.
2. Set `TELEGRAM_ALLOWED_USER_IDS` to the farmers' numeric Telegram ids (empty: nobody).
3. `docker compose --profile telegram up -d`

Run exactly one bot per token: a second poller anywhere makes both fail with 409 Conflict.
Voice notes are transcribed in the container with faster-whisper; the `small` model (about
460 MB) downloads on first use into the `whisper-cache` volume.

## 5. Gemini on Vertex AI (optional)

Without it, a local parser turns messages into drafts. To use Gemini:

1. In a Google Cloud project with Vertex AI enabled, create a service account with a custom
   role granting only `aiplatform.endpoints.predict`, and download a JSON key to
   `./secrets/vertex.json` (readable by uid 10001, nobody else).
2. In `.env`: `EXTRACTOR=vertex`, `VERTEX_PROJECT=<project id>`, and optionally
   `VERTEX_LOCATION` (default `europe-west4`) and `VERTEX_MODEL` (default `gemini-2.5-flash`).
3. `docker compose -f docker-compose.yml -f docker-compose.vertex.yml up -d`

What is sent to the model is described in [architecture.md](architecture.md#ai-boundary); every
call is logged with its payload on `/chat/`.

## Backups and restore

A backup is one file, a **bundle**: a `pg_dump` of the installation database (custom format,
without privileges) and a `manifest.json` (installation id, release, app versions and states,
schema revision, creation time, the dump's size and SHA-256), in one tar archive encrypted with
[age](https://age-encryption.org) to the owner's public key. Everything private is in the
database (FarmCore keeps no private files; `DATA_DIR` holds open-data caches only), so the
bundle is the whole installation. It never contains `POSTGRES_PASSWORD`,
`RUNTIME_DATABASE_PASSWORD`, `ADMIN_TOKEN`, connector tokens or keys, or any database role:
those are this installation's environment, not its data.

### What a bundle can and cannot do

**age gives a bundle confidentiality, not authenticity.** Only the owner's private key reads
one, but anyone holding the public key (it sits in the server's env file) can encrypt a bundle
of their own, so a bundle cannot prove who wrote it. Restore therefore treats every bundle as
data only:

- it never runs a bundle's SQL as the database superuser. The schema comes from this release's
  own migrations, never from the dump; a dump with anything that schema does not have (a
  function, trigger, view, extra table or index, another schema) is refused;
- it loads only rows and sequence positions of FarmCore's own tables, with `pg_restore
  --data-only` connected as a temporary role that may only insert into those tables: it is not
  a superuser, belongs to no role, and can create nothing anywhere. The role is dropped
  afterwards;
- it never loads member sessions, never applies the database settings an archive records, and
  always starts the copy with external effects paused.

What restore cannot check is whether the rows are the ones you backed up. Everything that
matters is a row: members and their password hashes, farm roles and memberships, sharing
grants, app states and settings, and the farms' links to Telegram chats. Whoever forges a bundle
therefore chooses who can sign in to the copy (as its owner, with a password they know) and,
once you enable effects, which chats its advice push writes to. Restore only bundles you wrote
yourself and kept where only you can write: a bundle someone hands you is just as untrusted as
one found on shared storage. Keep the private key off the server.

### The owner's key

Create the key pair once, on your own computer or password manager, not on the server:

```sh
age-keygen -o farmcore-backup.key        # private: keep it safe and offline; it decrypts
age-keygen -y farmcore-backup.key        # prints the public key, age1...
```

Put the public key in the installation's env file as `BACKUP_RECIPIENT=age1...` (only `age1`
keys; several are separated by spaces). The server then only ever holds the public key: it can
write bundles but not read them, so a compromised server cannot read old backups. Lose the
private key and every bundle is lost with it. Without `BACKUP_RECIPIENT` the `backup` service
writes nothing (and deletes nothing) and logs `backup: FAILED: set BACKUP_RECIPIENT` every hour
(`docker compose logs backup`).

### Back up

The `backup` service writes `farmcore-<UTC time>.age` daily into this project's `backups`
volume, or into `BACKUP_DIR` when set (one directory per installation, writable by uid 10001),
and deletes bundles older than seven days. Copy them off the server: a backup on the same disk
does not survive that disk.

```sh
docker compose run --rm --no-deps -e BACKUP_ONCE=1 backup     # one bundle now
docker compose run --rm --no-deps --entrypoint ls backup -l /backups
mkdir -p ~/farm-backups
docker compose cp backup:/backups/farmcore-20261020T020000Z.age ~/farm-backups/   # off the server
# Or anywhere with the owner's DATABASE_URL, pg_dump and age:
farmcore backup --recipient age1... --output farm-a.age
```

`pg_dump` cannot dump a newer PostgreSQL server than itself: the image ships PostgreSQL 17's
client, matching the compose file's server. `farmcore backup` refuses a newer server and names
both versions; on a newer server image, back up with a FarmCore image whose client matches.

To look inside one: `age -d -i farmcore-backup.key farm-a.age | tar -xO manifest.json`.

### Restore into a new installation

A restore always creates a new installation: same farms, members, records, sources and
provenance, app evidence and settings, but a new `installation_id` (so new cookie names), the
new project's own secrets, no signed-in sessions (members sign in again with their passwords),
and **external effects paused**. Restore never overwrites an installation: the target database
must hold no data of its own, so start only the new project's `db`. Keep the bundle and the key
in a directory outside the checkout (so it is never committed), readable only by you; the
restore container runs as your user so it can read them:

```sh
cp .env.example farm-c.env      # new POSTGRES_PASSWORD, RUNTIME_DATABASE_PASSWORD, ADMIN_TOKEN,
                                # PUBLIC_BASE_URL, FARMCORE_PORT; BACKUP_RECIPIENT
docker compose -p farm-c --env-file farm-c.env up -d --wait db
mkdir -m 700 ~/farmcore-restore
cp ~/farm-backups/farmcore-20261020T020000Z.age ~/farmcore-restore/bundle.age
cp ~/keys/farmcore-backup.key ~/farmcore-restore/owner.key
docker compose -p farm-c --env-file farm-c.env run --rm --no-deps \
  --user "$(id -u):$(id -g)" -v ~/farmcore-restore:/restore:ro migrate \
  farmcore restore /restore/bundle.age --identity /restore/owner.key
rm -P ~/farmcore-restore/owner.key 2>/dev/null || shred -u ~/farmcore-restore/owner.key
docker compose -p farm-c --env-file farm-c.env up -d
```

`farmcore restore` first checks it can read both files (a file the container's user cannot
read is named, with the `--user` hint), decrypts the whole bundle into a private temporary
directory and checks it: the key, every encrypted chunk (a modified or truncated file fails),
exactly two plain files no larger than the archive (no links, devices or sparse files), the
manifest's fields, the dump's size and SHA-256, the release (a bundle from a newer release,
or with an app this release does not ship, is refused: restore it with that release or later),
and that the schema revision inside the dump is the one the manifest names and is known to
this release. Only then does it touch the server: it checks that it runs as the superuser and
that the target holds no data, builds the copy in a staging database closed to every other
role with external effects already paused, creates the schema from this release's migrations,
refuses any dump entry that schema lacks, loads the rows as the temporary loader role, adds
the foreign keys back (which checks every row; a failure names the constraint, never a row),
empties the session tables and drops the loader. The staging database then takes the target's
name, and `migrate`'s steps follow: upgrade an older schema, generate the new
`installation_id`, grant the runtime role. The app can connect only after that.

Any refusal or failure before the swap leaves the target, the source installation, the bundle
and older bundles as they were, and leaves no staging database, loader role or plaintext. That
includes a restore stopped by `SIGTERM` or `SIGHUP` (`docker compose stop`, a closed terminal):
a signal that arrives while the staging database or the loader role is being created waits
for that to finish, so that both can be removed.
`SIGKILL` (or a power cut) skips that cleanup: the staging database
`farmcore_restore_<hex>_<hex>`, a full copy of the rows in clear, and its
`farmcore_restore_<hex>` role may stay, and so may the temporary directory
`farmcore-restore-*` (with the rows in clear) under `$TMPDIR` or `/tmp`. In a `run --rm`
container that directory goes with the container; run outside one, delete it by hand or let
the next restore do it. The next restore into the same target removes the database and the
role first, and any of your `farmcore-restore-*` directories older than six hours.

One restore runs per target: a second one into the same database is refused while the first
runs. Just before the swap, restore checks the target again and refuses it if it has gained data
meanwhile. Restore also revokes `CONNECT` and `TEMP` from PUBLIC on every database of the
server but `template0`, permanently, as `migrate` does, so the loader role can open only the
staging database. Restores and migrations on one server take turns for that step, and a
database cannot be created, renamed or dropped while it runs.

If restore stops after the swap (say, the runtime role cannot be set up), it says so: the
data is in, paused, with no sessions. Fix the cause and run
`docker compose -p farm-c --env-file farm-c.env up -d`, whose `migrate` finishes the job. A swap
that fails halfway renames the target back, and `SIGTERM` or `SIGHUP` during the swap waits
for it to finish, then says the copy is in. Only if the process is killed with `SIGKILL`
between the two renames is there no `farmcore` database: rename `farmcore_restore_<hex>_<hex>`
to `farmcore` with `ALTER DATABASE`, and drop the empty `farmcore_old_<hex>_<hex>`.

The restored installation is independent of the original: restoring into C never changes A
or any other project, and both may keep running.

Never run `docker compose down -v` on an installation with data: it deletes the database
volume.

### External effects

An installation's **external effects** are what it does outside itself on its own: today the
Telegram advice push (`chat.push_advice`) and the bot's polling of its token. A restored copy
starts with them paused, so two copies never both message the same farmers, and a second
poller on one bot token never breaks the original's bot. While paused, the advice push sends
nothing (pending changes go out in one digest once enabled), and the bot waits without polling
until effects are enabled, then starts by itself.

```sh
docker compose -p farm-c --env-file farm-c.env run --rm --no-deps migrate farmcore effects
docker compose -p farm-c --env-file farm-c.env run --rm --no-deps migrate farmcore effects enable
docker compose -p farm-a --env-file farm-a.env run --rm --no-deps migrate farmcore effects pause
```

Only the database owner can switch them (the `migrate` service holds its password); the app's
runtime role cannot, and is told which role it is connected as. Pausing stops the advice push
from its next run; a bot that is already polling stops at its next start (`docker compose
restart bot`). An installation that was never restored has effects enabled.

### Connectors after a restore

Connector credentials live in the env file, never in a bundle, so enter them again in the new
installation's env file before enabling effects:

1. **Telegram.** If C replaces A (a move), first stop A's bot and pause A
   (`docker compose -p farm-a stop bot`, then `farmcore effects pause` on A), then put the same
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME` and `TELEGRAM_ALLOWED_USER_IDS` in C's env
   file. If both keep running, give C its own bot from @BotFather and its own `TELEGRAM_*`
   values; farmers send `/start` to the new bot. The farms' links to Telegram chats (written
   by `/start` on A) come with the data, so once effects are on, C's advice push writes to the
   same farmers' chats as A's did.
2. **Gemini on Vertex AI.** Set `EXTRACTOR` and `VERTEX_*` again and provide the service
   account key file (a new key for C is better than a copy). Projects started from one
   checkout share the default key path `./secrets/vertex.json`: give each installation its own
   with `VERTEX_CREDENTIALS_FILE` in its env file.
3. **Voice.** `WHISPER_MODEL`; the model downloads again into C's `whisper-cache`.
4. Enable effects on C, then start the bot: `docker compose --profile telegram up -d`.

## Upgrading

```sh
git pull
docker compose up -d --build     # `migrate` runs once; app and bot start after it
```

With `FARMCORE_IMAGE` set, do not `--build`: tag the new release and point `FARMCORE_IMAGE` at
it (see "Running several installations"), then `docker compose up -d`.

Take a backup first. Migrations form one linear chain and run forward only.

From a release whose compose file had `name: farmcore` (app and bot connected as the owner and
migrated on start):

1. Keep the old volumes: add `COMPOSE_PROJECT_NAME=farmcore` to `.env` (volumes were
   `farmcore_pgdata` and `farmcore_whisper-cache`).
2. Add `RUNTIME_DATABASE_PASSWORD` to `.env`.
3. Dumps now go to the `backups` volume; set `BACKUP_DIR=./backups` to keep the old directory.
4. Remove the old `backup` container with its anonymous volume (the PostGIS image's data
   directory; it holds nothing): `docker compose rm -sfv backup`. A plain `up` would carry
   that volume over into the new container.
5. If that release's `backup` service wrote plain `.dump` files, copy them first (step 1 of the
   next list). Then `docker compose up -d --build`. Cookie names now carry the installation, so
   browsers no longer send their old cookies and everyone signs in once more in the browser.
   The sessions themselves stay valid in the database until they expire; only the cookie name
   changed.

From a release whose `backup` service wrote plain `.dump` files (do step 1 before anything
else in the list above):

1. **Before upgrading**, copy the old `.dump` files somewhere safe: they are not encrypted, and
   the new service deletes them with the bundles once they are seven days old.
   `docker compose cp backup:/backups ./old-backups` copies the whole directory.
2. Create the owner's key and set `BACKUP_RECIPIENT` (see "The owner's key"). Until it is set,
   no backup is written at all.
3. Upgrade (`docker compose up -d --build`). The service now runs as uid 10001, not root. Give
   it the existing directory once (as root inside the `backup` container, `chown -R
   10001:10001 /backups`), then restart it:

   ```sh
   docker compose run --rm --no-deps --user root --entrypoint chown backup -R 10001:10001 /backups
   docker compose restart backup
   ```

`farmcore restore` refuses an old `.dump` file and points here. To restore one by hand into a
new project's empty database (this runs the dump's SQL as the superuser: only do it with a
dump you made and kept yourself):

```sh
docker compose -p farm-c --env-file farm-c.env up -d --wait db
docker compose -p farm-c --env-file farm-c.env exec -T db psql -U farmcore -d postgres \
  -v ON_ERROR_STOP=1 -c 'DROP DATABASE farmcore WITH (FORCE)' \
  -c 'CREATE DATABASE farmcore OWNER farmcore TEMPLATE template0'
docker compose -p farm-c --env-file farm-c.env cp ./old-backups/farmcore-X.dump db:/tmp/old.dump
docker compose -p farm-c --env-file farm-c.env exec -T db pg_restore -U farmcore -d farmcore \
  --no-owner --no-acl --exit-on-error --single-transaction /tmp/old.dump
docker compose -p farm-c --env-file farm-c.env exec -T db rm /tmp/old.dump
docker compose -p farm-c --env-file farm-c.env exec -T db psql -U farmcore -d farmcore \
  -v ON_ERROR_STOP=1 -c 'TRUNCATE local_session, login_bucket' \
  -c "ALTER DATABASE farmcore SET farmcore.external_effects = 'paused'"
docker compose -p farm-c --env-file farm-c.env up -d
```

The last `psql` drops the original's sessions and pauses effects, as `farmcore restore` does;
`migrate` (run by `up`) gives the copy a new `installation_id`.

## Running several installations

Each installation is the stock `docker-compose.yml` under its own project name and env file.
Nothing is shared between projects: not the containers, network, database, roles, secrets,
volumes (database, Whisper cache, backups), image tag or cookies.

```sh
cp .env.example farm-a.env    # own POSTGRES_PASSWORD, RUNTIME_DATABASE_PASSWORD, ADMIN_TOKEN,
cp .env.example farm-b.env    # PUBLIC_BASE_URL and FARMCORE_PORT (e.g. 8001); *.env is ignored
docker compose -p farm-a --env-file farm-a.env up -d --build
docker compose -p farm-b --env-file farm-b.env up -d --build
curl -fsS http://127.0.0.1:8000/health    # two different installation_id values
curl -fsS http://127.0.0.1:8001/health
```

Always pass both `-p` and `--env-file` (or set `COMPOSE_PROJECT_NAME` inside each env file):
without `--env-file`, Compose reads `./.env`, which belongs to one installation only. Each
installation needs its own `PUBLIC_BASE_URL` (links in bot messages and exports point there).
With `BACKUP_DIR` set, give each installation its own directory.

Without `FARMCORE_IMAGE`, each project builds and runs its own `farmcore-<project>:local`, and
rebuilding one never changes another. To run one release everywhere instead, build (or pull)
and tag it once, then name it in every env file:

```sh
docker build -t farmcore:0.2.0 .           # once per release, from that release's checkout
echo FARMCORE_IMAGE=farmcore:0.2.0 >> farm-a.env
docker compose -p farm-a --env-file farm-a.env up -d    # no --build
```

Never run `up --build` while `FARMCORE_IMAGE` is set: Compose would rebuild from the current
checkout and retag the shared release under every installation that uses it. Upgrade by
tagging a new release (`farmcore:0.3.0`) and changing `FARMCORE_IMAGE` one installation at a
time.

What keeps them apart:

- **Database.** Each project has its own PostgreSQL server on its own network with no
  published port; a project's app cannot even resolve another project's `db`. Its runtime role
  can open no other database that existed at the last `migrate`, and change rows only (see
  above).
- **Cookies.** Browsers share cookies between ports of one host, so every cookie name carries
  the installation (`fc-<first 12 hex of installation_id>-<name>`, also for the notification
  read-state cookie the browser writes itself) and an installation ignores cookies that are
  not its own. Signing in to one never signs you in to, or out of, another, even with the same
  `ADMIN_TOKEN`. An app started on a database `migrate` never ran on logs a warning; on a
  network deployment (`FARMCORE_PUBLIC=1`) it then scopes cookies to the process (a restart
  signs everyone out) rather than share them.
- **Failure.** Stopping, upgrading or deleting one project leaves the others running.

Give each installation its own hostname behind the proxy (`farm-a.example.org`,
`farm-b.example.org`); serving two under different paths of one host is not supported.

### Idle cost of one installation

Measured on 2026-10-20 with the stock compose file and its defaults (replay clock, regex
extractor, no bot, no demo data), after `/health` was healthy and 60 s of settling:

| | RAM | CPU (one core) | Disk |
|---|---|---|---|
| `app` | 102 MiB | 0.5 % average, 8 % peak | |
| `db` (PostGIS) | 52 MiB | 0.3 % average, 4 % peak | 109 MiB (`pgdata`, freshly migrated) |
| `backup` | 1 MiB | 0 % | 12 KiB (one dump of the empty database) |
| **Installation, Whisper not loaded** | **155 MiB** | **0.9 %** | **109 MiB** + image |
| Whisper `small` loaded (int8, CPU) | +520 MiB (676 MiB total) | 1.4 % total | +464 MiB (`whisper-cache`) |
| Image (`farmcore`, shared by installations on one release) | | | 948 MB once per host |

Whisper loads in the process that first transcribes a voice note (the bot, or the web app for
voice drafts) and then stays resident, so budget the second line for an installation that uses
voice. The `migrate` container exits after start and costs nothing while idle.

Method, to repeat on your hardware: start one project with fresh secrets, wait until `app` is
healthy, wait 60 s, then take 24 `docker stats --no-stream` samples 5 s apart (2 minutes) and
average them per container; disk is `du -sk` of each volume and `docker image inspect -f
'{{.Size}}'`. For the Whisper line, start a resident Python process in `app` that loads
`WhisperModel("small", device="cpu", compute_type="int8")` (what the transcriber does), wait
30 s, and sample again. The numbers above come from Docker Desktop on Apple silicon (arm64,
10 CPUs, 8 GiB VM, `imresamu/postgis:17-3.5`); amd64 Linux hosts are not measured yet.

## Configuration reference

Every variable is listed with its default in [.env.example](../.env.example) and
`src/farmcore/settings.py`.
