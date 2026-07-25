# Vendored JavaScript dependencies


All runtime JS dependencies are now served from `app/static/vendor/` and the
three templates (`base.html`, `topology.html`, `devices/detail.html`) point at
`/static/vendor/...`. No `main.py` change required (mount already exists).

| File | Version | Source |
| --- | --- | --- |
| `htmx-2.0.3.min.js` | 2.0.3 | npm tarball `htmx.org` (registry.npmjs.org, official) |
| `alpinejs-3.15.12.min.js` | 3.15.12 (pinned; template previously floated `3.x.x`) | npm tarball `alpinejs` (official) |
| `3d-force-graph-1.80.0.min.js` | 1.80.0 | npm tarball `3d-force-graph` (official) |
| `three-0.183.2/three.module.min.js` + `three.core.min.js` | 0.183.2 | official three.js release |
| `three-addons-0.160.0/controls/OrbitControls.js` | 0.160.0 | npm tarball `three` (`examples/jsm/controls/`) |
| `fonts/inter-latin.woff2`, `fonts/inter-latin-ext.woff2` | Inter v20 (variable) | Google Fonts `css2` API, latin + latin-ext subsets |
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
- **Inter is now self-hosted** (`app/static/fonts/`), loaded via
  `/static/fonts/inter.css` from `base.html` and `login.html`. The Google
  Fonts `<link>`s cost two extra DNS + TLS handshakes on the critical render
  path, made the styling depend on outbound internet, and added an origin the
  production reverse proxy has to allow. Inter v20 is a variable font and
  Google returns the same URL for every weight in a subset, so one file per
  subset covers 400-800 via a `font-weight: 100 900` range. Only latin and
  latin-ext are vendored; the cyrillic/greek/vietnamese subsets were 25 more
  files nothing in this app renders.
- **three.js is deduplicated on 0.183.2.** The app used to ship two full
  engines: topology on 0.183.2 and `devices/detail.html` on 0.160.0
  (1.27 MB), ~2 MB of near-identical code. The importmap in
  `devices/detail.html` now maps
  `"three" -> /static/vendor/three-0.183.2/three.module.min.js` with
  `"three/addons/" -> /static/vendor/three-addons-0.160.0/`.
  `OrbitControls.js` imports only the bare specifier `'three'`, and the ten
  symbols it pulls in (`EventDispatcher`, `MOUSE`, `Quaternion`, `Spherical`,
  `TOUCH`, `Vector2`, `Vector3`, `Plane`, `Ray`, `MathUtils`) are all verified
  present in 0.183.2's module exports. The page keeps its WebGL feature-detect
  and `#ports-fallback` plain port grid if the 3D view fails to boot.
- The importmap now lives in `{% block head %}` rather than mid-`<main>`: an
  importmap must be parsed before any module script that uses it.

