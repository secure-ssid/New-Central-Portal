#!/usr/bin/env bash
# Pre-build Tailwind CSS for production (set USE_BUILT_TAILWIND=1 in .env).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if ! command -v npx >/dev/null 2>&1; then
  echo "npx not found — install Node.js to build Tailwind CSS" >&2
  exit 1
fi
mkdir -p app/static/dist
npx --yes tailwindcss@3.4.10 \
  -i app/static/input.css \
  -o app/static/dist/tailwind.css \
  --minify \
  --content "app/templates/**/*.html"
echo "Wrote app/static/dist/tailwind.css"
