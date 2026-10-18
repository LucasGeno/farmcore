#!/usr/bin/env bash
# One check before every commit: data guard, ruff lint + format, pytest (args pass through).
set -euo pipefail
cd "$(dirname "$0")/.."

# Data guard: raw datasets, spreadsheets, archives and credentials are never tracked or staged.
if leaked=$(git ls-files | grep -E '^(data|secrets|backups)/|\.(xlsx|zip|gz|dump)$|(^|/)\.env$'); then
  echo "data guard: these tracked/staged files must not be committed:" >&2
  echo "$leaked" >&2
  exit 1
fi

if ! (exec 3<>/dev/tcp/127.0.0.1/55442) 2>/dev/null; then
  echo "hint: dev PostGIS is not reachable on localhost:55442;" \
    "start it with: docker compose -f docker-compose.dev.yml up -d db" >&2
fi

uv run ruff check . && uv run ruff format --check . && uv run pytest -q "$@"
