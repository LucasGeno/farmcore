# Self-hosting

`docker-compose.yml` runs four services from one image:

| Service | What |
|---|---|
| `db` | PostgreSQL 17 + PostGIS; no published port |
| `app` | The web app on `127.0.0.1:8000`; runs `farmcore migrate` on start |
| `bot` | Telegram long-polling bot; only with `--profile telegram` |
| `backup` | Daily `pg_dump` into `./backups`, checked with `pg_restore --list`, kept 7 days |

Read [SECURITY.md](../SECURITY.md) before exposing an installation to a network.

## 1. Configure

```sh
cp .env.example .env
```

Set at least:

- `POSTGRES_PASSWORD`: `openssl rand -hex 32`
- `ADMIN_TOKEN`: `openssl rand -hex 24` (at least 32 characters, no whitespace)
- `PUBLIC_BASE_URL`: the HTTPS address users open

The compose file always sets `FARMCORE_PUBLIC=1`: the app refuses to start without a strong
`ADMIN_TOKEN`, and the interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are off.
Without that flag (local development) and without a token, every visitor is administrator.

On arm64 hosts set `POSTGIS_IMAGE=imresamu/postgis:17-3.5`.

## 2. Start

```sh
docker compose up -d --build
docker compose ps                                   # app healthy; db, backup up
curl -fsS http://127.0.0.1:8000/health              # {"db":"ok"}
```

Optional demo data (the open-data Saint-Émilion estate, replaying 2024):

```sh
docker compose exec app farmcore seed --scene B
docker compose exec app farmcore replay --to "2024-04-10 06:00"
```

The administrator signs in at `/admin/sign-in` with `ADMIN_TOKEN`; the demo controls are at
`/demo/`. Share `/tour` for a read-only walkthrough that refuses every change.

## 3. Reverse proxy and TLS

Serve FarmCore over HTTPS only: the admin cookie is `Secure`. Keep the app bound to
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
ls backups/
```

Restore a dump into a fresh database:

```sh
docker compose stop app bot
docker compose exec -T db psql -U farmcore -d postgres -v ON_ERROR_STOP=1 \
  -c 'DROP DATABASE farmcore WITH (FORCE)' -c 'CREATE DATABASE farmcore OWNER farmcore'
docker compose exec -T db pg_restore -U farmcore -d farmcore --no-owner < backups/<file>.dump
docker compose up -d
```

Never run `docker compose down -v` on an installation with data: it deletes the database
volume.

## Upgrading

```sh
git pull
docker compose up -d --build     # the app migrates on start; the bot waits for it
```

Take a backup first. Migrations form one linear chain and run forward only.

## Configuration reference

Every variable is listed with its default in [.env.example](../.env.example) and
`src/farmcore/settings.py`.
