# Configuration & API Deep-Dive Audit — 2026-07-25

A full read-mostly audit of every configuration surface and the entire Central
API integration layer, run as nine parallel auditors (runtime config,
deployment, database, secret handling, all 83 `central_bridge` wrappers,
centralmcp tool coverage, Central config APIs, the portal HTTP surface, and
background jobs), each independently re-checked by an adversarial refuter that
was told to *refute* rather than confirm. Findings below survived that pass.

This is a working document: the **Fix status** table tracks what has since been
shipped. The body (Parts 1–7) is the synthesized report; Appendices A and B are
two dimensions recovered from the run journal that the synthesizer's input cap
dropped.

Numbers reflect a **single-user private LAN** deployment; security items are
ranked for that and marked with whether they only begin to matter once the
portal is shared beyond its owner.

---

## Fix status

| ID | Finding | Status | Commit |
|---|---|---|---|
| D2 | Site/device-type filter 400s → site page, topology filter, site-health all blank | **fixed** — client-side filter | `246d43f` |
| D3 | Firmware compliance verdict wrong both ways; two pages disagree 2× | **fixed** — shared rule + `unknown` bucket | `246d43f` |
| D6 | `/healthz` returns 200 while the scheduler is unelected (alerting off) | **fixed** — 503 when unelected | `246d43f` |
| D7 | Connection pool `minconn=1` → ~100× checkout latency | **fixed** — `minconn=maxconn=10` | `246d43f` |
| D8 | `_parse_dsn` drops the DSN query string; no connect timeout | **fixed** — merge params + timeouts/keepalives | `246d43f` |
| D1 | Built stylesheet is an untracked host artifact; a rebuild renders unstyled | **partly fixed** — CDN fallback when `dist/` absent | `246d43f` |
| D11 | `/wlans/` renders a raw dict, drops the VLAN, hardcodes enabled | **fixed** — unwrap essid, read `vlan-id-range`/`enable` | `246d43f` |
| — | `/platform/nac` three em-dash columns | **fixed** — map `displayName`/`enable`/`staticTags` | `246d43f` |
| D17 | GLP service-offer catalog always blank (wrong envelope key) | **fixed** — read `data.items` | `246d43f` |
| D12 | `/platform/config` "Fetch config" dumps a raw dict with internal API paths | **fixed** — `show running-config` via ops CLI, secrets masked | `794d793` |
| S13 | Running-config exposes `password ciphertext …` verbatim | **fixed** — `mask_config_secrets` on both config pages | `794d793` |

All other findings below are **open**. The Classic-Central and device-inventory
items are gated on the mutation experiments in Part 7, which require live writes
to settle.

# New Central Portal — Consolidated Deep-Dive

**Synthesis of nine area audits + adversarial re-check. Read the coverage note before trusting completeness.**

---

## 0. Coverage of this synthesis (read first)

I received **six** of the nine area reports:

| # | Area | Report | Refuter verdicts |
|---|---|---|---|
| 1 | Runtime configuration and environment | full | full (20 findings, all CONFIRMED, 9 severities corrected) |
| 2 | Deployment, container and proxy config | full | **absent** — findings below carry original severities and are marked ⚠︎unrefuted |
| 3 | Database schema, migrations, connections | full | full (18 findings, 17 CONFIRMED / 1 REFUTED) |
| 4 | central_bridge wrapper audit (83 wrappers) | full | full (18 findings, 17 CONFIRMED / 1 REFUTED) |
| 5 | centralmcp tool coverage | full | full (15 findings, all CONFIRMED, 8 severities corrected) |
| 6 | Central configuration APIs | full | **truncated mid-verdict** — findings retained, `needs_mutation_test` lost |
| 7–9 | *not delivered* | — | — |

Three areas are missing entirely. Anything they covered is absent here, and the "fix these first" list is drawn from six areas, not nine.

**Refuted and dropped:** `get_report_settings` RETURNING-cannot-fire (database) — Postgres does return the row when there is no conflict, verified in a rolled-back txn. `list_active_alerts returns empty = defect` (bridge) — all 30 alerts on the tenant are `status: Cleared`; the empty active list is *correct*, and the proposed fix (repoint the hub at `list_all_alerts`) would have shown 30 cleared alerts as live.

---

# PART 1 — WHAT EXISTS

## 1.1 Runtime shape

- **Compose services actually running:** `db`, `app`. Caddy is defined in `docker-compose.yml:70-83` but `profiles: ["disabled"]` in the override; **`netlab-caddy` has never existed in `docker ps -a`**. The app serves *no* security headers of its own.
- **App:** `new-central-portal-app` sha256:e03ea48c7663, uid 999:10, `0.0.0.0:8200 → 8000`, restart `unless-stopped`, RestartCount 0. **2 uvicorn workers** (`UVICORN_WORKERS=2`, Dockerfile + override; pids 8/9 confirmed).
- **Postgres:** pgvector/pgvector:pg16, **not published** (`ports: !override []` → `PortBindings: {}`; `docker port netlab-db` empty). `max_connections=100`, `statement_timeout=0`, `idle_in_transaction_session_timeout=0`, extensions: `plpgsql` only.
- **Mounts:** `./app→/app` **RW** (shadows the image's `/app` entirely), `/centralmcp` RO, `/claude-cfg` **RW**, `/fastembed-cache` RW (523 MB), `/home/choate85/.local/bin` RO (20+ binaries, not just `claude`).
- **Hardening present:** non-root uid, unpublished DB, disabled Caddy — **all three come only from the gitignored `docker-compose.override.yml`**.
- **Hardening absent:** no `mem_limit`/`cpus`/`pids_limit`, no `cap_drop`, no `no-new-privileges`, no log rotation (`LogConfig.Config: {}`, no daemon-level `log-opts`; ~10-12 MB/day).

## 1.2 Configuration surface

**`app/config.py` Settings — 10 fields.** Live values: `aruba_central_base_url` = apigw-prod2 host (**dead**), `aruba_central_access_token` = `your_token_here` (**dead**), `anthropic_api_key` / `github_token` = placeholders, `database_url` (**never read**), `device_check_interval_seconds`=60, `device_fetch_limit`=1000, `portal_password` (9 ch, real), `session_secret` (64 hex, real), `session_max_age_hours`=24.

**Env vars read directly, not declared in Settings (load-bearing, undocumented):** `DATABASE_URL`, `LOG_LEVEL`, `CENTRAL_RATE_LIMIT_INITIAL_DELAY` (main.py:54), `CENTRAL_MAX_CONCURRENCY` (central_bridge.py:117), `CLASSIC_CENTRAL_*`, `ASSISTANT_BACKEND`, `ASSISTANT_CLAUDE_MODEL`, `CLAUDE_BIN`, `CLAUDE_CODE_OAUTH_TOKEN`, `USE_BUILT_TAILWIND`, `CREDS_PATH`, `TOKEN_CACHE_DIR`, `FASTEMBED_CACHE_PATH`, `PYTHONPATH=/centralmcp`, `CENTRALMCP_GLP_V2BETA1_WRITES`.

**The real Central credential interface** is `CREDS_PATH` → `/centralmcp/config/credentials.yaml`, overridable by an entirely undocumented `SOURCE_*` / `TARGET_*` / `GLP_*` set (`/centralmcp/pipeline/config.py:50-79`, "Environment variables always win over YAML values"). Proven: 2297 log hits on `internal.api.central.arubanetworks.com`, **zero** on the `ARUBA_CENTRAL_BASE_URL` host.

**Not settable from `.env` at all** (compose never interpolates them): `USE_BUILT_TAILWIND`, `CENTRAL_MAX_CONCURRENCY`, `CENTRAL_RATE_LIMIT_INITIAL_DELAY`. Documenting them in `.env.example` would produce knobs that do nothing.

**DB-resident config** (`alert_settings`, 14 keys) takes precedence over env for `assistant_backend`, `assistant_model`, and the portal password.

**Hardcoded, not tunable:** `ALERT_FETCH_LIMIT=100`, `EVENT_FEED_LIMIT=10`, `_CACHE_TTL_SECONDS=60.0`, `_STALE_GRACE_SECONDS=600`, `_LOW_CONFIDENCE_TTL=5`, `_MAX_CACHE_ENTRIES=512`, `_DB_HASH_CACHE_TTL=10.0`, `LoginRateLimiter(10, 300)`, `CLAUDE_TIMEOUT_SECONDS=120`, `SCHEDULER_ELECTION_RETRY_SECONDS=60`.

## 1.3 Database

10 tables, 8,055 kB, one schema, 13 indexes (**all primary keys + 3 declared UNIQUE — zero secondary indexes**), `schema_version = 1` applied 2026-06-15.

| table | rows | writer | reader |
|---|---|---|---|
| `alert_settings` | 14 | `db.set_setting`, init seed | security, assistant, notifications |
| `alert_recipients` | 0 | routes/notifications | notifications sweep |
| `notifications_sent` | 0 | `record_notification` | `/notifications/` history |
| `device_status_history` | 0 (0 lifetime inserts in 40 d) | alerting sweep | `/lab/activity` |
| `device_status_snapshot` | 13 | sweep (429 updates ≈ 13×33 restarts) | `_seed_baseline` |
| `alert_rules` | 1 | routes/notifications | sweep (**58,623 seq_scans** ≈ 1/min since 2026-06-15 — independent proof the sweep runs) |
| `in_app_notifications` | 0 | sweep | bell poll, `/alerts` |
| `report_settings` | 1 | routes/notifications | summary report |
| `schema_version` | 1 | migrations | `_applied_version` |
| `audit_log` | 86 | `security.record_audit` | `/lab/activity` |

Pool: `ThreadedConnectionPool(1, 10)` under a double-checked lock; two raw non-pooled connects for the scheduler advisory lock. Advisory locks: `0x4E435031` scheduler (session-scoped, live, one leader), `0x4E435032` migrations (txn-scoped).

## 1.4 central_bridge — 83 wrappers

53 `@_cached()`, 30 undecorated (5 infrastructure, 2 internal helpers, 6 deliberately-uncached diagnostics, 14 write/click-to-run/RAG, **2 dead**).

Cache mechanics **verified sound**: all 52 probed wrappers served their second call with zero upstream; unhashable-arg guard works; `timeseries.window()` is properly quantised; exceptions are never cached; stale-while-revalidate serves immediately and refreshes behind the response.

Upstream-endpoint collisions (each pair issues a *byte-identical* GET under separate cache keys):

| endpoint | colliding wrappers |
|---|---|
| `/aps/{serial}/radios` | `get_ap_radios` + `get_channel_utilization` (both fired in one `gather` on every AP page) |
| `/network-services/v1alpha1/firmware-details` | `list_firmware_upgrades` + `get_firmware_compliance` |
| `/network-config/v1alpha1/config-health/devices` | `get_device_health` + `get_fleet_health` (+ `list_devices_config_health` with limit/offset) |
| `/device-inventory?limit=200` | `get_devices(limit=200)` + `_list_devices_page(200)` |
| `/network-config/v1/sites` | `get_sites` ×3 key variants, `find_site`, `get_display_sites` |
| `/wlan-ssids` | `list_wlans(200)` + `list_wlans(50)` |

## 1.5 centralmcp tool coverage

**The brief's "391 tools" is wrong for this deployment.** Measured by walking each FastMCP `_tool_manager`:

- **Reachable router index: 291 tools** (monitoring 77, config 75, glp 62, ops 40, nac 34, rag 3), of which **168 are read-only**. `CENTRALMCP_TOOLSETS` / `CENTRALMCP_PRODUCTS` are unset, so `central_generated`'s 1,347 tools are registered in code but **not indexed** (`'central_read_ap_system' in _tool_index` → False).
- **Total registered across all backends: 5,270** (mist 1,077, edgeconnect 1,265, clearpass 829, aos8 307, apstra 68, uxi 49, axis 25, central_generated 1,347).
- **The portal calls 60** (monitoring 32, ops 16, glp 5, config 3, rag 3, nac 1) = **21% of the reachable core**. One name in that list, `get_client_signal_history`, has zero references — actual bridge imports are 60 tools + 7 helpers.
- **All six optional vendor backends confirmed NOT APPLICABLE by calling** (`{"error": "<product> not configured"}`) — 3,571 tools of pure dead weight. **UXI is the exception:** the GLP workspace owns serial CNNCKYT02W, model UX-G6, deviceType SENSOR, no subscription — 49 tools dark on hardware the owner physically has.
- The **Lab MCP Tool Tester lists 13 tools but reaches all 168 read-only ones** via `tool_router.invoke_read_tool` fallback (verified live with `list_wlans`, `list_insights`, `list_device_groups`).

`mcp_servers/config.py` ships **75 tools** (26 read-only, 38 idempotent-write, 11 destructive). **21 of the 26 reads return real data on this tenant** — 550 scope-maps, 253 config-assignments, 38 roles, 67 gateway policies, 4 device groups, passpoint, ospfv2/vrf/telemetry profiles — and **only 3 reach the portal**.

## 1.6 Rate-limit characterisation (operational data)

- Every Central response: `x-ratelimit-limit: 10`, `x-ratelimit-reset: 1`. **No `Retry-After`, ever** (138/138 backoff lines).
- Backoff ladder is **5 → 7 → 10 s** (not 10.5 — `min(int(delay*1.5), max)` truncates), max 3 attempts, `central_client.py:49`/`:292`. Worst case ≈ 23.75 s of blocked thread.
- 6-hour 429 tallies (with eight agents active): `/network-troubleshooting/v1/events` 51-53 (**12-13%**), `/device-inventory` 35-36 (**8.7%**), `/config-health/devices` 11 (**13.6% — as bad as the other two; the bridge auditor's "only two endpoints rate-limit" claim is wrong, and three wrappers pile onto it**), `/alerts` 10, `/clients` 8, `tenant-client-health` 4.
- Config-plane endpoints (`/network-config/*`, `/network-services/*`) are **not** in the startup warm burst. 24 concurrent → 0 × 429; 40 concurrent → 10 × 429; 60 concurrent → ~32 × 429, 13.2 s wall, but all eventually 200.
- The portal itself is not a bottleneck: 12 parallel `GET /` all 200 in 57 ms wall; 200 concurrent `/healthz` all 200.
- **Deprecated endpoints past sunset** (`Sunset: Tue, 30 Jun 2026`, i.e. 25 days ago) still on hot paths: `v1alpha1/device-inventory` ×56/6h, `v1alpha1/firmware-details` ×16/6h, several `v1alpha1/switch/*`.

---

# PART 2 — WHAT IS WRONG

Ranked by severity × confidence × blast radius. Every entry names all areas it surfaced in. Severities are the refuters'.

---

### D1 · The served stylesheet is an untracked host artifact; a fresh clone or a rebuild renders the portal unstyled
**high** · areas: deployment, runtime-config · `app/Dockerfile:25`, `app/templates/base.html:10`, `app/templates_shared.py:14`

Three mechanisms compound:
1. `Dockerfile:4-15` builds Tailwind in a node stage and `:25` copies it in — but `./app:/app` **shadows the entire image `/app`**. Proven by md5: image `5ba3ebf4` (29,789 B), running container **and** live-served `43d434aa` (29,665 B) = the host file. The image's copy is 3 selectors *ahead* and missing `.list-disc`/`.pl-4` used by `lab/activity.html:75,81`.
2. `git ls-files app/static/` lists 20 files, none under `dist/`. `git check-ignore -v` → **`.git/info/exclude:7`** — a machine-local exclude, invisible to anyone reading the repo.
3. `Dockerfile:27 ENV USE_BUILT_TAILWIND=1` is hardcoded and **cannot be overridden from `.env`** (diffed `docker compose config` vs `docker inspect`: it is the only app-relevant key present in the container and absent from compose). `base.html:10-18` has **no fallback branch** — `asset_url()` swallows the OSError and emits a link to a file that 404s.

Net: clone the repo elsewhere, `docker compose up -d`, and every Tailwind utility is gone, with one 404 as the only signal. `docker compose build` cannot fix it.

**Fix:** add `os.path.exists()` to `templates_shared.py` so a missing dist file falls back to the Play-CDN branch; move the exclude into committed `.gitignore`; add `- USE_BUILT_TAILWIND=${USE_BUILT_TAILWIND:-1}` to the compose environment block. Pick one producer — either delete the Dockerfile tailwind stage, or stop shadowing it by mounting `./app/templates` and `./app/static/app.css` individually instead of all of `./app`.

---

### D2 · Every device-inventory *filter* 400s, so the site page, the site-filtered topology, and the site-health card are all permanently empty
**high** · areas: bridge-wrappers, central-config-api · `app/vendors/central_bridge.py:597`, `:786`

`get_devices(site_id=…)` → `?siteId=…` → **400** on both v1 and v1alpha1 → `[]`. `get_devices(device_type='AP')` → **400** → `[]`. Unfiltered returns 13.

Downstream, all reproduced live:
- `/sites/79244870000394240` → "Devices 0 / Online 0 / No devices at this site" beside "Clients 39".
- `/topology/?site=79244870000394240` → 0 nodes (unfiltered → 21). Also `/topology/?site=SecureSSID` → 0, and **that name form is the exact link the site page itself renders** ("View in topology").
- `get_site_health_summary` (line 786) spends 4 upstream calls — device-inventory 400, v1alpha1 400, clients 200, alerts-with-siteId 400 — to return `devices.total: 0, alerts.total: 0`. `_is_low_confidence` is **False** on that payload, so the wrong zeros cache for 60 s + 600 s grace. Its three consumers (`home.py:310-320`, `sites.py:131-136`) read `status`/`healthStatus`/`summary`, **none of which exist in the payload** → permanent em-dash on the dashboard card and the site page.

The fix is proven viable: unfiltered records carry `siteId` = `79244870000394240` and `siteName` = `SecureSSID`. Note `/clients` accepts `site-id` (hyphenated) and works — the divergence is a param-spelling difference, not a missing capability.

**Fix:** in `get_devices`, when `site_id` is set fetch unfiltered and filter on `d['siteId']` (fall back to `siteName`) in Python — 13 devices makes it free and the list is already cached. Same for `device_type` (`mcp_servers/monitoring.py:142-147` already has the client-side post-filter; it is unreachable because the request 400s first). Then either drop `get_site_health_summary` from `home.py`/`sites.py` or point the callers at `devices`/`clients`/`alerts`.

---

### D3 · Firmware compliance is wrong in both directions, contradicts the other firmware page, and rides an endpoint 25 days past its Sunset
**high** · areas: central-config-api, mcp-coverage · `app/vendors/central_bridge.py:1167`, `:1173`, `:1176`

Three defects in one call chain:

1. **Wrong verdict.** `"compliant" if (not target or current == target) else "non-compliant"`. `CNP6L2H02W` has `recommendedVersion: null, firmwareVersion: null, softwareVersion: null` — *no data at all* — and renders **compliant**. `LR-AP735`, `MB_635`, `Outsidefront` have `firmwareVersion: null` with a known recommendation and render **non-compliant** with an em-dash for Current. Live header: "13 devices / 2 compliant / 11 need attention". `/lab/compliance`, from the same upstream data, says "Firmware drift 5 of 9" because `lab.py:922` uses the careful rule `bool(recommended and current and recommended not in current and current not in recommended)`. **The two pages disagree by 2× and the authoritative-looking one is the wrong one.**
2. **Envelope violation.** `get_firmware_compliance` returns `{"items": [...]}`. Measured in-process: `_is_low_confidence([])` → True, `_is_low_confidence({"items": []})` → **False**. So the day this endpoint starts returning nothing, the empty result is cached as high-confidence for 60 s + 600 s per worker instead of 5 s.
3. **Past sunset.** `/network-services/v1alpha1/firmware-details` returns `deprecation: true`, `sunset: Tue, 30 Jun 2026`. It backs `/platform/config`, `/lab/compliance`'s firmware panel, and `config.get_firmware`. No replacement fleet-listing endpoint responded during the audit; the non-deprecated `/network-config/v1alpha1/firmware-compliance` is per-scope-id + device-function and returns an empty policy, so a fleet view needs a fan-out over `list_device_groups`.

**Fix:** lift `lab.py:922`'s rule into the wrapper, emit `"unknown"` when either side is blank, fall back to `softwareVersion`; return the bare list and move `[:limit]` inline (`platform.py:29 _normalize_firmware_compliance` already accepts a list); log an operational alarm when the deprecation header is present (`CentralClient.last_deprecation` at `central_client.py:179-188` already exists).

---

### D4 · The portal's only two config-write paths report `ok: true` when Central rejects them with 401
**high** · areas: central-config-api, bridge-wrappers · `app/routes/devices.py:718`, `:736`

`move_device_to_group` (`central_bridge.py:1316`) and `assign_device_to_site` (`:1329`) build `{"status_code": r.status_code, "response": body}` and **never call `raise_for_status`**. The routes do `result = await …` then `return JSONResponse({"ok": True, "result": result})` — the only path to `ok:False` is a Python exception. Classic Central is confirmed 401 on every call, so both writes report success while nothing happened.

Compounding, and **new about the known Classic-401 item**:
- `ClassicCentralClient.__init__` (`central_bridge.py:55`) sets `self._expires_at = time.time() + 7000  # assume ~2h from startup`, and `_ensure_token` (`:81`) refreshes only on that clock. `get()`/`post()` (`:93-97`) **never inspect the response status**. So a token that is already invalid at container start is used unchanged for ~117 minutes after *every* restart, even if the refresh token is good.
- `ClassicCentralClient.get` uses `timeout=30`. One probe returned `requests.ReadTimeout` — a hung Classic gateway blocks a `/devices` render for up to 30 s while holding a semaphore slot **and** a threadpool worker.
- Exceptions are never cached (`_fetch_and_store` re-raises without writing), so `/devices` fires **two uncached 401s on every single page load, forever**, with no log line.
- The group/site dropdowns are consequently empty (parsed live: 2 `<option>` each, both placeholders) — while `GET /network-config/v1/device-groups` returns **200 with 4 real groups** (Wireless/8 devices, Switches/1, zztest-lab33-cluster-gws/0, Lab-Wireless/0) in ~140 ms on the plane the portal already authenticates against.

**⚠ Do not apply the obvious fix.** One auditor proposed pointing `devices.py:177` at `get_display_sites`; the refuter killed it: `get_display_sites` returns New Central shape (`id`/`scopeId`/`scopeName`/`city`), while `devices/list.html:155-156` renders `s.site_id`, `s.site_name`, `s.city` — you would get a dropdown of `undefined`.

**Fix (three edits, in order):** (a) check `result.get('status_code') not in (200,201,202)` in both handlers and return `ok:False` with the upstream status; (b) catch `requests.HTTPError` in `get_device_groups`/`get_classic_sites` and return `None` (a `None` is low-confidence → 5 s TTL, bounding the retry rate), initialise `_expires_at = 0`, force one `_refresh()` + retry on a 401, and drop the timeout to ~5 s; (c) add a `@_cached()` wrapper over `mcp_servers.config.list_device_groups` and update the template keys together with it.

---

### D5 · The startup 429 burst: ~13 of 16 warmed cache keys are unreachable, ×2 workers
**medium** · areas: bridge-wrappers, runtime-config, deployment · `app/vendors/central_bridge.py:213`, `:471`

This is the mechanism behind the already-known "startup cache-warm burst provokes 429s", and it is worse than a burst — it is **mostly wasted**.

`warm_cache()` issues 16 upstream requests and writes 12 cache keys. Only **3** are ever read by a route (`get_sites()`, `get_tenant_health()`, `get_fleet_health()`). The other 9 miss because `_cached` keys on `(fn.__name__, args, tuple(sorted(kwargs.items())))` with **no binding against the signature's defaults** (`:471`):

| warmed key | what routes actually call |
|---|---|
| `get_devices()` | `get_all_devices()` → `get_devices(limit=200, offset=0)` |
| `get_clients()` | `get_all_clients()` → `get_clients(limit=200, offset=0)` |
| `list_active_alerts()` | `limit=100` (home, alerts) / `limit=50` (search cache) |
| `get_site_health_summary(site_id=…, site_name=None)` | `site_name='SecureSSID'` — warm_cache reads `siteName`/`site_name`/`name`, but New Central spells it **`scopeName`**, so it passes `None` |
| 5 × `get_device_events(device_type='ACCESS_POINT'…)` | `home.py:113` passes normalised lowercase `access_point` + `hours=` + `limit=` |

Measured duplicate upstream from fragmentation alone: `get_sites(); get_sites(limit=4); get_sites(limit=100)` → **3** identical `GET /network-config/v1/sites` (params `None` — the limit is applied client-side). `list_wlans(200); list_wlans(50)` → **2** identical. `list_active_alerts(); list_active_alerts(limit=50)` → **2** byte-identical.

× 2 uvicorn workers with independent in-process caches ⇒ ~26 wasted requests per cold start into endpoints that 429 at 8-14%, each 429 costing ≥5 s of a held semaphore slot. Confirmed in logs: every warm URL appears exactly 2× (some 3-4×), 37 ms apart.

**Fix (three independent edits, all needed):** (1) normalise the cache key — `sig.bind(*args, **kwargs); bound.apply_defaults()`, key on `tuple(sorted(bound.arguments.items()))`, memoise the `Signature` per function; (2) fix `warm_cache`'s site-name resolution to include `scopeName`, and make its argument forms match the real call sites; (3) elect one worker to run `warm_cache()` using the advisory-lock pattern already used for the scheduler.

---

### D6 · The healthcheck can only detect a dead socket — `/healthz` returns 200 while reporting `db: fail` and `scheduler: unelected`
**medium ⚠︎unrefuted** · area: deployment · `app/main.py:412`

`healthz()` (`main.py:412-427`) returns a dict with no `status_code`, so FastAPI always emits 200 regardless of `db.ping()` failing or the scheduler role being `unelected`. The compose healthcheck tests exactly `.status == 200`. `RestartPolicy: unless-stopped` acts only on process exit, never on unhealthy.

Consequence: a portal with no database and **no background jobs running anywhere** — no device-down alerting, no expiry checks, no summary reports — reports `Up (healthy)` indefinitely. The docstring at `main.py:418-419` explicitly says the scheduler field exists so a portal with no scheduler does not look healthy; nothing acts on it.

**Fix:** return `JSONResponse(status_code=503)` when the scheduler role is `unelected` (that is the state where alerting is silently off); keep `/health` as the unconditional liveness probe. A hard 503 on `db_ok == False` is optional — the app is designed to run degraded.

---

### D7 · Connection pool: `minconn=1` makes it a pool of one; exhaustion silently empties the notification bell
**medium + medium** · area: database · `app/db.py:52`, `app/routes/notifications.py:445`

`ThreadedConnectionPool(1, 10, …)`. psycopg2 2.9.12 `_putconn` retains a connection only `if len(self._pool) < self.minconn` — everything else is `conn.close()`d. Measured: 5 serial `get_conn()` all pid 76336; 4 concurrent → 4 distinct pids; afterwards idle pool = 1 and the other three are physically closed.

**Cost, measured by A/B benchmarking two throwaway pools against the live DB from a separate process:** `minconn=1` → 64.8 / 78.8 / 64.1 ms per checkout; `minconn=10` → 0.7 / 0.5 / 0.4 ms. **~100×.** The amplifier the first auditor missed: `ThreadedConnectionPool.getconn` holds `self._lock` across `_connect`, so every fresh `psycopg2.connect` (median 10.57 ms) is serialised under one global mutex — 8 barrier-synchronised checkouts all took ~74 ms each, a convoy.

**Exhaustion is firing in production**, contrary to the first auditor's log-grep conclusion. Controlled sweep on `/notifications/api/recent`: N=20 → 0 warnings; **N=30 → 6; N=50 → 30; N=400 → 378**. Every one still returned **HTTP 200**, because `routes/notifications.py:445-447` catches the `PoolError` and returns `{"items": [], "unread": 0}` — the bell silently empties itself under load with no user-visible error. (The predicted `/lab/activity` 500 does **not** happen; `routes/lab.py:1038-1043` catches and renders a banner.)

*Methodology warning for whoever re-runs this:* `docker logs --since` reads a naive timestamp as **host-local** (CDT) while the app logs UTC — that produces false-negative zeros. Use epoch seconds.

**Fix:** `ThreadedConnectionPool(10, 10, …)` — `max_connections` is 100 and 2 workers × 10 = 20, so it is free. Raise `maxconn` to the anyio limiter (40) or cap the limiter to the pool size at startup. Make `api/recent` distinguish "DB unavailable" from "no notifications". Add `assert created.minconn == created.maxconn` to `tests/test_db_pool.py:29`, which currently captures `minconn` and never asserts it.

---

### D8 · `_parse_dsn` silently discards the DSN query string, so no libpq option is reachable from the environment
**low** · area: database · `app/db.py:26`

`_parse_dsn` returns exactly `{host, port, dbname, user, password}`. `db._parse_dsn('postgresql://u:p@h:5433/d?sslmode=require&connect_timeout=5&application_name=x')` → the whole query string is **dropped without warning**. No `connect_timeout`, `keepalives`, `sslmode` or `application_name` anywhere in `app/` (grep returns nothing), and no server-side backstop (`statement_timeout=0`, `idle_in_transaction_session_timeout=0`). Live `pg_stat_activity` shows portal backends with a blank `application_name`.

Combined with `minconn=1` (nearly every concurrent request opens a fresh connection) and the writes-on-the-event-loop item below, a black-holing network path wedges a worker for the kernel TCP retry budget (~130 s).

**Fix:** add `connect_timeout: 5`, `application_name: 'netlab-portal'`, `keepalives=1, keepalives_idle=30` to the dict `_parse_dsn` returns, and **merge** rather than drop DSN query parameters.

---

### D9 · Migration framework: the stamp-existing path stamps *every* version, so a future migration can never run
**medium** · area: database · `app/db.py:351`

`db.py:351-362`: `if current == 0 and _baseline_exists(): for version in versions: cur.execute(_STAMP_SQL, (version,))` — it iterates **every** version, not just the baseline. Reproduced by driving the real `run_migrations()` against a fake connection with `MIGRATIONS = [(1, SCHEMA_SQL), (2, "ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS notes TEXT")]`: **stamped `[1, 2]`, MIG2 executed: False**.

It is *enshrined*: `tests/test_migrations.py:121-128` asserts `fake.versions == {1, 2}` **together with** `assert MIG2_SQL not in fake.committed_sql`.

Preconditions today: `MIGRATIONS` has one entry, so it is inert. It needs (a) someone to add version 2 and (b) a database with baseline tables but empty `schema_version` — i.e. a restored pre-migration volume. Given this project's known rebuild-DB gotcha, (b) is not far-fetched. The failure is silent: `schema_version` says up-to-date while the column does not exist.

**Interacting:** `audit_log` is created **outside** the framework — its DDL is `security.py:285-293`, applied by `ensure_audit_schema()` from `main.py:192`, after `init_db()`, outside the `_MIGRATION_LOCK_KEY` transaction, with a bare `except Exception: logger.warning`. Both workers run it every boot (paired log lines 81-127 ms apart). So `schema_version=1` describes 9 tables while the schema has 10. **Lifting it into `MIGRATIONS` as version 2 collides with the stamp bug** — fix the stamp first, or a restored volume gets stamped at 2 without ever creating `audit_log`.

**Fix:** `cur.execute(_STAMP_SQL, (versions[0],))` then fall through to the normal loop; flip the assertion in `test_stamp_existing_covers_every_version` to require `MIG2_SQL in committed_sql`. Then move `AUDIT_SCHEMA_SQL` into `MIGRATIONS` as version 2 and delete `ensure_audit_schema`.

Also: `init_db`'s docstring (`db.py:383-386`) claims the seeding runs under the advisory lock. It does not — the `with get_conn()` block closes at `:392`, and all three seed blocks are at `:396-438`. The `alert_rules` check-then-insert (`:421-428`) races across the two workers on a fresh DB. **Impact refuted:** `notifications._matching_rule` (`notifications.py:515-520`) selects exactly one rule via `min(matches, key=(offline_minutes, id))`, so two identical rules cannot produce two alerts — the consequence is a cosmetic duplicate row.

---

### D10 · Config-page test fixtures invent payload shapes Central never sends — the direct cause of four shipped rendering bugs
**high** · area: central-config-api · `tests/test_wlans.py:17`, `tests/test_platform.py:13`, `:88`

Every one of these fixtures patches `central_bridge` itself — the exact violation `tests/test_bridge_contract.py`'s own docstring names ("Stub one layer BELOW the code under test — here that means faking `mcp_servers.*` via `sys.modules` rather than patching the bridge").

| fixture claims | Central actually sends |
|---|---|
| `{"name": "corp-wifi", "essid": "corp-wifi", "vlanId": 20, "enabled": True}` | no `name`; `essid` is a **dict** `{"name": "Air Pass"}`; no `vlanId` (it is `vlan-id-range: ["200"]`); flag is `enable` not `enabled` |
| firmware: `model`, `targetVersion`, `siteName`, `complianceStatus` | firmware-details has **none of those four** |
| MAC reg: `description`, `role` | payload has `displayName`, `enable`, `staticTags`; **no role field exists** |

Result: `/wlans/`, `/platform/config` and `/platform/nac` tests all pass green against data that does not exist, which is why every rendering bug below survived.

**Fix:** extend `tests/test_bridge_contract.py` with a config section using the real shapes captured in the audit, stubbing `mcp_servers.config` / `mcp_servers.nac` via `sys.modules` the way `fake_monitoring` already does at `test_bridge_contract.py:41`.

---

### D11 · `/wlans/` renders a raw Python dict, drops the VLAN, and hardcodes the status badge
**medium** · area: central-config-api · `app/routes/wlans.py:26`, `:29`, `:30`

Live page body, unescaped: `Air Pass | {'name': 'Air Pass'} | EMPLOYEE | WPA3_ENTERPRISE_CCM_128 | — | enabled` — and the same for the other two SSIDs.

- `:26` `w.get("essid") or w.get("ssid")` — a non-empty dict is truthy, so the dict repr reaches the template. **`app/routes/search.py:158` already does this correctly** by reading `w["ssid"]` first.
- `:29` reads `vlan`/`vlanId`; the real keys are `vlan-selector: "VLAN_RANGES"` and `vlan-id-range: ["200"]`. All three SSIDs are on VLAN 200 and all three render `—`.
- `:30` `w.get("enabled", w.get("status") != "disabled")` — the key is `enable`, and there is no `status` key at all, so the default evaluates `None != "disabled"` → **True unconditionally**. Correct today by luck; wrong the moment an SSID is disabled.

**Fix:** three one-liners — unwrap `essid` when it is a dict, join `vlan-id-range`, read `enable`.

---

### D12 · `/platform/config` "Fetch config" is dead on every device and dumps a raw Python dict with internal endpoint paths into the page
**medium** · areas: central-config-api, mcp-coverage, bridge-wrappers (triple-reported) · `app/routes/platform.py:170`

`config.get_device_running_config` tries four endpoints and all four fail identically on the switch, an AP and a gateway: 400 `/network-config/v1/devices/{serial}/configuration`, 400 v1alpha1, 404 `/configuration/v1/…`, 404 `/configuration/v1/devices/template/…`. `platform.py:167-170` then does `result.get("config") or result.get("output") or str(result)` → the `<pre>` contains the Python dict repr **including all four internal API paths** and the `_note`.

Two contributing convention violations at the wrapper: `central_bridge.py:1181 get_device_running_config` has **no `@_cached()`** (that part is deliberate and allow-listed) but **returns the envelope instead of `None` on failure** — which is precisely why the route falls through to `str(result)`.

**A working path exists and was verified:** `ops.cx_show(SG30LMR164, ["show running-config"])` → COMPLETED in 5.9 s, **16,950 characters** of real config (`show running-config` is on the CX allowlist). `/lab/config` already does this correctly via `ops.run_show` — verified live on both the CX6300 (FL.10.17.1010) and an AP735 (AOS-10 10.8.0.1).

**Fix:** return `None` when `config` is None; repoint the panel at `run_show(serial, type, ["show running-config"])`; render `result['errors']` as a styled error state. **Mask first** — the output contains the AOS-CX admin `password ciphertext AQBapaB6…` line.

---

### D13 · Two ops diagnostics send CLI commands the CX rejects — LLDP and Find-MAC have never worked
**medium ×2** · area: mcp-coverage · `mcp_servers/ops.py:499`, `:535`

- `get_lldp_neighbors` sends `show lldp neighbors` (plural). Live `POST /devices/SG30LMR164/lldp` → 5.7 s and a `<pre>` containing `Command 'show lldp neighbors' not allowed`. The device's own `list_show_commands` returns 141 permitted commands; the Physical Connection category is `['show lldp local','show lldp local-device','show lldp neighbor','show lldp neighbor-info']`. **Verified fix:** `cx_show(…,['show lldp neighbor'])` → 1,762 chars, "Total Neighbor Entries : 10". (`show lldp neighbor-info` is on the allowlist but is **also** rejected by the device — the allowlist is itself partly wrong.)
- `find_mac_on_switch` sends `show mac-address-table address <mac>`. Live `POST /devices/SG30LMR164/find-mac` → `Command '…' not allowed`. **Verified fix:** `cx_show(…,['show mac-address-table'])` → 3,666 chars / 51 MACs including the exact line `00:0b:86:b8:c4:b8    200    dynamic    1/1/17`, which matches what `get_client_details` independently reports.

Related dead tool: `get_switch_interface_counters` (`ops.py:591`) sends `show interface counters`, not on the 6-command Interface allowlist (`show interface statistics` works and is what `get_switch_port_errors` uses). Unused by the portal; a trap for anyone wiring an interface-stats panel. **info.**

Each failed attempt costs ~5.7 s and one device session. `/centralmcp` is mounted read-only, so both fixes belong in `central_bridge`.

---

### D14 · RF metrics are null two layers deep, and the AP page fetches the same URL twice to get them
**medium** · areas: mcp-coverage, bridge-wrappers · `mcp_servers/monitoring.py:1602`, `app/vendors/central_bridge.py:1004`, `app/routes/devices.py:284`

**New about the known `devices.py _wireless_cards` bug:** even a corrected `devices.py` would render nulls, because `monitoring.py:1600-1606` maps `utilization_pct ← r.get('utilization') or r.get('channel_utilization')`, `noise_floor_dbm ← 'noise'/'noise_floor'`, `tx_power_dbm ← 'txPower'/'tx_power'`. The payload uses **`channelUtilization`, `noiseFloor`, `power`**. Confirmed by dumping the raw radio object: `channelUtilization '18'`, `noiseFloor '-98'`, `power '8'`, `channelQuality '96'`, `nonWifiInterference`, `rxUtilization`. The `or` chains also turn a genuine `clientCount: 0` into `None`.

**A cheaper fix than either auditor proposed:** `get_channel_utilization` already returns the complete correct payload under `result["raw"]` (`monitoring.py:1608`). `devices.py` can read `channel_util["raw"]["radios"]` with zero upstream change. (Side note: shipping `raw` alongside the summary roughly doubles that cache entry.)

Better still, delete the call: `get_ap_radios` and `get_channel_utilization` hit the **identical URL** `/network-monitoring/v1/aps/{serial}/radios` with `params=None`, and `devices.py:284-289` fires both in one `gather` on every AP detail view — into an endpoint family that 429s. Fleet-wide, `monitoring.list_radios` returns all 15 radios with the same fields in one 0.45 s call, replacing 9 per-AP calls.

---

### D15 · Every DB write in `routes/notifications.py` runs blocking psycopg2 on the event loop; only the reads were moved off it
**low** · area: database · `app/routes/notifications.py:254`

AST scan (excluding closures passed to `run_in_threadpool`) finds the genuinely-on-the-loop set: `notifications.py` **254** (inside a `for k,v in body.items()` loop — up to 10 sequential round trips), **273, 275, 343, 365, 378, 443, 477, 478**; plus `routes/auth.py:62` (`security.verify_password`) and `routes/lab.py:185/192`. Every **read** in the same file is correctly wrapped, with explanatory comments. `main.py:282` short-circuits on `settings.portal_password` before touching the DB, so the middleware is safe today.

**Cleared item worth recording:** `security.record_audit`, the hottest write path in the app, **is** correctly wrapped at `main.py:317-322`.

**Fix:** wrap each write in `run_in_threadpool` as the reads already are; collapse `save_settings` into one `def _save()` so it is one hop rather than ten.

---

### D16 · Eight cached wrappers return the raw envelope; the docstring justifying it is factually false on this tenant
**low** · area: bridge-wrappers · `app/vendors/central_bridge.py:741, 780, 797, 803, 809, 858, 998, 1004`

None of the eight call `_payload`/`_unwrap`. Measured TTL consequence: `get_wireless_metrics('PHT5M520SZ')` → `errors: []` → 60 s; `get_wireless_metrics('CNP6L2H02W')` (gateway) → 3 × 404 in `errors` → `_is_low_confidence` True → **5 s**. So correctness is intact by luck, and any device whose first endpoint candidate stops answering silently becomes one of the most frequently refetched calls in the portal.

The module docstring at `:866-871` asserts "These monitoring envelopes ALWAYS carry a non-empty `errors[]` on success" — **not true here**, and actively misleading.

Two related convention violations: `get_lldp_neighbors` (`:779`) carries `@_cached()` despite being a click-to-run async-job diagnostic structurally identical to the six in `DELIBERATELY_UNCACHED` (a user re-clicking LLDP after re-cabling gets a ≤60 s stale snapshot, ≤660 s with stale grace); `test_every_read_wrapper_is_cached` only flags *missing* decorators, never extra ones.

**Fix:** `_payload(result, key)` on all eight; correct the docstring; drop `@_cached()` from `get_lldp_neighbors` and add it to `DELIBERATELY_UNCACHED`, plus the inverse assertion in `test_bridge_contract.py`.

---

### D17 · GLP service-offer wrapper reads the wrong envelope key — the catalog tab is permanently blank while blaming missing API access
**low** · area: bridge-wrappers/central-config-api · `app/vendors/central_bridge.py:1197`

`list_glp_service_offers` returns `{'data': {'count','items','next','total'}, 'endpoint_used', 'errors'}` — **21 items under `data.items`**, nothing at top level. The wrapper does `result.get("items", [])` → always `[]`. Measured against the other four GLP wrappers, which all return top-level `items` (devices 14, subscriptions 34, users 2, audit 100), it is the sole odd one out. Live `/lab/greenlake` renders "No service catalog entries — **requires GLP API access**", which is false: the call succeeds.

**Fix:** `result.get('data', {}).get('items', []) or result.get('items', [])`, or extend `_unwrap()` to look one level into `data` for GLP-shaped envelopes.

---

### D18 · Log rotation is configured nowhere
**medium ⚠︎unrefuted** · area: deployment · `docker-compose.yml:2`

`LogConfig: {'Type': 'json-file', 'Config': {}}`; `/etc/docker/daemon.json` has no `log-opts`; no `logging:` key in any of the three compose files. 746 KB in ~100 min ≈ 10-12 MB/day and growing, unbounded, on `/volume1/@docker`. Most of the volume is self-inflicted: `GET /healthz` every 30 s from two probers plus one INFO line per httpx call to the tenant.

**Fix:** `logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}` on both services, or globally in `daemon.json`. Quiet `httpx` to WARNING and exclude `/healthz` from the access log.

---

### D19 · Six concurrent ops diagnostics starve the whole upstream semaphore
**low** · area: bridge-wrappers · `app/vendors/central_bridge.py:136`

`_run` wraps the entire callable in `async with _upstream_semaphore()`, including centralmcp's `atroubleshoot_poll`, which does `await asyncio.sleep(5)` **before** its first poll (`shared.py:854/1091`). Measured: `gather(get_cx_arp_table ×6, get_device ×2)` → wall 6.18 s; the diagnostics 5.73-5.84 s each; the two `get_device` calls, **0.98 s uncontended, took 6.08 s**. Each diagnostic holds 1 of 6 slots for ~5.8 s while doing ~0.7 s of network work — a 12% duty cycle.

Related: `_MAX_CONCURRENT_UPSTREAM` is a `WeakKeyDictionary` keyed by **event loop**, so the ceiling is 6 × live loops, not 6 process-wide — and with 2 workers plus the scheduler's `new_event_loop()` per sweep, ≥18 host-wide. The comment at `:110-116` calling it a rate-limiter ceiling is wrong on both counts.

**Fix:** exempt the `mcp_servers.ops` async-job callables from `_upstream_semaphore`, or acquire per-HTTP-request rather than per-callable. Correct the comment.

---

### D20 · Client detail page: four guaranteed 404s per load at a 5 s TTL, plus an 18-request roaming scan
**low** · area: bridge-wrappers · `app/routes/clients.py:150`

`client_detail` gathers three wrappers. `locate_client` issues **4 requests, all 404** (three MAC forms + `/location/v1/…`) and its envelope trips `_is_low_confidence` → **5 s TTL**, so the four 404s are re-issued on essentially every reload. `get_client_roaming_history` issues **18 requests** (a device-inventory lookup + an events fetch for every one of 13 devices) at 2.5 s. Timing correction: the first auditor's 7.8 s figure included a 429 and is not representative; live page is 3.2 s cold, 4 ms warm (twice cold, because two workers).

Same shape on AP pages: `get_ap_rf_neighbors` (`:1009`) issues **3 × 404** and returns `[]` → `_is_low_confidence([])` True → **5 s TTL**, unconditionally on every AP detail view (5 hits per 404 path per hour in the logs). Its `except Exception: invoke_tool_router(...)` fallback at `:1015-1018` is **unreachable** — the import succeeds and the tool swallows its own HTTP errors into an envelope.

**Fix:** drop `locate_client` and `get_ap_rf_neighbors` from the page-load gathers (both on the known-404 list); make the roaming history a click-to-run `hx-get` fragment; delete the dead fallback branch.

---

### D21 · Immutable one-year caching on unversioned assets, and woff2 served as `text/plain`
**low ×2 ⚠︎unrefuted** · area: deployment · `app/main.py:365`, `:382`

`_IMMUTABLE_PREFIXES = ("vendor/", "fonts/", "icons/")` with the justifying comment "Vendored files carry their version in the filename" — true for `vendor/`, **false for `fonts/` and `icons/`** (`inter-latin.woff2`, `ap.svg`, etc.). Those tags are emitted raw, without `asset_url()`, and served `public, max-age=31536000, immutable`. Replace an icon and every browser that has loaded the portal keeps the old bytes for a year without revalidating, with no escape hatch.

Separately `GET /static/fonts/inter-latin.woff2` → `content-type: text/plain; charset=utf-8` (stdlib `mimetypes` has no `.woff2`). Cosmetic today because `format('woff2')` governs — **but** under the OPNsense-injected strict CSP, a `font-src` policy plus `nosniff` can reject the font outright, which would present as exactly the frozen/wrong-typeface symptom this deployment already fights.

**Fix:** narrow `_IMMUTABLE_PREFIXES` to `("vendor/",)` (one-character change; ETags already produce 304s), and `mimetypes.add_type("font/woff2", ".woff2")` before mounting StaticFiles.

---

### D22 · Invalid values for four env vars crash the app at import, against the documented "never raises" contract
**low** · area: runtime-config · `app/main.py:32`, `app/config.py:29`

`config.py:41-45` states "Never raises — the app should start in a degraded mode … rather than crash", but `Settings()` is constructed at `:29` before `validate_settings` runs. Reproduced in-container: `DEVICE_FETCH_LIMIT='lots'` → `pydantic_core.ValidationError`; `logging.basicConfig(level='VERBOSE')` and `setLevel('VERBOSE')` both → `ValueError: Unknown level`. `main.py:30-36` passes `LOG_LEVEL` to both branches. `main.py:54` `float(...)` and `central_bridge.py:117` `int(...)` are unguarded.

**Fix:** coerce `LOG_LEVEL` against `logging.getLevelNamesMapping()` with an INFO fallback; wrap the float/int reads in try/except; catch `ValidationError` around `Settings()`.

---

### D23 · Assistant is down and its failure message is empty
**medium + low** · areas: runtime-config, deployment · `docker-compose.override.yml:21`, `app/routes/assistant.py:262`

DB `assistant_backend='claude_cli'`, `CLAUDE_CODE_OAUTH_TOKEN=''`. Reproduced with the exact argv the code builds: rc=1, `Failed to authenticate: OAuth session expired and could not be refreshed`. **Corrected cause:** `/volume1/docker/claude-bridge-cfg/.credentials.json` is not stale, it is **blanked** — `accessToken` and `refreshToken` are both empty strings, `expiresAt` is a 1-digit int, and the file's mtime (22:01 UTC) coincides with the app's first CLI failure. The mount is **read-write**, so the CLI most plausibly wrote the cleared credentials back itself.

**Corrected fix:** a host `claude` login writes to `~/.claude`, not `/volume1/docker/claude-bridge-cfg` — only `CLAUDE_CODE_OAUTH_TOKEN` in `.env` (or an explicit `CLAUDE_CONFIG_DIR` host login) reaches the container. The GitHub Models fallback cannot cover it either: `GITHUB_TOKEN` is still `your_token_here`, so **there is no working AI backend in either selection**.

`assistant.py:262` reads only stderr; measured, the CLI puts the entire failure on **stdout** and leaves stderr empty — hence the live log line `claude CLI exited 1: ` with nothing after the colon. The route then returns **HTTP 200** to the browser with the error swallowed.

**Fix:** `detail = (stderr.decode(...).strip() or stdout.decode(...).strip())[:500]`; surface the failure to the user instead of 200; mount `.credentials.json` read-only so a failed refresh cannot blank the host copy.

---

### D24 · `POST /notifications/settings` coerces arbitrary JSON with `str()`
**low** · area: runtime-config · `app/routes/notifications.py:253`

`db.set_setting(k, str(v))` with no normalisation. Consumers compare exact lowercase strings: `:146` `!= "true"` (subscriptions), `:309` (SSL), `:110` `use_tls = cfg["tls"] != "false"`. A body of `{"check_ssl": true}` stores `"True"` → **check silently disabled**; `{"smtp_tls": false}` stores `"False"` → TLS stays on. Not reachable through the shipped UI (Alpine binds `true-value="true"`), and current DB values are correct. Failure is invisible — nothing logs a skipped check, and the request returns `{"ok": true}`.

**Fix:** normalise the three boolean keys on write; read them through one shared helper.

---

### D25 · Upstream centralmcp defects on the config plane (unwired today)
**low, grouped** · area: central-config-api

- `config.list_named_vlans` (`config.py:2098`): the endpoint returns `{"profile": [{"name":"data","vlan":{"vlan-id-ranges":["200"]}}]}`; the tool looks for `items`/`vlans`/`named_vlans` and returns `vlans: []` **while reporting the endpoint as the one that answered**. Separately its scope resolution calls `/network-monitoring/v1/globalScopeId` → **404**, while `monitoring.get_global_scope_id` (which delegates to `s6_configure._fetch_global_scope_id`, already imported at `config.py:51`) returns `79236221864456192` correctly. So the tenant's only named VLAN — `data` / VLAN 200, the VLAN every SSID binds to — is invisible.
- `config.get_firmware` (`config.py:221`): always **400**. `?serialNumber=` → "must be in kebab-case"; `?serial-number=` → "Unknown query parameter". The endpoint has no serial filter at all. Fix: drop the param and filter client-side, exactly as `list_firmware_upgrades` (`config.py:320`) already does.
- `config.list_webhooks` (`:1837`): endpoint returns literal JSON `null`; tool returns `{"items": None}` — a latent `TypeError` for any consumer.
- `get_ssid` / `get_passpoint_profile`: annotated `-> dict | None`, documented "Returns None if not found", actually return `{}` — because the API answers **200 with a 3-byte body** for unknown names. Any `if get_ssid(name) is None: create…` guard takes the wrong branch.
- `monitoring.list_scope_devices`: returns `items: []` silently because it queries the past-sunset `v1alpha1/device-inventory`, which now 400s on a siteId filter.

---

### D26 · Router/tool-tester plumbing
**low, grouped** · area: mcp-coverage

- `tool_router.invoke_read_tool` (`tool_router.py:668-679`) blocks all 38 non-read-only ops tools. Verified live: `list_wlans` → SUCCESS, `get_lldp_neighbors`/`cx_show` → `not read-only`. Only `cx_ping`/`cx_traceroute` work, because `_TOOL_MAP` dispatches them directly. **This is the router behaving as designed** — it is a discoverability gap, not a defect. (One auditor filed it against the wrong file, `ops.py:492`.)
- `central_bridge.py:1200 invoke_tool_router` and `:1711 run_tool` carry **no `@_cached()`** — every Lab-tester submit is an uncached call against a 10 req/s limiter, the one path where a human trivially generates 429s by clicking.
- `run_tool` (`:1724-1735`) decides success with `if "error" not in output` — a standard envelope with a populated **`errors`** (plural) is reported as SUCCESS with empty data. Same singular/plural asymmetry that was fixed in `_is_low_confidence` today, not fixed here.
- The generated backend's redactor **over-redacts**: `central_read_management_users` returns `_pagination.list_key: "******"`. Know this before relying on `redact_sensitive`.

---

### D27 · Small, confirmed, low-value
**info/low, grouped**

`get_notification_history` (`db.py:508`) orders by `sent_at DESC` with no `id` tiebreak, unlike its three sibling getters — nondeterministic ordering for same-second rows. · `asset_url()` re-reads and re-hashes ~59 KB of CSS on every page render, synchronously, inside the async render path (measured impact currently nil; memoise on `(path, st_mtime_ns, st_size)`). · `_prune_locked` (`central_bridge.py:371`) only evicts entries older than TTL+600 s, so the 512-entry cap is not a real ceiling under fast key churn; `_inflight` is never pruned and orphans entries on cross-loop overwrite (unreachable at 12-key steady state). · `device_status_snapshot` has no removal path — a decommissioned serial keeps its row forever (inert). · Four columns written and never read: `device_status_snapshot.name`/`.updated_at` (keep `updated_at` — it is the only record of when the sweep last ran), `alert_recipients.created_at` (an `isoformat()` per row for nothing), `schema_version.applied_at`. · `db.py:580`'s docstring says nothing has ever read `device_status_history`; `routes/lab.py:1033` does. · Zero secondary indexes and no retention on four append-only tables — **correctly info-grade**: 0 rows in three of them, 0.42 audit rows/day pre-audit, and a seq scan over 17 rows is the right plan. `notifications_sent` is *not* bounded by its UNIQUE constraint as claimed (`_fire_down_alert` embeds a timestamp in `source_id` deliberately); it is bounded by `cooldown_minutes`. · `ruff.toml:9 target-version = "py311"` vs Python 3.12 everywhere.

---

# PART 3 — SECURITY INVENTORY

Ranked for a **private single-user LAN deployment**. "Shared-only" = starts to matter once someone other than the owner has a login or reaches the box.

| # | Item | Sev (lab) | Shared-only? | Evidence / fix |
|---|---|---|---|---|
| S1 | **`docker-compose.override.yml` is gitignored and is the only thing making the deployment safe** ⚠︎unrefuted | **high** | **No** — it is a redeployment hazard today | Base compose has no `user:` (override's `user: "999:10"` is the only reason it is not root), publishes `5432:5432` (override's `ports: !override []` is the only reason it is not), and starts Caddy on 80/443. `.env` has no `POSTGRES_PASSWORD`, so `${POSTGRES_PASSWORD:-netlab}` wins. Clone this repo anywhere else, run the documented `docker compose up --build -d`, and you get Postgres on 0.0.0.0 with netlab/netlab, Caddy on 80/443, app as root, RW over the source. **Fix:** move `user:` into the base compose, drop the db `ports` block entirely, move caddy to an opt-in `docker-compose.caddy.yml`, keep only host paths in the override. |
| S2 | **`client_ip()` trusts `X-Forwarded-For` from any peer — confirmed forgery of the audit trail** ⚠︎unrefuted | medium | Mostly — but see S3 | `security.py:174` returns the first XFF entry unconditionally. Reproduced: `curl -H 'X-Forwarded-For: 8.8.8.8' … /login` → log line `Successful login from 8.8.8.8`, sitting among genuine entries, over plain HTTP with no proxy in the path. Also bypasses any per-IP throttling by header rotation. **Fix:** honour XFF only when `request.client.host` is in an explicit trusted-proxy allowlist. |
| S3 | **Login rate limiter is a self-DoS, and logins are never audited** | medium | **No** — it bit the auditors today | `security.py:264-268` + `routes/auth.py:54-59` check `is_limited(ip)` **before** verifying the password, key solely on client IP, and `reset()` only on success. Reproduced: six consecutive POSTs with the **correct** password from 192.168.1.31 → 429, while 127.0.0.1 succeeded; 29 "Login rate limit exceeded" warnings. With 2 workers each holding its own counter, a legitimate login is nondeterministically refused depending on which worker accepts the connection. Separately, `audit_log` has 86 rows covering `/lab/mcp-tester`, `/devices/assign-group` etc. and **zero `/login` rows**, because `main.py:288-289` returns early for `AUTH_EXEMPT_PATHS` (which includes `/login`) before the audit call at `:317-322`. **The one security event you want a trail for is the one that is not recorded.** |
| S4 | **GreenLake subscription keys rendered unmasked — on two pages, one of them high-traffic** | medium | Yes | `/lab/greenlake`: 34 subscriptions with 18-char unmasked `key` in `<script id="glp-subs-json">` and a sortable "Subscription Key" column (`greenlake.html:428/441`); 193 KB page, 46 distinct key values. **Bigger surface the wrapper audit found:** `/devices/` embeds the full raw device-inventory JSON — `aruba_central.py:58 _norm_device` attaches `"_raw": d`, `devices/list.html:107` does `{{ devices \| tojson }}` — carrying the same `subscriptionKey` values on the portal's most-visited page. **Fix is one function:** `mcp_servers.shared._is_sensitive_key` returns **True** for `key`, `subscriptionKey` and `mpsk` (verified) — routing these through `redact_sensitive` masks all of them. `assign_glp_subscription` uses the subscription **id**, not the key, so nothing needs the plaintext. All 34 are eval/trial subscriptions. |
| S5 | **`/claude-cfg` is mounted read-write and the container runs as its owner** | low | No | `docker exec netlab-app id` → uid=999 gid=10 = choate85; the mount has no `:ro`. The 0700/0600 modes defend against *other host accounts*, not against the portal process — which is how the CLI blanked `.credentials.json` (D23). The dir also holds `.claude.json` (32 KB) with `accountUuid`, `emailAddress`, `organizationUuid`, `organizationName`, top-level `userID`/`machineID`, and a `backups/` subdir with five historical copies. **This contradicts the "correctly locked down" framing in the runtime-config report.** |
| S6 | **Auth fails OPEN when `PORTAL_PASSWORD` is empty and Postgres is unreachable — and drops CSRF and audit with it** | low | Yes | `security.py:43` `return bool(settings.portal_password) or bool(_db_password_hash())`; `_db_password_hash()` returns `None` on any DB exception (`:134-137`). Proven **end-to-end through the real middleware** with a starlette TestClient in an isolated process: baseline `GET /no-such-path` → 303 (redirect to /login); with `portal_password=''` and `db.get_setting` raising → **404**, i.e. routing reached and auth bypassed. `main.py:279-281`'s early return also skips **CSRF checking**, not just auth. Inert today (`PORTAL_PASSWORD` is set), but it bites the *documented* configuration — `.env.example:53` ships it empty and the UI presents the DB-hash Change Password as the way to set it. **Fix:** have `_db_password_hash()` distinguish "unset" from "unknown" and treat "unknown" as auth-enabled. |
| S7 | **`.env`, `credentials.yaml` and the live JWT cache are mode 0777** | low | Yes | `-rwxrwxrwx choate85:admin` on `.env`, `.env.example`, `docker-compose.override.yml`; same on `/centralmcp/config/credentials.yaml` and `/volume1/docker/fastembed-cache/centralmcp-tokens/` holding two live bearer JWTs (995 and 1389 chars, continuously refreshed on a ~15-min cycle). `.env` is correctly gitignored — no repo-leak dimension. **Fix:** `chmod 600` the files, `chmod 700` the tokens dir, check the Synology share ACL. |
| S8 | **Postgres runs on the shipped default password** | low | Yes | `.env` has 14 keys, none `POSTGRES_*`. Not published to the LAN (confirmed `PortBindings: {}` and `/dev/tcp/…/5432` refused), so this is defence-in-depth only — anything else on the compose network can read the alerting config, recipients and the portal password hash with a guessable credential. **Note:** Postgres reads `POSTGRES_PASSWORD` only on first init, so changing `.env` alone breaks the connection — needs `ALTER ROLE` or a volume recreate. |
| S9 | **`smtp_password` round-trips to the browser in plaintext** | low | Yes | `_SETTING_KEYS` includes it; `notifications.html:363` emits `{{ settings \| tojson }}`; `:141` binds it to `<input type="password">`, which masks glyphs, not source. Verified live: the key is present in the page source, currently empty. **Fix:** return a `smtp_password_set` boolean or a sentinel; treat the sentinel as leave-unchanged on save. |
| S10 | **`is_secure_request()` trusts `X-Forwarded-Proto` from any peer** ⚠︎unrefuted | low | Yes | `security.py:185` → `routes/auth.py:78 secure=`. Verified: same plain-HTTP request with and without the header mints the cookie with and without `Secure`. Spoofing it *up* is self-inflicted; the consequential direction is **down** — if the real off-box Caddy/OPNsense does not forward it, every cookie minted behind HTTPS lacks `Secure`. Could not observe those proxies. |
| S11 | **The override mounts the operator's entire host bin directory** ⚠︎unrefuted | low | Yes | `/home/choate85/.local/bin:ro` to get one binary; it holds 20+ executables (`uv`, `uvx`, `pip`, `dotenv`, `openai`, `cursor-agent`, `kimi-cli`, `agent`, `autohand`, `uvicorn`…). Read-only, so low, but a much larger blast radius than "the claude binary". |
| S12 | **`nac.list_auth_servers` returns fields literally named `plaintext-value` unredacted to the Lab tester** | low | Yes | Reproduced through the live portal: `POST /lab/mcp-tester tool=list_auth_servers` returns two `shared-secret-config.plaintext-value` blocks. Both are HPE **vault ciphertext** (`vault:v5:…` 89 ch, `vault:v6:…` 57 ch), so no usable credential is disclosed. The finding is that the field flows unredacted, and would matter the day Central puts a real secret in that slot. Same one-line fix as S4 (`shared.py` sensitive-key list). |
| S13 | **`ops.cx_show(['show running-config'])` returns the AOS-CX admin `password ciphertext` line** | low | Yes | 16,950 chars including `password ciphertext AQBapaB6…`. Only reachable via the Lab tester today — **but it is also the proposed fix for D12**, so mask before wiring it into `/platform/config`. |
| S14 | Four endpoints auth-exempt, one reporting backend connectivity | info | Yes | `AUTH_EXEMPT_PATHS = {/login, /health, /healthz, /api/status, /favicon.ico, /auth/whoami}`. Unauthenticated `/api/status` → `{"mode":"live","db":"ok","central":"connected"}`; `/healthz` → `{"status":"ok","db":"ok","scheduler":"leader"}`. No hostnames, devices, clients or credentials. `/docs` and `/openapi.json` correctly redirect. `/healthz` must stay exempt for the healthcheck; `/api/status` is exempt so the login page can render its live/demo banner. **No action for a single-user LAN.** |
| S15 | App emits no security headers of its own ⚠︎unrefuted | info | Yes | Full header dump on `/` and `/login`: `server`, `date`, `content-length`, `content-type`, `cache-control: no-store`, `pragma`. No nosniff, no X-Frame-Options, no Referrer-Policy, no CSP. Because the app ships no CSP, the OPNsense-injected policy is the only one in effect and the app has no say in it — which is the frozen-UI problem's root. **Fix:** a small middleware, so it survives the proxy topology changing. |
| S16 | `load_dotenv(override=True)` in the credential loader | info | No | `/centralmcp/pipeline/config.py:20`, reached at runtime via `get_client()`. `override=True` means a discovered `.env` **wins over already-set process env**. Purely latent — `find_dotenv()` returns `''` and none of `/.env`, `/app/.env`, `/centralmcp/.env` exist — but `/app` is a RW bind mount of the repo's `app/`, and `.env.example` says "Every value here is read by docker-compose.yml", which invites exactly that mistake. **Fix:** a startup assertion that `/app/.env` does not exist. |
| S17 | Integration compose runs as root and mounts the repo RW ⚠︎unrefuted | low | Yes | The documented invocation passes `-f` explicitly, which **suppresses the override** — so none of the hardening applies. `integration-tests` declares no `user:` and mounts `.:/workspace` RW including the 0777 `.env`. Corroborated on disk: `.pytest_cache/`, `app/__pycache__/` and **`app/static/dist/`** are `root:root` — the very directory holding the served stylesheet, writable by the app only because the NAS forces 777. |
| S18 | `SESSION_MAX_AGE_HOURS` does not revoke minted sessions | info | Yes | `session_max_age_seconds()` is consulted only in `create_session_token()`, which bakes an absolute expiry into the cookie; `verify_session_token()` checks the embedded value, never the current setting. Lowering it cuts nobody off for up to the old lifetime. |

---

# PART 4 — DEAD CONFIG & DOCUMENTATION DRIFT

| Item | Where | Status |
|---|---|---|
| `ARUBA_CENTRAL_BASE_URL` / `ARUBA_CENTRAL_ACCESS_TOKEN` | `config.py:11-12`, `:63-64`, `README.md:156-157`, `docker-compose.yml:9-10`, `.env.example:24-25` | **Dead.** Read by nothing but a startup warning. Container has the apigw-prod2 host; 2297 log hits go to internal.api. The token is a permanent placeholder with nothing to fix it into. |
| The single startup config warning this deployment emits | `config.py:63` | **A permanent false alarm with an untrue claim** — "views may fall back to mock data" is false; the mock switch is import-availability-based (`aruba_central.py:161/173/197`, zero references to any Setting). It is 100% of the config-warning output and 0% of it is true. |
| `OLLAMA_URL` | `docker-compose.yml:29`, `.env.example:82-86`, `README.md:172` | **Dead.** Only occurrence in centralmcp is a module *constant* `OLLAMA_URL = "http://localhost:11434"` (`ollama_client.py:5`), never `os.getenv`. Every `OllamaClient()` call site instantiates with no url. Zero impact today (`_BACKEND` defaults to lancedb). |
| `settings.database_url` | `config.py:15` | **Dead field.** `db.py:20-22` does its own `os.environ.get` with the same literal default repeated. |
| `env_file = ".env"` | `config.py:25` | **Never resolves in any context.** `/app/.env` does not exist (bind mount is `./app→/app`); conftest chdirs to `<repo>/app` where it also does not exist. Incidentally protective for tests. |
| `find_site`, `add_device_to_group` | `central_bridge.py:589`, `:1349` | **Dead wrappers**, 0 references anywhere including the module itself. `find_site` would duplicate `get_sites`' upstream request under a separate key. |
| `get_glp_subscriptions_raw`, `get_classic_client` in `DELIBERATELY_UNCACHED` | `tests/test_bridge_contract.py:208` | **Phantom entries** — the first does not exist; the second is a sync def already excluded by the test's own `iscoroutinefunction` filter. |
| `Caddyfile` + caddy service + README architecture block | `Caddyfile:1`, `README.md:38, 79-80, 101-109` | **Dead.** Never ran. README claims "Browser → Caddy (:80/:443) → FastAPI", "security headers", and "compose waits for /healthz before starting Caddy" — none true here. gzip actually comes from `GZipMiddleware` (`main.py:355`). The Caddyfile listens on `:80` only, so the base compose's published 443 would be dead anyway. |
| README config table | `README.md:152-173` | 20 rows; 3 dead; ~15 consumed vars absent, including the entire `SOURCE_*`/`TARGET_*`/`GLP_*` set and the `CLAUDE_*`/`ASSISTANT_*` group — which lives **only** in the gitignored override, i.e. nowhere a repo reader can find it. `.env.example:6-7` asserts "Every value here is read by docker-compose.yml and passed to the app container" — false for four of them. |
| `validate_settings` / `is_placeholder` | — | **Zero test coverage.** 46 test files, no `test_config.py`, grep returns nothing. `main.py:170` discards the returned warnings despite the docstring offering them "for callers that want to surface them". Also `config.py:63` bypasses `is_placeholder()` (no `.lower()`) and `lab.py:610` hardcodes `!= "your_key_here"`. |
| No `.dockerignore` | anywhere | 40 `.pyc` in the image including stale `config.cpython-**311**.pyc` in a python:3.12 image. `COPY . .` also copies the host's `static/dist/tailwind.css` in as a build input. |
| CI | `.github/workflows/ci.yml` | Two jobs: pytest, ruff. **No `docker build`, no tailwind build, no `docker compose config` validation** — the two things most likely to break a deployment are the two CI does not test. The tailwind stage runs `npx --yes tailwindcss@3.4.10` with no lockfile, so it cannot build offline. |

---

# PART 5 — NEW FACTS ABOUT ALREADY-KNOWN ITEMS

Reported per the brief's rule (new information only):

1. **Unmasked MPSK passphrase** — the fix already exists as a function. `mcp_servers.shared._is_sensitive_key('mpsk')` returns **True** (as do `key` and `subscriptionKey`). Routing the Lab result renderer / the GLP wrappers through `redact_sensitive` masks all three in one pass.
2. **`devices.py _wireless_cards` channel_util null** — there is a *second, deeper* cause at `mcp_servers/monitoring.py:1602` (wrong key names: `utilization`/`noise`/`txPower` vs `channelUtilization`/`noiseFloor`/`power`), so a corrected `devices.py` alone would still render nulls. **And** the correct values are already in hand under `result["raw"]["radios"]`, so the fix needs no upstream change and no extra call.
3. **`/lab/device-scope` empty gateway charts** — the working diagnosis is wrong. `get_ap_trends` always passes `device_type="AP"` (`central_bridge.py:895`), so centralmcp's type detection never runs and the missing GATEWAY branch is never exercised. The AP endpoints answer **HTTP 200** on the gateway with well-formed payloads whose every sample is `[None]`. `normalize_device_trends` reports `ok=True, series=['cpu'], all_none=True`, so `trends.has("cpu")` is True and a chart card is built with no data. **Nothing to fix in `get_device_trends`' branching** — make `normalize_device_trends` treat an all-None series as absent so the existing `trend_error` empty state renders.
4. **`platform.py` NAC em-dashes** — two of the three columns are recoverable from data already in hand: `description → r['displayName']`, `status → 'enabled'/'disabled'` from `r['enable']`. **Role has no source at all**; `staticTags` is the nearest equivalent. Payload union keys across 28 records: `autoCreated, createdAt, displayName, enable, id, macAddress, modifiedAt, staticTags, type`.
5. **Startup cache-warm 429 burst** — see D5. The mechanism is not just "a burst": ~13 of 16 requests warm keys **no route can ever read**, because of default-vs-explicit key fragmentation plus a `siteName`/`scopeName` spelling mismatch, doubled by two workers.
6. **Classic Central 401** — see D4. New: no refresh-on-401 (a ~2-hour dead window after *every* restart, `central_bridge.py:55`), a 30 s timeout that can block a page render, `ok:true` reporting on both write paths, and a working New Central `device-groups` endpoint sitting unwired.
7. **The four/five known-404 tools** — new operational detail: `get_ap_rf_neighbors` and `locate_client` both cache their empty/error result at the **5 s low-confidence TTL**, so their 3 and 4 404s respectively are re-issued on essentially every AP and client page view rather than amortised. `get_ap_rf_neighbors`' `invoke_tool_router` fallback branch (`:1015-1018`) is **unreachable dead code**.
8. **`get_wireless_metrics` un-unwrapped envelope** — quantified: on an AP it returns `errors: []` and keeps the 60 s TTL; on the gateway it returns three 404s in `errors` and drops to **5 s**. The module docstring at `:866-871` justifying the envelope return ("ALWAYS carry a non-empty errors[] on success") is factually false on this tenant.

---

# PART 6 — FIX THESE FIRST (8)

| # | Fix | Size | Why first |
|---|---|---|---|
| 1 | **`get_devices`: filter by site/type client-side** (`central_bridge.py:597`) | ~15 lines | Un-blanks the site page, the topology site filter and `get_site_health_summary`'s device counts in one edit. Data proven present (`siteId`/`siteName` on unfiltered records). Highest blast radius per line. |
| 2 | **Tailwind: CDN fallback + track `dist/` + pass `USE_BUILT_TAILWIND` through compose** (`templates_shared.py:14`, `.gitignore`, `docker-compose.yml`) | ~20 lines + config | The only thing standing between a rebuild/migration and a completely unstyled portal, and it is invisible in the repo (`.git/info/exclude`). |
| 3 | **`assign-group`/`assign-site`: return `ok:false` on non-2xx; `get_device_groups`/`get_classic_sites`: return `None` on HTTPError; `_expires_at = 0` + refresh-on-401; timeout 30→5 s** (`devices.py:718/736`, `central_bridge.py:55/81/93/1254/1273`) | ~25 lines | Stops the portal lying about writes, stops two uncached 401s per `/devices` load, and removes the guaranteed 2-hour dead window after every restart. Also the prerequisite for testing whether the Classic credential is merely stale. |
| 4 | **Firmware compliance: reuse `lab.py:922`'s rule, emit `unknown`, return a bare list** (`central_bridge.py:1173/1176`) | ~10 lines | The two firmware pages disagree by 2× and the wrong one looks authoritative. The envelope fix also stops a 60 s cache of an empty result when the sunset endpoint finally dies. |
| 5 | **Cache-key normalisation (`sig.bind` + `apply_defaults`) + `warm_cache` `scopeName` fix + single-worker warm election** (`central_bridge.py:471`, `:213`) | ~40 lines | Kills the largest known source of 429s: ~26 wasted upstream requests per cold start into endpoints that rate-limit at 8-14%, plus duplicate steady-state requests on every page. |
| 6 | **`/healthz` returns 503 when the scheduler is `unelected`** (`main.py:412`) | 3 lines | Today a portal with no background jobs anywhere — no device-down alerting — reports `Up (healthy)` forever. Cheapest high-value signal in the list. |
| 7 | **Pool: `ThreadedConnectionPool(10, 10)` + `connect_timeout`/`application_name`/`keepalives` in `_parse_dsn` + merge DSN query params + `assert minconn` in the test** (`db.py:52`, `:26`, `tests/test_db_pool.py:29`) | ~10 lines | ~100× measured on checkout latency, removes the connect convoy under the pool's global mutex, and closes the "unreachable Postgres wedges a worker for 130 s" path. |
| 8 | **Render-bundle: `/wlans/` essid/VLAN/enable, `/platform/nac` displayName/enable, `/platform/config` running-config via `cx_show` with masking, GLP `data.items`, LLDP + Find-MAC command strings** (`wlans.py:26/29/30`, `platform.py:91-95/170`, `central_bridge.py:1197/779/…`) | ~40 lines total, all one-liners | Six user-visible wrongs — including a raw Python dict on the WLAN page and another in the config viewer — that are all a key name or a command string. Pair with the `test_bridge_contract.py` config fixtures (D10) or they will regress. |

**Same-edit notes:** #1 also fixes the device counts inside #4's neighbourhood (`get_site_health_summary`). #5's key normalisation and the `warm_cache` fix must ship together or the warm stays broken. #8's `platform/config` fix and S13's masking are the same edit. Fixing D9's stamp bug is a prerequisite for ever moving `audit_log` into `MIGRATIONS`.

---

# PART 7 — MUTATION EXPERIMENTS (the follow-up run)

Consolidated from all delivered areas, deduped, ordered by value. **Two experiments were settled read-only by refuters and are removed:** the auth fail-open (proven end-to-end through the real middleware with a starlette TestClient) and the stamp-every-version bug (proven by driving `run_migrations()` against a fake connection). The `minconn` A/B was also settled read-only by benchmarking throwaway pools; only the in-app change remains.

### Tier A — settle root causes (do these first)

**A1. Is the Classic Central 401 just a stale access token?** *(highest-value single test on this list)*
```
docker exec -i netlab-app python3 -c "from vendors.central_bridge import get_classic_client; get_classic_client()._refresh()"
```
- **Success →** `get_device_groups`, `get_classic_sites` and `assign_device_to_site` all start working; several "known" items dissolve and the D4 fix set shrinks.
- **400/401 →** the credential itself is dead; the group/site pickers must be rebuilt on New Central (`config.list_device_groups` + template key changes) and the Classic path deleted.
- **Risk:** POSTs to `/oauth2/token` and **consumes the single-use refresh token**. If it fails you cannot retry without re-minting credentials in Central. Back up `.env` first.

**A2. Is the device-inventory 400 a param-shape problem or a missing capability?**
```
docker exec -i netlab-app python3 -c "
from mcp_servers.shared import get_client
print(get_client()._request('GET','/network-monitoring/v1/device-inventory',
      params={'filter':\"siteId eq '79244870000394240'\"}))"
```
Repeat for `/network-notifications/v1/alerts` with `filter=status eq 'Active' and siteId eq '…'`.
- **200 with 13 rows →** centralmcp is sending the wrong parameter shape; the fix belongs upstream in `MCPClient.get_devices_page`, not in the portal, and #1 in the fix list changes.
- **400 →** no server-side site filter exists; client-side filtering is the only option (proceed with fix #1 as written).
- **Risk:** none — read-only, hand-crafted request. *(Listed here rather than as read-only work because it bypasses the wrapper layer.)*

**A3. Does key normalisation actually collapse the duplicate upstream requests?**
Patch `_cached`'s key construction to bind against the signature, then with a cleared cache run `get_sites(); get_sites(limit=4); get_sites(limit=100)` and `list_wlans(200); list_wlans(50)` with the request log instrumented.
- **1 request per group (vs 3 and 2) →** confirmed; quantifies the saving and validates fix #5.
- **Still 3 and 2 →** the fragmentation comes from somewhere other than defaults; re-diagnose before shipping.
- **Then** verify the `warm_cache` half: after the `scopeName` fix, assert each of the 12 warmed keys matches a key some route actually requests.
- **Risk:** code change + restart; read-only against the tenant.

**A4. Does `UVICORN_WORKERS=1` halve the warm burst and the 429s?**
```
# docker-compose.override.yml: UVICORN_WORKERS=1
docker compose up -d app
docker logs netlab-app --since <epoch> | grep -c 429      # NOT a naive timestamp — logs are UTC, host is CDT
```
- **Each warm URL once, materially lower 429 count →** per-worker cache duplication confirmed; single-worker warm election is the right fix.
- **Unchanged →** the burst is fan-out inside one worker; worker count is a red herring. (Expect *partial*: some warm URLs appear 3-4×, so halving is the ceiling, not elimination.)
- **Risk:** halves request concurrency for the duration; revert after.

### Tier B — verify the specific fixes

**B1. Pool `minconn`.** Set `ThreadedConnectionPool(10, 10)` in `db.py:52`, restart, then run 4 concurrent `get_conn()` reporting `pg_backend_pid()` twice. **Same four pids on the second pass and `len(pool._pool) == 10` →** confirmed. Re-run a 200-concurrent `/healthz` burst and compare the `pg_stat_database.sessions` delta against the ~44 measured today; it should approach zero. **Risk:** none — 2 workers × 10 = 20 of 100 `max_connections`.

**B2. Pool exhaustion through a real route.** Temporarily set `maxconn=2`, restart, fire 10 concurrent authenticated GETs at `/lab/activity` and 30 at `/notifications/api/recent`. **Expect** `/lab/activity` to render a db_error banner (not a 500) and `api/recent` to return 200 with `{"items": [], "unread": 0}` — confirming the *silent* failure mode is the real one. **Risk:** degrades the portal briefly; revert.

**B3. LLDP + Find-MAC.** Rewire `central_bridge.get_lldp_neighbors → cx_show(serial, ['show lldp neighbor'])` and `find_mac_on_switch → get_cx_mac_table(serial)` + text filter. `POST /devices/SG30LMR164/lldp` → **expect 10 neighbour entries**. `POST /devices/SG30LMR164/find-mac` with `00:0b:86:b8:c4:b8` → **expect VLAN 200, port 1/1/17**. **Failure mode to watch:** `devices.py:560` expects `result["neighbors"]` as a list of dicts and will fall to the raw-text branch — still an improvement, but tells you the parser needs a text path. **Risk:** opens a device session per click (~5.7 s), no writes.

**B4. RF card.** Change `devices.py` to read `channel_util["raw"]["radios"]` (or drop the call and use `get_ap_radios`), reload `/devices/PHT5M520SZ`. **Expect** utilisation ~18-21%, noise floor -98 dBm, channel quality 95-96. Also assert the request log shows **one** GET to `/aps/{serial}/radios`, not two.

**B5. Running config.** Point `get_device_running_config` at `ops.cx_show(serial, ['show running-config'])`, add masking for the `password ciphertext` line, `POST /platform/config/running`. **Success = 16,950 chars of config AND the ciphertext line redacted.** Do not ship the first half without the second.

**B6. GLP catalog.** `central_bridge.py:1197` → `data.items`, reload `/lab/greenlake`, open the catalog tab. **Expect 21 rows and a badge of 21.**

**B7. Envelope unwrapping.** Change `get_ap_radios` to `_payload(await _run(...), "radios")`, reload an AP detail page. **Panel still populates →** `devices.py` reads defensively and the other seven can follow. **Blank →** call sites reach into `result["radios"]` and must be updated in the same commit. Same shape for `get_wireless_metrics`.

**B8. Gateway trend cards.** Make `normalize_device_trends` drop an all-None series, load `/lab/device-scope?serial=CNP6L2H02W`. **Expect the existing `trend_error` empty state instead of two blank chart cards.**

**B9. Semaphore.** Move `async with _upstream_semaphore()` from around the whole callable to around each HTTP request (or exempt `mcp_servers.ops`), re-run `gather(get_cx_arp_table ×6, get_device ×2)`. **`get_device` back to ~1 s (from 6.1 s) →** the poll wait was the blocker. **Still ~6 s →** the shared thread-pool executor is the real constraint and the fix is different.

### Tier C — infrastructure and posture

**C1. Healthcheck blindness.** `docker stop netlab-db`, wait 90 s, then `docker inspect netlab-app --format '{{.State.Health.Status}}'` and `curl -s localhost:8200/healthz`. **Still `healthy` + HTTP 200 with `"db":"fail"` →** confirmed. Gentler variant that isolates pool recovery from server restart: `SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='netlab' AND application_name <> 'psql'` — then watch whether the scheduler's dedicated lock connection returns and `main.py`'s re-election loop fires. **If it does not, device-down alerting is silently off for the life of the container.** `docker start netlab-db` to restore. **Risk:** portal degraded ~90 s.

**C2. Fresh-clone unstyled test.** `git clone` to a scratch dir, copy `.env`, `docker compose -f docker-compose.yml -p nctest up -d --build` (**deliberately without the override**), then `curl -sI :<port>/static/dist/tailwind.css` and `curl -s :<port>/login | grep -c tailwindcss-play`. **404 + 0 Play-CDN tags →** confirmed high severity. While that stack is up, also settle S1: `nc -z <nas-ip> 5432` from a second LAN host and `psql postgresql://netlab:netlab@<nas-ip>:5432/netlab -c 'select 1'`. **Tear down with `docker compose -p nctest down -v`.** **Risk:** a second stack on the host; must use a distinct project name and remapped ports.

**C3. Does the image still build?** `docker compose build --no-cache app` then `docker run --rm --entrypoint sh <image> -c "grep -c surface-700 /app/static/dist/tailwind.css"`. **Build fails on the npx step →** the no-lockfile / offline gap is urgent. **Risk: this replaces the image the live container was created from** — do it last, or with a tagged copy.

**C4. Claude assistant.** `claude setup-token` on the host → `CLAUDE_CODE_OAUTH_TOKEN` in `.env` → restart → `POST /assistant/chat`. **Reply →** blanked OAuth was the sole cause. **Still rc=1 →** something else (the `/claude-cfg` mount as uid 999, or `--strict-mcp-config`). Consider remounting `.credentials.json` read-only in the same pass so a failed refresh cannot blank the host copy again.

**C5. `FORWARDED_ALLOW_IPS`.** Add `- FORWARDED_ALLOW_IPS=192.168.128.1` to the app env, restart, then from another LAN host `curl -H 'X-Forwarded-For: 8.8.8.8' -X POST …/login` and read the audit line. **Still `8.8.8.8` →** `security.py:174` is what matters and uvicorn's middleware is irrelevant; the fix must be in `security.py`. **Real client IP →** a `request.client.host` change in `security.py` becomes sufficient.

**C6. `SOURCE_BASE_URL` override through the live app.** Add `SOURCE_BASE_URL=https://example.invalid`, restart, watch the first upstream call. **Targets example.invalid →** the undocumented override is live in the real request path and `ARUBA_CENTRAL_BASE_URL` is definitively decorative. **Still internal.api →** the MCP layer resolves credentials differently and the finding narrows. **Risk:** breaks all fetches until reverted.

**C7. `USE_BUILT_TAILWIND=0` in `.env`** → `docker compose up -d app` → check whether `base.html` emits the dist link or the CDN script. **Still dist →** compose genuinely does not pass it.

**C8. Invalid setting = crash-loop?** `LOG_LEVEL=verbose` (or `DEVICE_FETCH_LIMIT=lots`) → `docker compose up -d app` → `docker inspect netlab-app --format '{{.State.Status}} {{.RestartCount}}'`. **Restart loop with a traceback →** confirms the gap against the "never raises" contract.

**C9. Generated toolset.** Set `CENTRALMCP_TOOLSETS=central,central-generated,glp,rag`, restart, assert `len(tool_router._tool_index)` jumps 291 → ~1,638 and `POST /lab/mcp-tester tool=central_read_ap_system` succeeds. **⚠ This also indexes generated WRITE tools.** Verify `invoke_read_tool` still refuses them and that `central_write_*` requires `dry_run=False` + `confirm=True` before executing. **Neither auditor read that gate — read `_write_is_enabled` / `_optional_write_disabled` first.** Do not do this before deciding the write posture.

**C10. UXI.** Set `UXI_CLIENT_ID`/`UXI_CLIENT_SECRET` → `uxi_status` (expect `configured: true`) → `uxi_list_sensors` (expect CNNCKYT02W). **Empty list despite configured:true →** the sensor is not entitled (it has no GLP subscription), distinguishing "credentials missing" from "not entitled".

### Tier D — scratch database only (never `netlab`)

**D-a. `alert_rules` seed race.** Empty scratch DB, two processes calling `db.init_db()` simultaneously. **`count(*) = 2` →** the check-then-insert at `db.py:421-428` races. (Consequence is cosmetic — `_matching_rule` picks one — but it proves the docstring is false.) While there, run two concurrent `security.ensure_audit_schema()` calls and see whether either raises the `pg_type` duplicate-key error that concurrent `CREATE TABLE IF NOT EXISTS` can produce.

**D-b. Migration end-to-end.** Add the indexes from the retention plan as migration version 2 against a scratch DB **after** fixing the stamp bug, and confirm they actually execute. This would be **the first time the migration framework has ever run in anger** — no "Applied schema migration" line appears anywhere in the retained container log.

### Tier E — tenant-side (requires changing Central state)

**E1. Does device-down alerting fire?** Unplug a device, wait past `offline_minutes=5`, confirm a row lands in `device_status_history` (0 lifetime inserts in 40 days) and `in_app_notifications` (0 inserts), and that `list_active_alerts` becomes non-empty. This is the only way to settle both "is the sweep's write path exercised" and "does `status eq 'Active'` ever populate". Pairs naturally with the device-down alerting work.

**E2. Rate-limit headroom per endpoint family.** 25 simultaneous GETs against each of `device-inventory`, `events`, `config-health/devices`. **Deliberately trips the limiter and degrades the portal for anything else running** — schedule it alone. Note the "per endpoint family" claim in the reports is **thin**: one 429 on one path is equally consistent with a single global bucket. Treat "10/sec global" as the safe assumption until this runs.

---

# PART 8 — NEEDS A HUMAN / COULD NOT BE VERIFIED

**Structural gaps in this synthesis**

1. **Three of nine area reports were not delivered.** Whatever they covered is absent.
2. **The deployment area has no refuter verdicts.** Its 21 findings are marked ⚠︎unrefuted above and carry original severities. Two of them (`docker-compose.override.yml` is load-bearing; healthcheck blindness) are high/medium and made the fix list on the strength of one auditor's evidence alone. Cross-corroboration exists for its Tailwind and Claude-CLI findings (independently confirmed by the runtime-config refuter) but not for the rest.
3. **The central-config-api report's `needs_mutation_test` section was truncated.** Its findings are included; its own proposed experiments are lost. B3/B5/B6 above were reconstructed from other areas.

**Genuinely unresolvable from this host**

4. **The production reverse proxies.** Caddy runs on a separate server and OPNsense injects the CSP. Every request in this audit went straight to uvicorn on `:8200`. Nobody could observe what those proxies add, strip or forward — so S10 (does `X-Forwarded-Proto` reach the app behind TLS?) and the CSP interaction with the woff2 MIME type are **stated from code, not from observed production traffic**.
5. **Host firewall policy.** `iptables` is not on PATH on this DSM build and `/usr/syno/etc/firewall.d/` was not readable. Reachability was established empirically (8200 open, 5432 refused off-box); the rule *set* was never read.
6. **Whether `ARUBA_CENTRAL_ACCESS_TOKEN` would work with a real token.** There is no code path that consumes it, so the only meaningful experiment is to build one.
7. **Docker's on-disk log file size.** `/volume1/@docker/containers/…-json.log` returned permission denied; the 746 KB figure counts the decoded stream, so D18's growth estimate is a **lower bound**.

**Measurement caveats that affect specific numbers**

8. **Rate-limit attribution.** All 429 tallies come from a log window shared with eight concurrent agents. Per-endpoint *rates* are characteristic of the endpoint; nobody could separate their own contribution. The 6-hour counts drifted upward during the audit (429s 138→139, events 51→53, device-inventory 35→36) — consistent, not contradictory.
9. **`audit_log` growth rate.** The pre-audit window (17 rows / 40.8 days = 0.42/day) is the honest figure; the table reached 86 rows during the audit purely from agent traffic.
10. **The 5,270-tool registry count** was measured by walking `_tool_manager`, but only 24 of `central_generated`'s 448 read tools were sampled (~50% returned an empty `{}`). No reliable extrapolation to the full set.
11. **`ops.list_show_commands` is partly inaccurate** — it lists `show lldp neighbor-info` as permitted and the device rejects it. Nobody enumerated how much of the 141-command allowlist is similarly wrong; that would cost 141 × 5.7 s of device sessions.
12. **Per-page cold cost.** Quoted per-page request totals (e.g. ~23 for the client detail page) are **sums of independently measured wrapper costs**, not a single observed cold render — `clear_bridge_cache` is not exposed on any route and restarting was out of scope.
13. **No auditor ever observed a migration execute.** The framework's ordering, idempotency and partial-failure behaviour are reasoned from `db.py:340-375` plus a test that asserts against a fake.

**Not examined by anyone**

14. **The write-safety posture.** 107 unused write/destructive tools exist inside the reachable 291. `_optional_write_disabled`, `_write_is_enabled` and the `dry_run`+`confirm` gate in `tool_router` were **never read, even statically** — by either the tool-coverage auditor or its refuter. Since experiment C9 proposes enabling the generated toolset, this is the single most important thing to read before recommending it.
15. **All 12 write wrappers** in `central_bridge` (`run_show`, `run_ping`, `run_traceroute`, `run_reboot`, `move_device_to_group`, `assign_device_to_site`, the five GLP assign/add functions). Only static reachability is known. Flagged without evidence: `run_ping`'s gateway branch routes through `gateway_show(serial, [f"ping {destination}"])` — a **string-interpolated CLI command** and therefore a command-injection surface that nobody tested.
16. **The SMTP path.** `smtp_host`/`user` are empty so `_send_email` returns False before touching the network; the `use_tls = cfg["tls"] != "false"` coercion issue is established by reading and by DB values only.
17. **The Redis/Ollama RAG backend.** `CENTRALMCP_RAG_BACKEND` unset, no Redis Stack running — the claim that `OLLAMA_URL` would be ignored rests on grep, not a failed embed call.
18. **Whether the two third-party devices in `get_topology`** (`tpd_204c03ff61e2`, `tpd_204c03ff8c8a`, health Unknown, 5 Gbps and 2.5 Gbps) are real gear or Central placeholders. They appear only in topology, never in device-inventory.
19. **Timezone rendering.** Both containers run UTC, the host is CDT, and the code is consistently timezone-aware (`datetime.now(timezone.utc)` throughout, `home.py:619` explicitly labels output "UTC"). No rendering bug found, but not every timestamped view was clicked through.

**Operational note for the next run:** every agent on this host shares one source IP, and `LoginRateLimiter` is 10 failed attempts per 300 s **per IP** with the check *before* password verification (S3). One agent's bad password locks out the operator and every other agent for five minutes. The read-only workaround used successfully was minting a session token directly via `security.create_session_token()` inside the container rather than retrying logins.---

## Appendix A — Portal HTTP surface (every route)

_The portal exposes 84 HTTP operations across 14 routers plus 4 app-level endpoints. Every route in the smoke-test list still returns 200 against the live tenant, and every GET route I could reach (54 of them) returned 200/303/404 as designed — I found no route that 500s on a malformed path parameter. Auth gating is uniformly correct: the middleware catches everything except six deliberate exempt paths, forged cookies are rejected, and CSRF (Origin/Referer vs Host) is genuinely enforced on unsafe methods. The real damage is concentrated elsewhere. The shared ops renderer (`ops_format.format_ops_response`) never unwraps centralmcp's ops job envelope, so eight user-facing operational buttons — LLDP, port errors, traceroute, MAC table, ping, STP, ARP, find-MAC fallback — render raw JSON, a blank table row, or a green ping painted red, and all of them leak the internal Central endpoint path to the user. Four JSON POST routes return an unhandled 500 (HTML error page, to a JSON API) on type-confused request bodies, and one leaks the raw Python exception string. Three HTML page routes return raw FastAPI 422 JSON instead of the themed error page when a query parameter fails coercion. Measured load behaviour: 13 concurrent device-detail loads produced 23 upstream 429s and stretched p100 latency from 0.6 s to 17.0 s with no request deadline, and one AP detail render costs 12 upstream calls, three of which are already-known dead 404 endpoints._

| Sev | Kind | Finding | Location |
|---|---|---|---|
| high | defect | format_ops_response never unwraps the centralmcp ops envelope — eight operational panels render raw JSON instead of command output | `app/ops_format.py:36` |
| high | defect | Ping panel dumps a raw Python dict repr and paints a successful ping red | `app/routes/devices.py:484` |
| medium | defect | MAC table always renders exactly one completely blank row | `app/routes/devices.py:661` |
| medium | defect | Four JSON POST routes return an unhandled 500 (as an HTML error page) on a type-confused request body | `app/routes/devices.py:712` |
| medium | defect | HTML page routes return raw FastAPI 422 JSON instead of the themed error page when a query parameter fails coercion | `app/errors.py:101` |
| medium | dead-code | The notifications page's database-unavailable banner is dead — the route computes it, the template never renders it | `app/routes/notifications.py:238` |
| medium | misconfiguration | Login rate limiter is per-worker with UVICORN_WORKERS=2, so the failed-login budget is double the configured value and correct passwords are rejected during lockout | `app/security.py:245` |
| medium | opportunity | Device detail for an AP costs 12 upstream calls including three known-dead 404 endpoints and a duplicated radios fetch; 13 concurrent loads produce 23 upstream 429s and 17 s latency with no request deadline | `app/routes/devices.py:285` |
| low | security | Eight ops handlers and five GreenLake handlers echo the raw Python exception string to the user | `app/routes/devices.py:458` |
| low | security | SMTP password is serialized into the notifications page HTML (currently empty, so no live exposure) | `app/templates/notifications.html:363` |
| low | misconfiguration | JSON endpoints carry no Cache-Control at all, while every HTML response is explicitly no-store | `app/main.py:340` |
| low | gap | No security response headers on any route | `app/main.py:355` |
| low | security | /lab/mcp-tester has no portal-side tool allowlist — it forwards any tool name to centralmcp and relies entirely on the upstream readOnlyHint annotation | `app/routes/lab.py:528` |
| low | dead-code | Six context keys are computed and passed by routes but referenced by no template | `app/routes/lab.py:759` |
| low | defect | Device Deep-Dive tier-1 fragments accept any serial, including access points and nonexistent devices | `app/routes/lab.py:764` |
| low | defect | aruba_central silently substitutes a mock fleet built from this tenant's real serials when centralmcp fails | `app/vendors/aruba_central.py:207` |
| low | misconfiguration | /healthz reports a per-worker scheduler role, so with two workers the value flaps and cannot be alerted on | `app/main.py:426` |
| info | security | Unauthenticated endpoints disclose deployment state, and the vendored SecureSSID tool is served without auth | `app/main.py:261` |
| info | gap | CSV upload reads the whole file into memory with no size limit | `app/routes/lab.py:1617` |
| info | defect | notifications.save_settings runs blocking psycopg2 writes directly on the event loop | `app/routes/notifications.py:254` |

## Appendix B — Scheduler, notifications & AI/RAG

_The scheduler is the healthiest part of this area: leader election via a Postgres advisory lock works correctly and was observed live (one worker reports `leader`, one `follower` on /healthz), three jobs are registered, and the device-down sweep has run every 60s without a single abort or misfire across a 5h38m log window. Its measured upstream cost is exactly 60 Central calls/hour (one `device-inventory?limit=200` per minute) — the other two jobs cost zero because they short-circuit on empty config. The notification engine is code-complete and its escalation ladder and dedup keys are correct, but it is entirely inert in production: SMTP host/user/password are all empty strings and `alert_recipients` has zero rows, so every email path is a logged no-op and device-down alerting is in-app only. The AI layer is completely dead: the configured backend (`claude_cli`) has empty OAuth tokens in its mounted credentials file, and both fallbacks (`GITHUB_TOKEN`, `ANTHROPIC_API_KEY`) are still the literal `.env` placeholders — all four AI surfaces return their "unavailable" copy, gracefully, with HTTP 200. RAG is the one AI-adjacent thing that genuinely works and is fully local (LanceDB + SQLite + in-process ONNX, zero network egress), but the installed index is the v0.2.8 artifact under centralmcp v0.4.0, and the OpenAPI exact-lookup index behind /lab/doc-api carries roughly a third of the endpoints the v0.4.0 release index ships. On the question that matters most: no credential can reach an email body or an LLM prompt — verified by rendering all four email templates and by materialising the actual assistant system prompt — but tenant inventory (device names, bare serials, types, statuses, site names, client counts) is constructed for every assistant turn and would go to api.anthropic.com or models.inference.ai.azure.com the moment a backend is repaired._

| Sev | Kind | Finding | Location |
|---|---|---|---|
| high | defect | Every AI backend is non-functional: claude_cli OAuth tokens are empty and both fallbacks are literal .env placeholders | `docker-compose.override.yml:22` |
| medium | defect | claude CLI failure reason is unloggable — the CLI writes its error to stdout but only stderr is captured | `app/routes/assistant.py:262` |
| medium | defect | /lab/health-report gathers the entire live network before checking whether it has an API key | `app/routes/lab.py:557` |
| medium | misconfiguration | The RAG/OpenAPI indexes are the v0.2.8 artifact running under centralmcp v0.4.0 — lookup_api has ~31% of the endpoints the v0.4.0 index ships | `data/INDEX-MANIFEST.json:2` |
| medium | security | The SMTP password is embedded verbatim in the /notifications/ page HTML | `app/templates/notifications.html:363` |
| medium | misconfiguration | Notification email is entirely inert: no SMTP server and no recipients, so device-down alerting is in-app only | `app/notifications.py:112` |
| medium | gap | Scheduler liveness is unobservable: db.scheduler_lock_healthy() has no callers and /healthz reports a static role string | `app/db.py:116` |
| medium | defect | All three scheduled jobs inherit APScheduler's 1-second misfire grace, so a brief stall silently skips a whole day's expiry check or report | `app/main.py:110` |
| low | defect | The device-down email promises re-alerting after the cooldown, which the code makes impossible for an ongoing outage | `app/notifications.py:824` |
| low | security | Device, site and alert names from Central are interpolated unescaped into HTML email bodies | `app/notifications.py:813` |
| low | gap | Five of thirteen devices are permanently un-alertable — baseline-offline with a NULL offline_since that survives every restart | `app/notifications.py:648` |
| low | security | /assistant/chat is excluded from the audit log — the one route that ships tenant inventory to a third party is the one that is not audited | `app/main.py:265` |
| low | defect | The summary report's 'alerts sent' line counts emails, not alerts, so it is structurally always zero here | `app/db.py:656` |
| low | gap | device_status_snapshot.updated_at is not a heartbeat — it freezes after the boot sweep | `app/notifications.py:591` |
| low | defect | Assistant backend defaults disagree between the resolver and the settings save handler | `app/routes/lab.py:123` |
| low | defect | /lab/health-report uses a hardcoded placeholder string instead of config.is_placeholder() | `app/routes/lab.py:610` |
| low | security | The Claude CLI child process inherits the portal's entire secret set in its environment | `app/routes/assistant.py:238` |
| low | security | Live token caches for GreenLake and Central sit in a world-writable host directory | `docker-compose.override.yml:33` |
| info | dead-code | The centralmcp 429 backoff override at startup is now a silent no-op | `app/main.py:67` |
| info | gap | No test coverage for routes/assistant.py or the RAG routes | `app/routes/assistant.py:1` |
| info | opportunity | Every doc query re-opens the LanceDB directory and emits a read-only-filesystem warning | `mcp_servers/rag.py:102` |
| info | defect | Lab menu copy attributes the Network Chatbot to Claude when it calls gpt-4o via GitHub Models | `app/routes/lab.py:28` |

