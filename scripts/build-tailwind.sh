#!/usr/bin/env bash
# Pre-build Tailwind CSS for production (set USE_BUILT_TAILWIND=1 in .env).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if ! command -v npx >/dev/null 2>&1; then
  echo "npx not found — install Node.js to build Tailwind CSS" >&2
  exit 1
fi
# Run from app/ so the `content` globs in tailwind.config.js resolve.
cd app
mkdir -p static/dist
npx --yes tailwindcss@3.4.10 \
  -i static/input.css \
  -o static/dist/tailwind.css \
  --minify \
  --config tailwind.config.js

# The theme lives in static/tailwind-theme.js; if the config is not picked up
# the build silently emits a stylesheet with no brand-*/surface-* utilities
# while the templates still reference them. Fail loudly instead.
if ! grep -q 'surface-700' static/dist/tailwind.css; then
  echo "ERROR: built CSS has no surface-* utilities — tailwind.config.js was not applied" >&2
  exit 1
fi
echo "Wrote app/static/dist/tailwind.css"
