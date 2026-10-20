# Self-hosting

`docker-compose.yml` is one installation: five services, one image, one Compose project.

| Service | What |
|---|---|
| `db` | PostgreSQL 17 + PostGIS; no published port |
| `migrate` | One-shot `farmcore migrate` as the database owner, then exits; see below |
| `app` | The web app on `127.0.0.1:8000`; starts after `migrate` exits 0, never migrates |
| `bot` | Telegram long-polling bot; only with `--profile telegram`; also waits for `migrate` |
| `backup` | Daily `pg_dump` into the `backups` volume, checked with `pg_restore --list`, kept 7 days |

The database has two roles. `farmcore` (password `POSTGRES_PASSWORD`) is the bootstrap
superuser of this installation's PostgreSQL server (the image's `POSTGRES_USER`); it owns the
`farmcore` database, its schema and the PostGIS extension. Only `migrate` and `backup` hold its
password. `farmcore_app` (password `RUNTIME_DATABASE_PASSWORD`) is what the app, the bot and
`docker compose exec app farmcore ...` use: it reads and writes rows and nothing else. It
cannot create databases, roles, extensions, schemas, tables or temporary tables, alter or drop
tables, write `alembic_version`, or become another role. It can connect to the `farmcore`
database only: as a superuser, `migrate` also revokes the default `CONNECT` and `TEMPORARY`
rights of every role (`PUBLIC`) on the server's `postgres`, `template1` and `template_postgis`
databases. That change is server-wide, which suits the dedicated server each Compose project
runs. On a server shared with other software, make the owner an ordinary role instead (with
`CREATEROLE`, owning the database and its `public` schema; a superuser creates the PostGIS
extension): `migrate` then changes only what that role owns and leaves the server's databases
alone. A superuser must first allow it to store the installation id, once:
`GRANT SET ON PARAMETER farmcore.installation_id TO <owner>` (`migrate` says so if missing).

One right PostgreSQL gives every role and cannot take away: creating large objects
(`lo_create`). FarmCore uses none; the runtime role could store data that way, inside this
installation's database only, which it can already do in rows. A large object it owns makes
the next `migrate` refuse until it is reassigned to the owner.

`migrate` upgrades the schema to head, gives the installation its identity on first run, and
creates `farmcore_app` every time or resets it: password and attributes; every role
membership, whoever granted it; role settings; and every privilege granted to it directly,
which it then grants again as described above. It refuses a `farmcore_app` that owns any
object, and refuses to run as any role other than the database owner, so
`docker compose exec app farmcore migrate` (the runtime role) fails with an explanation; use
`docker compose run --rm migrate`. The password reaches PostgreSQL only as a SCRAM verifier.

The identity, `installation_id`, is a random UUID the server generates once; nobody chooses
it. It is stored as a setting of the database (not a table, so no schema change), survives
restarts and upgrades, and is reported by `/health`. It names the installation, never a farm:
one installation holds many farms. Whatever carries database settings keeps it: a copy of the
`pgdata` volume, `pg_dumpall`, or `pg_dump --create` restored with `pg_restore --create`.
Whatever does not gets a new one at the next `migrate`: `CREATE DATABASE ... TEMPLATE`, or a
plain `pg_dump` restored into a new database (the procedure under "Backups and restore").
A copy that keeps the id is the same installation: never run it next to the original on one
host, or the two share cookie names.

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

```sh
docker compose run --rm --no-deps -e BACKUP_ONCE=1 backup     # one backup now
docker compose run --rm --no-deps --entrypoint ls backup -l /backups
```

Dumps live in this project's `backups` volume, or in `BACKUP_DIR` when set (give every
installation its own directory). Restore a dump into a fresh database:

```sh
docker compose stop app bot
docker compose exec -T db psql -U farmcore -d postgres -v ON_ERROR_STOP=1 \
  -c 'DROP DATABASE farmcore WITH (FORCE)' -c 'CREATE DATABASE farmcore OWNER farmcore'
docker compose run --rm --no-deps --entrypoint pg_restore backup \
  -h db -U farmcore -d farmcore --no-owner /backups/<file>.dump
docker compose up -d             # `migrate` re-grants the runtime role
```

The restored database gets a new `installation_id`, so members sign in again.

Never run `docker compose down -v` on an installation with data: it deletes the database
volume.

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
5. `docker compose up -d --build`. Cookie names now carry the installation, so everyone signs in
   once more.

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
  can open only its own database and change rows only (see above).
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
