#!/usr/bin/env bash
# Build the optional oracle containers and regenerate the stored fixtures.
#
# Usage: tools/oracles/run.sh [oracle...]   (default: degday viticolr plasmopy)
# Writes tests/fixtures/oracles/<oracle>/*.json; the downy mildew oracles read the synthetic
# season in tests/fixtures/oracles/inputs/, rewritten first by inputs/make_season.py.
# Builds one image at a time. Never runs in CI; the images are never pushed.
set -euo pipefail
cd "$(dirname "$0")/../.."

fixtures=tests/fixtures/oracles
oracles=("$@")
((${#oracles[@]})) || oracles=(degday viticolr plasmopy)

mkdir -p "$fixtures/inputs"
python3 tools/oracles/inputs/make_season.py "$fixtures/inputs/season-2025.csv"

for oracle in "${oracles[@]}"; do
  case "$oracle" in
    degday) cmd=(Rscript degday/generate.R /out) ;;
    viticolr) cmd=(Rscript viticolr/generate.R /in/season-2025.csv /out) ;;
    plasmopy) cmd=(python3 plasmopy/generate.py /in/season-2025.csv /out) ;;
    *) echo "unknown oracle: $oracle" >&2; exit 2 ;;
  esac
  image="farmcore-oracles-$oracle:local"
  docker build --target "$oracle" -t "$image" tools/oracles
  out="$fixtures/$oracle"
  rm -rf "$out"
  mkdir -p "$out"
  docker run --rm --network none --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$PWD/$fixtures/inputs:/in:ro" -v "$PWD/$out:/out" "$image" "${cmd[@]}"
done
