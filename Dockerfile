# FarmCore web app and Telegram bot: one image, two commands (see docker-compose.yml).
# Secrets never enter the image: they come from the environment at run time.
# Pinned (tag + digest) so rebuilds get the same Python and Debian.
FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

COPY --from=ghcr.io/astral-sh/uv:0.12.16 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/home/farmcore/.cache/huggingface \
    DATA_DIR=/app/sample-data

# `farmcore backup` / `restore`: pg_dump and pg_restore (Debian's PostgreSQL 17, the server's
# major) and age for the bundle's encryption. /backups is where the compose `backup` service
# writes; a new volume mounted there inherits its owner.
RUN apt-get update \
    && apt-get install -y --no-install-recommends age postgresql-client \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 farmcore \
    && mkdir -p "$HF_HOME" \
    && chown -R farmcore:farmcore /home/farmcore/.cache \
    && install -d -o farmcore -g farmcore -m 700 /backups

WORKDIR /app

# Dependencies first (cached layer), then the project itself as a regular (non-editable) install.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE NOTICE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable
# Open demo data (IGN parcels, Météo-France station, Open-Meteo runs) for `farmcore seed`.
COPY sample-data ./sample-data

USER farmcore
EXPOSE 8000
# Serve only: migrations run in the one-shot `migrate` service (`farmcore migrate`, as the
# database owner) before the app starts. The `migrate` and `bot` services override this command.
# Behind a reverse proxy, set FORWARDED_ALLOW_IPS (read by uvicorn) to the proxy's address.
CMD ["uvicorn", "farmcore.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
