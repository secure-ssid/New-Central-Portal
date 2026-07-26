# Changelog

Notable changes to the New Central Portal. Newest first. See
`docs/DEEP_DIVE_AUDIT.md` for the full audit these fixes came out of.

## 2026-07-25

### Fixed — configuration & API audit batch

- **Site / device-type filtering** now happens client-side. Central `400`s on
  the `?siteId=` and `?deviceType=` query filters, so the site page showed
  "0 devices", the topology site filter showed no nodes, and the site-health
  card was blank. The fields are present on every unfiltered record; the fleet
  is small enough that filtering in Python is free and reuses the cached list.
- **Firmware compliance** verdicts are correct in both directions. A device
  with no version data no longer counts as "compliant", and one with a known
  target but null current no longer counts as "drift" — both are now `unknown`,
  a separate bucket. `/platform/config` and `/lab/compliance` agree.
- **`/healthz` returns 503** when this worker knows of no scheduler leader, so a
  portal with no background jobs (no device-down alerting) no longer reports
  healthy. A follower still returns 200.
- **Database connection pool** holds `minconn = maxconn = 10` instead of 1
  (psycopg2 closed every connection above `minconn`, making it a pool of one —
  ~100× slower checkout under concurrency). `DATABASE_URL` query parameters are
  now honoured, and connections carry a connect timeout, `application_name` and
  TCP keepalives.
- **`/wlans/`** unwraps the `essid` object (it arrives as `{"name": …}`, not a
  string — a raw Python dict was reaching the page), reads the VLAN from
  `vlan-id-range`, and reads the real `enable` flag.
- **`/platform/nac`** maps the real MAC-registration fields (`displayName`,
  `enable`, `staticTags`) instead of `description`/`status`/`role`, which the
  payload does not contain — three columns of em-dashes before.
- **GreenLake service catalog** reads its rows from `data.items`; it was always
  empty and blamed missing API access for a call that succeeds.
- **Running-config viewer** (`/platform/config`) works: it runs
  `show running-config` over the ops CLI (its previous endpoint 404s and it was
  dumping a raw Python dict with internal API paths), and both config pages mask
  credential material (`password`/`key ciphertext`, SNMP community,
  WPA passphrase) before rendering.
- **Tailwind** falls back to the Play CDN when the built stylesheet is missing,
  so a fresh clone or rebuild renders styled instead of unstyled.

### Added

- **Application Visibility** Lab tool (`/lab/app-visibility`) — DPI application
  traffic, Aruba risk classification, and a per-client drilldown.
- **Compliance Board** promoted to the main Operations nav; **Device Deep-Dive**,
  **Application Visibility** and **Activity & History** linked from the device,
  client and alerts pages respectively.
