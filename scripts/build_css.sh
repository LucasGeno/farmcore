#!/usr/bin/env bash
# Build committed CSS on a development machine; the Python image needs no Node/npm.
set -euo pipefail
cd "$(dirname "$0")/.."
npx --yes tailwindcss@3.4.17 --config tailwind.config.js \
  --input scripts/tailwind.css --output src/farmcore/core/static/vendor/tailwind-3.4.17.min.css \
  --minify
