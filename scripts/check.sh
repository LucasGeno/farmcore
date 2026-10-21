#!/usr/bin/env bash
# One check before every commit: data guard, ruff lint + format, pytest.
#
# Usage: scripts/check.sh [--all] [pytest args...]
#   (default)  pytest -m 'not data and not slow': the fast set for the inner loop
#   --all      every test, including the `data` replays and the `slow` Compose journeys;
#              run it once before merge
#   An explicit -m in the pytest args or in PYTEST_ADDOPTS wins over both.
#   Other args pass through to pytest unchanged.
set -euo pipefail
cd "$(dirname "$0")/.."

usage() { sed -n '4,9p' "$0" | sed 's/^# \{0,1\}//'; }

all=0
args=()
for arg in "$@"; do
  case "$arg" in
    --all) all=1 ;;
    -h | --help) usage; exit 0 ;;
    *) args+=("$arg") ;;
  esac
done

# Data guard: raw datasets, spreadsheets, archives and credentials are never tracked or staged.
# Env files: `.env`, `.env.*`, per-installation `<name>.env` and `<name>.env.*`; only `.env.example`
# is committed.
if leaked=$(git ls-files | grep -E '^(data|secrets|backups)/|\.(xlsx|zip|gz|dump)$|\.env(\.[^/]*)?$' \
  | grep -vE '(^|/)\.env\.example$'); then
  echo "data guard: these tracked/staged files must not be committed:" >&2
  echo "$leaked" >&2
  exit 1
fi

if ! (exec 3<>/dev/tcp/127.0.0.1/55442) 2>/dev/null; then
  echo "hint: dev PostGIS is not reachable on localhost:55442;" \
    "start it with: docker compose -f docker-compose.dev.yml up -d db" >&2
fi

explicit_m=0
for arg in ${args[@]+"${args[@]}"}; do
  case "$arg" in -m* | --markexpr*) explicit_m=1 ;; esac
done
if [[ " ${PYTEST_ADDOPTS:-} " =~ [[:space:]](-m|--markexpr) ]]; then
  explicit_m=1
fi

select=()
if ((explicit_m)); then
  echo "check: pytest selection: the explicit -m given" >&2
elif ((all)); then
  echo "check: pytest selection: all tests (--all)" >&2
else
  select=(-m 'not data and not slow')
  echo "check: pytest selection: -m 'not data and not slow' (--all runs everything)" >&2
fi

uv run ruff check . && uv run ruff format --check . \
  && uv run pytest -q ${select[@]+"${select[@]}"} ${args[@]+"${args[@]}"}
