# Self-hosting

`docker-compose.yml` is one installation: five services, one image, one Compose project.

| Service | What |
|---|---|
| `db` | PostgreSQL 17 + PostGIS; no published port |
| `migrate` | One-shot `farmcore migrate` as the database owner, then exits; see below |
| `app` | The web app on `127.0.0.1:8000`; starts after `migrate` exits 0, never migrates |
| `bot` | Telegram long-polling bot; only with `--profile telegram`; also waits for `migrate` |
| `backup` | Daily `pg_dump` into the `backups` volume, checked with `pg_restore --list`, kept 7 days |

The database has two roles. `farmcore` (password `POSTGRES_PASSWORD`) owns the database, the
schema and the PostGIS extension; only `migrate` and `backup` hold it. `farmcore_app`
(password `RUNTIME_DATABASE_PASSWORD`) is what the app, the bot and `docker compose exec app
farmcore ...` use: it reads and writes rows and nothing else. It cannot create databases,
roles, extensions, schemas, tables or temporary tables, alter or drop tables, or write
`alembic_version`; it can connect to this installation's database only.

`migrate` upgrades the schema to head, gives the installation its identity on first run, and
creates `farmcore_app` (or resets its password and grants) every time. The identity,
`installation_id`, is a random UUID the server generates once; nobody chooses it. It is stored
as a setting of the database (not a table, so no schema change), survives restarts and
upgrades, and is reported by `/health`. A copy or restore of the database gets a new one at the
next `migrate`. It names the installation, never a farm: one installation holds many farms.

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
docker compose restart app bot                                     # app switches apply at start
```

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
clock) it runs with no outbound network at all. Live weather (`CLOCK_MODE=live`) fetches
public forecasts.

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

Take a backup first. Migrations form one linear chain and run forward only.

From a release whose compose file had `name: farmcore` (app and bot connected as the owner and
migrated on start):

1. Keep the old volumes: add `COMPOSE_PROJECT_NAME=farmcore` to `.env` (volumes were
   `farmcore_pgdata` and `farmcore_whisper-cache`).
2. Add `RUNTIME_DATABASE_PASSWORD` to `.env`.
3. Dumps now go to the `backups` volume; set `BACKUP_DIR=./backups` to keep the old directory.
4. `docker compose up -d --build`. Cookie names now carry the installation, so everyone signs in
   once more.

## Running several installations

Each installation is the stock `docker-compose.yml` under its own project name and env file.
Nothing is shared between projects: not the containers, network, database, roles, secrets,
volumes (database, Whisper cache, backups), image tag or cookies.

```sh
cp .env.example farm-a.env    # own POSTGRES_PASSWORD, RUNTIME_DATABASE_PASSWORD, ADMIN_TOKEN
cp .env.example farm-b.env    # ... and its own FARMCORE_PORT, e.g. 8001
docker compose -p farm-a --env-file farm-a.env up -d --build
docker compose -p farm-b --env-file farm-b.env up -d --build
curl -fsS http://127.0.0.1:8000/health    # two different installation_id values
curl -fsS http://127.0.0.1:8001/health
```

Always pass both `-p` and `--env-file` (or set `COMPOSE_PROJECT_NAME` inside each env file):
without `--env-file`, Compose reads `./.env`, which belongs to one installation only. To run
the same release everywhere, build or pull it once and set `FARMCORE_IMAGE` to its tag in every
env file; otherwise each project builds `farmcore-<project>:local`, and rebuilding one project
never changes another. With `BACKUP_DIR` set, give each installation its own directory.

What keeps them apart:

- **Database.** Each project has its own PostgreSQL server on its own network with no
  published port; a project's app cannot even resolve another project's `db`. Its runtime role
  can open only its own database and change rows only (see above).
- **Cookies.** Browsers share cookies between ports of one host, so every cookie name carries
  the installation (`fc-<first 12 hex of installation_id>-<name>`) and an installation ignores
  cookies that are not its own. Signing in to one never signs you in to, or out of, another,
  even with the same `ADMIN_TOKEN`. Exception: the notification read-state cookie that the
  browser writes itself keeps its plain name for now, so two installations on one host share
  which notifications look read.
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
