#!/usr/bin/env bash
# Build the optional oracle container and regenerate the stored fixtures.
#
# Usage: tools/oracles/run.sh [oracle...]   (default: every oracle; known: degday)
# Writes tests/fixtures/oracles/<oracle>/*.json. Never runs in CI; the image is never pushed.
set -euo pipefail
cd "$(dirname "$0")/../.."

image=farmcore-oracles:local
oracles=("$@")
((${#oracles[@]})) || oracles=(degday)

docker build -t "$image" tools/oracles

for oracle in "${oracles[@]}"; do
  case "$oracle" in
    degday) cmd=(Rscript degday/generate.R /out) ;;
    *) echo "unknown oracle: $oracle" >&2; exit 2 ;;
  esac
  out="tests/fixtures/oracles/$oracle"
  rm -rf "$out"
  mkdir -p "$out"
  docker run --rm --network none --user "$(id -u):$(id -g)" \
    -v "$PWD/$out:/out" "$image" "${cmd[@]}"
done
