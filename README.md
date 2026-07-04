# New Central Portal

A modern, self-hosted operations portal for HPE Aruba Networking Central. It puts a live dashboard, an interactive 3D network topology, a 3D switch visualization, an AI assistant, and a device-down alerting/automation engine on top of your Central environment — all in a fast, dark-themed web UI you run yourself with Docker Compose. Every integration degrades gracefully: missing credentials disable a feature (or fall back to mock data) instead of breaking the app, so you can explore the full UI before wiring up a single API token.

## Features

- **Dashboard** — fleet-wide stats with SVG donut and mix charts (online/offline, device types, wired/wireless clients), a merged recent-events feed from your busiest devices, tenant/site health widgets, and an HTMX partial that auto-refreshes the live section every 30 seconds (lite mode skips expensive widgets on poll).
- **Devices** — searchable inventory with bulk group and site assignment (Classic Central), plus a drill-down detail page featuring an interactive **3D switch faceplate** (Three.js + OrbitControls) that renders real port state: link, PoE, uplinks, speed, and LLDP neighbours. Run validated `show` commands, ping from the device, or reboot it — output streams into the page via HTMX.
- **Clients** — wired and wireless client list with server-side type tabs (`?type=wireless|wired`), search, pagination, and per-client detail including the connection path (client → AP → uplink switch) resolved live from switch port data.
- **3D Topology** — force-directed 3D graph of your network (3d-force-graph/WebGL) built from live LLDP neighbour data. Status/type/site filters, shift+click focus mode to isolate a node's neighbourhood, link colors by wired speed tier, and one-click PNG export.
- **Sites** — site grid with device and client counts pulled from Central.
- **WLANs** — read-only SSID/WLAN inventory with search (`/wlans/`).
- **Platform** — NAC MAC registration viewer and config/firmware tools: structured firmware compliance table plus read-only running-config fetch (`/platform/nac`, `/platform/config`).
- **Notifications & Automation** — a background automation engine (APScheduler) with:
  - Device-down alert rules: per-site and per-device-type filters, configurable offline threshold and cooldown, evaluated every 60 s (tunable).
  - In-app notification bell with unread counts and mark-as-read.
  - Email alerts and scheduled daily/weekly summary reports over SMTP (configured in the UI, test-send included).
  - License/subscription expiry checks against GreenLake (daily) and SSL certificate expiry monitoring for hosts you list.
- **AI Assistant** — a chat drawer available on every page, grounded with a just-fetched snapshot of your devices and clients, so it answers questions about *your* network. A global command palette (Ctrl+K / Cmd+K) searches devices, clients, sites, alerts, and WLANs instantly.
- **Lab** — a sandbox of self-contained experiments: network chatbot with RAG (centralmcp LanceDB index) and MCP tool calling, semantic doc search, MCP tool tester, self-healing simulator (dry-run), AI health report, config viewer, ping tester, alert dashboard, client fingerprints, and a GreenLake Platform explorer.
- **Platform** — optional session login (`PORTAL_PASSWORD`), `/healthz` liveness + DB check for orchestration, responsive mobile layout with slide-in sidebar, accessibility-minded markup (ARIA labels, keyboard navigation, focus management), and defensive error handling throughout: API failures log and degrade, they don't 500.

## Screenshots

UI screenshots are optional — add PNG captures under `docs/screenshots/` when you want README previews. Expected filenames:

- `dashboard-desktop.png` — dashboard with live stats and events feed
- `device-detail-desktop.png` — device detail with 3D switch faceplate
- `topology-desktop.png` — 3D topology view
- `notifications-desktop.png` — notification settings
- `assistant-palette-desktop.png` — AI assistant drawer and command palette

Until those files exist, the links below will 404 in GitHub's preview — that is expected.

## Architecture

```
Browser ──► Caddy (:80/:443) ──► FastAPI app (:8000)
            zstd/gzip,            ├─ Jinja2 + HTMX + Alpine.js + Tailwind (server-rendered UI)
            security headers      ├─ APScheduler — device-down checks, expiry checks, summary reports
                                  ├─ vendors/aruba_central ───► central_bridge (normalization + mock fallback)
                                  ├─ vendors/central_bridge ──► centralmcp monitoring/ops/config/nac/glp
                                  │                          ──► Classic Central OAuth2 (groups, sites)
                                  │                          ──► centralmcp RAG (LanceDB; optional Redis + Ollama)
                                  ├─ GET /api/status ─────────► DB + data-source probe (live vs demo banner)
                                  └─ PostgreSQL 16 + pgvector — settings, alert rules, notification
                                     history, device status snapshots, report schedule
```

```
New-Central-Portal/
├── app/
│   ├── main.py               # FastAPI entry point, lifespan, scheduler wiring
│   ├── config.py             # Settings (pydantic-settings) + startup validation
│   ├── db.py                 # PostgreSQL pool, schema bootstrap, queries
│   ├── pagination.py         # Shared list pagination helper
│   ├── notifications.py      # Alert engine, expiry checks, email + reports
│   ├── routes/               # One module per section
│   │   ├── home.py           #   dashboard (+ 30s HTMX live fragment, lite poll)
│   │   ├── devices.py        #   list/detail, show/ping/reboot, group & site ops
│   │   ├── clients.py        #   list/detail with uplink resolution
│   │   ├── topology.py       #   3D graph data from LLDP neighbours
│   │   ├── sites.py          #   site list + detail (site_id-scoped fetches)
│   │   ├── wlans.py          #   WLAN/SSID inventory
│   │   ├── platform.py       #   NAC + firmware compliance + running config
│   │   ├── alerts.py         #   unified Central + portal alerts hub
│   │   ├── status.py         #   /api/status connectivity probe
│   │   ├── notifications.py  #   rules, recipients, reports, in-app bell API
│   │   ├── search.py         #   Ctrl+K command palette API
│   │   ├── assistant.py      #   AI assistant chat backend
│   │   └── lab.py            #   experiments
│   ├── vendors/
│   │   ├── aruba_central.py  # New Central client (normalization + mock data)
│   │   └── central_bridge.py # Classic Central OAuth2 + centralmcp/GLP bridge
│   ├── templates/            # Jinja2 HTML (Tailwind, HTMX, Alpine.js, Three.js)
│   ├── static/
│   ├── requirements.txt
│   └── Dockerfile
├── docker-compose.yml        # app + PostgreSQL (pgvector) + Caddy, healthchecked
├── Caddyfile                 # reverse proxy, compression, security headers
├── .env.example              # every setting, documented
└── README.md
```

## Getting started

### Prerequisites

- Docker with the Compose plugin.
- Optional: an HPE Aruba Networking Central API token (the UI serves mock data without one).
- Optional: a local `centralmcp` checkout (the companion MCP tools project) for the MCP bridge, GreenLake features, and Lab tools.
- Optional: centralmcp doc indexes built (`scripts/download_indexes.py` in centralmcp) for Lab RAG; optional Redis + Ollama for the alternate RAG backend.

### Run it

```bash
# 1. Configure
cp .env.example .env
$EDITOR .env        # set PORTAL_PASSWORD and your Central credentials

# 2. Build and start (app + postgres + caddy)
docker compose up --build -d

# 3. Open
#    http://localhost        (via Caddy)
#    http://localhost:8000   (direct to FastAPI)
```

Compose waits for PostgreSQL to be healthy before starting the app, and for the app's `/healthz` to pass before starting Caddy.

**First login:** set `PORTAL_PASSWORD` in `.env` to enable the login page, and set `SESSION_SECRET` (e.g. `openssl rand -hex 32`) so sessions survive restarts. If `PORTAL_PASSWORD` is empty, authentication is **disabled** and anyone who can reach the portal can manage devices — only do that on a trusted network.

### Local development (without Docker)

```bash
cd app
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Point at any PostgreSQL 16 instance (optional — the app starts degraded without one)
export DATABASE_URL=postgresql://netlab:netlab@localhost:5432/netlab

uvicorn main:app --reload --port 8000
```

Run uvicorn from inside `app/` — template and static paths are relative. The Docker dev setup bind-mounts `./app` and runs uvicorn with `--reload`, so code changes restart the server automatically and template edits only need a browser refresh.

### Unit tests

From the repo root (pytest changes cwd to `app/` automatically):

```bash
python3 -m pytest -q
cd app && python3 -m ruff check .
```

### Tailwind CSS

Development uses the Tailwind Play CDN script (no build step). For production, pre-build CSS:

```bash
./scripts/build-tailwind.sh
export USE_BUILT_TAILWIND=1
```

The Docker image builds `static/dist/tailwind.css` during `docker compose build` and sets `USE_BUILT_TAILWIND=1` automatically. When bind-mounting `./app` for local dev, either keep the CDN default or rebuild Tailwind on the host after template/CSS changes.

## Configuration

All settings are environment variables, documented with safe placeholders in [.env.example](.env.example). Copy it to `.env`; `docker-compose.yml` passes everything through to the app container.

| Variable | Description | Default | Required |
|---|---|---|---|
| `POSTGRES_PASSWORD` | Password for the bundled PostgreSQL service; compose derives `DATABASE_URL` from it. | `netlab` | No |
| `DATABASE_URL` | PostgreSQL connection string (set automatically inside compose; set manually for local dev). | `postgresql://netlab:netlab@db:5432/netlab` | No |
| `ARUBA_CENTRAL_BASE_URL` | New Central API gateway base URL. | empty | No — mock data without it |
| `ARUBA_CENTRAL_ACCESS_TOKEN` | New Central API access token. | empty | No — mock data without it |
| `CLASSIC_CENTRAL_BASE_URL` | Classic Central gateway base URL. | empty | For group/site management |
| `CLASSIC_CENTRAL_CLIENT_ID` | Classic Central OAuth2 client ID. | empty | For group/site management |
| `CLASSIC_CENTRAL_CLIENT_SECRET` | Classic Central OAuth2 client secret. | empty | For group/site management |
| `CLASSIC_CENTRAL_ACCESS_TOKEN` | Seed OAuth2 access token (refreshed automatically). | empty | No |
| `CLASSIC_CENTRAL_REFRESH_TOKEN` | OAuth2 refresh token used to renew access tokens. | empty | Recommended with Classic Central |
| `CENTRALMCP_PATH` | Host path to your centralmcp checkout, mounted read-only at `/centralmcp`. | — | For MCP/GLP/Lab tools |
| `CENTRALMCP_GLP_V2BETA1_WRITES` | Allow GreenLake (GLP) write operations through the bridge (`0` = read-only). | `1` | No |
| `PORTAL_PASSWORD` | Shared password for the portal login page. **Empty disables authentication.** | empty | Strongly recommended |
| `SESSION_SECRET` | Secret for signing session cookies; if unset, an ephemeral secret logs everyone out on restart. | empty | Recommended with auth |
| `SESSION_MAX_AGE_HOURS` | Login session lifetime in hours. | `24` | No |
| `DEVICE_CHECK_INTERVAL_SECONDS` | Device-down alert engine poll interval (runtime minimum 15). | `60` | No |
| `DEVICE_FETCH_LIMIT` | Max devices fetched per status poll. | `1000` | No |
| `GITHUB_TOKEN` | GitHub PAT for the GitHub Models endpoint — powers the AI assistant and Lab chatbot. | empty | For AI features |
| `ANTHROPIC_API_KEY` | Anthropic API key (reserved for Claude-backed experiments). | empty | No |
| `OLLAMA_URL` | Ollama URL when using centralmcp's Redis RAG backend. | `http://host.docker.internal:11434` | Optional Lab RAG |
| `LOG_LEVEL` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). | `INFO` | No |

SMTP settings (server, port, credentials, sender, recipients) are managed in the UI under **Notifications** and stored in the database, not in the environment.

## Operations notes

- **Health:** `GET /healthz` returns `{"status": "ok", "db": "ok" | "fail"}` and is used by the compose healthcheck; `GET /health` is a bare liveness probe.
- **Scheduler:** expiry checks run daily at 07:00, device-down checks every `DEVICE_CHECK_INTERVAL_SECONDS`, and the summary-report job evaluates hourly whether a daily/weekly report is due.
- **Degraded mode:** the app starts (with logged warnings) even if the database or any upstream API is unreachable.

## Extending

- **Integration tests:** `docker compose -f docker-compose.yml -f docker-compose.integration.yml run --rm integration-tests` (requires centralmcp mounted with valid `credentials.yaml`).

- **New Lab experiment:** add a route in `app/routes/lab.py`, a template under `app/templates/lab/`, and an entry in the `lab_menu()` experiments list. Experiments are self-contained, so one breaking never affects the others.
- **New vendor:** add a client module under `app/vendors/` with a singleton instance, then import it from the routes that need it.
