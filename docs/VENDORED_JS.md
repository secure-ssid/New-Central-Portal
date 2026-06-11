# Vendored JavaScript dependencies


All runtime JS dependencies are now served from `app/static/vendor/` and the
three templates (`base.html`, `topology.html`, `devices/detail.html`) point at
`/static/vendor/...`. No `main.py` change required (mount already exists).

| File | Version | Source |
| --- | --- | --- |
| `htmx-2.0.3.min.js` | 2.0.3 | npm tarball `htmx.org` (registry.npmjs.org, official) |
| `alpinejs-3.15.12.min.js` | 3.15.12 (pinned; template previously floated `3.x.x`) | npm tarball `alpinejs` (official) |
| `3d-force-graph-1.80.0.min.js` | 1.80.0 | npm tarball `3d-force-graph` (official) |
| `three-0.160.0.module.js` | 0.160.0 | npm tarball `three` (official, `build/three.module.js`) |
| `three-addons-0.160.0/controls/OrbitControls.js` | 0.160.0 | npm tarball `three` (`examples/jsm/controls/`) |
| `tailwindcss-play-3.4.10.js` | 3.4.10 | npm tarball `tailwindcss-cdn` (**unofficial mirror** — see note) |

Notes / caveats:

- The sandbox proxy allowlist blocks `cdn.tailwindcss.com`, `unpkg.com`,
  `cdn.jsdelivr.net` and `cdnjs.cloudflare.com` (`403 Host not in allowlist`);
  only `registry.npmjs.org` (and raw.githubusercontent.com) were reachable.
  The Tailwind **Play CDN** build is not published on npm by Tailwind Labs, so
  the file was taken from the `tailwindcss-cdn@3.4.10` npm package (a mirror
  of the official Play CDN script, maintainer `fondoger`). It was verified to
  be the genuine Play build (same structure, `window.tailwind` config API,
  only documentation URLs inside, passes `node --check`, 366 KB). If policy
  prefers an official source, replace
  `app/static/vendor/tailwindcss-play-3.4.10.js` with a fresh download of
  `https://cdn.tailwindcss.com/3.4.10` from a trusted network — the filename
  and template reference can stay the same.
- The `tailwind.config = {...}` inline block in `base.html` is the v3 Play
  API and works unchanged with the vendored script.
- Google Fonts `<link>` tags (Inter) in `base.html` were intentionally left
  on the CDN: they are cosmetic and degrade gracefully to the system font
  stack when offline (the JS app no longer breaks without internet).
- The `three` importmap in `devices/detail.html` now maps
  `"three" -> /static/vendor/three-0.160.0.module.js` and
  `"three/addons/" -> /static/vendor/three-addons-0.160.0/`;
  `OrbitControls.js` only imports the bare specifier `'three'` (verified), so
  the importmap resolves everything locally.

