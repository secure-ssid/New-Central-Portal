# New Central Portal

A modern, self-hosted operations portal for HPE Aruba Networking Central. It puts a live dashboard, an interactive 3D network topology, a 3D switch visualization, an AI assistant, and a device-down alerting/automation engine on top of your Central environment — all in a fast, dark-themed web UI you run yourself with Docker Compose. Every integration degrades gracefully: missing credentials disable a feature (or fall back to mock data) instead of breaking the app, so you can explore the full UI before wiring up a single API token.

## Features

- **Dashboard** — fleet-wide stats with SVG donut and mix charts (online/offline, device types, wired/wireless clients), a merged recent-events feed from your busiest devices, and an HTMX partial that auto-refreshes the live section every 30 seconds while the tab is visible.
- **Devices** — searchable inventory with bulk group and site assignment (Classic Central), plus a drill-down detail page featuring an interactive **3D switch faceplate** (Three.js + OrbitControls) that renders real port state: link, PoE, uplinks, speed, and LLDP neighbours. Run validated `show` commands, ping from the device, or reboot it — output streams into the page via HTMX.
- **Clients** — wired and wireless client list with per-client detail, including the connection path (client → AP → uplink switch) resolved live from switch port data.
- **3D Topology** — force-directed 3D graph of your network (3d-force-graph/WebGL) built from live LLDP neighbour data. Status/type/site filters, shift+click focus mode to isolate a node's neighbourhood, link colors by wired speed tier, and one-click PNG export.
- **Sites** — site grid with device and client counts pulled from Central.
- **Notifications & Automation** — a background automation engine (APScheduler) with:
  - Device-down alert rules: per-site and per-device-type filters, configurable offline threshold and cooldown, evaluated every 60 s (tunable).
  - In-app notification bell with unread counts and mark-as-read.
  - Email alerts and scheduled daily/weekly summary reports over SMTP (configured in the UI, test-send included).
  - License/subscription expiry checks against GreenLake (daily) and SSL certificate expiry monitoring for hosts you list.
- **AI Assistant** — a chat drawer available on every page, grounded with a just-fetched snapshot of your devices and clients, so it answers questions about *your* network. A global command palette (Ctrl+K / Cmd+K) searches devices, clients, and sites instantly.
- **Lab** — a sandbox of self-contained experiments: network chatbot with RAG (Qdrant + Ollama) and MCP tool calling, semantic doc search, MCP tool tester, self-healing simulator (dry-run), AI health report, config viewer, ping tester, alert dashboard, client fingerprints, and a GreenLake Platform explorer.
- **Platform** — optional session login (`PORTAL_PASSWORD`), `/healthz` liveness + DB check for orchestration, responsive mobile layout with slide-in sidebar, accessibility-minded markup (ARIA labels, keyboard navigation, focus management), and defensive error handling throughout: API failures log and degrade, they don't 500.

## Screenshots

Screenshots live in `docs/screenshots/`.

<!-- screenshot: dashboard -->
<!-- ![Dashboard — live stats, charts, and events feed](docs/screenshots/dashboard.png) -->

<!-- screenshot: device-detail -->
<!-- ![Device detail — 3D switch faceplate and ops panel](docs/screenshots/device-detail.png) -->

<!-- screenshot: topology -->
<!-- ![3D topology — filters, focus mode, PNG export](docs/screenshots/topology.png) -->

<!-- screenshot: notifications -->
<!-- ![Notifications — alert rules, recipients, and reports](docs/screenshots/notifications.png) -->

<!-- screenshot: assistant -->
<!-- ![AI assistant drawer and Ctrl+K command palette](docs/screenshots/assistant.png) -->

## Architecture

```
Browser ──► Caddy (:80/:443) ──► FastAPI app (:8000)
            zstd/gzip,            ├─ Jinja2 + HTMX + Alpine.js + Tailwind (server-rendered UI)
            security headers      ├─ APScheduler — device-down checks, expiry checks, summary reports
                                  ├─ vendors/aruba_central ───► New Central REST API (mock fallback)
                                  ├─ vendors/central_bridge ──► Classic Central (OAuth2 w/ auto-refresh)
                                  │                          ──► centralmcp tools + GreenLake (GLP)
                                  │                          ──► Qdrant + Ollama (RAG for the Lab)
                                  └─ PostgreSQL 16 + pgvector — settings, alert rules, notification
                                     history, device status snapshots, report schedule
```

```
New-Central-Portal/
├── app/
│   ├── main.py               # FastAPI entry point, lifespan, scheduler wiring
│   ├── config.py             # Settings (pydantic-settings) + startup validation
│   ├── db.py                 # PostgreSQL pool, schema bootstrap, queries
│   ├── notifications.py      # Alert engine, expiry checks, email + reports
│   ├── routes/               # One module per section
│   │   ├── home.py           #   dashboard (+ 30s HTMX live fragment)
│   │   ├── devices.py        #   list/detail, show/ping/reboot, group & site ops
│   │   ├── clients.py        #   list/detail with uplink resolution
│   │   ├── topology.py       #   3D graph data from LLDP neighbours
│   │   ├── sites.py
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
- Optional: Qdrant and Ollama running on the host for the Lab's RAG features.

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
| `QDRANT_URL` | Qdrant vector store URL for RAG. | `http://host.docker.internal:6333` | For Lab RAG |
| `OLLAMA_URL` | Ollama URL used for RAG embeddings. | `http://host.docker.internal:11434` | For Lab RAG |
| `LOG_LEVEL` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). | `INFO` | No |

SMTP settings (server, port, credentials, sender, recipients) are managed in the UI under **Notifications** and stored in the database, not in the environment.

## Operations notes

- **Health:** `GET /healthz` returns `{"status": "ok", "db": "ok" | "fail"}` and is used by the compose healthcheck; `GET /health` is a bare liveness probe.
- **Scheduler:** expiry checks run daily at 07:00, device-down checks every `DEVICE_CHECK_INTERVAL_SECONDS`, and the summary-report job evaluates hourly whether a daily/weekly report is due.
- **Degraded mode:** the app starts (with logged warnings) even if the database or any upstream API is unreachable.

## Extending

- **New Lab experiment:** add a route in `app/routes/lab.py`, a template under `app/templates/lab/`, and an entry in the `lab_menu()` experiments list. Experiments are self-contained, so one breaking never affects the others.
- **New vendor:** add a client module under `app/vendors/` with a singleton instance, then import it from the routes that need it.
