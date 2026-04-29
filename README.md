# NetLab

Network engineer's lab + ops app. Dashboards, drill-downs, and experiments all in one.

## Stack

- **Backend**: Python + FastAPI
- **Frontend**: HTMX + Jinja2 templates + Tailwind CSS + Alpine.js
- **Database**: PostgreSQL + pgvector (for RAG)
- **Reverse proxy**: Caddy
- **Runs in**: Docker Compose

## Quick start

```bash
# 1. Copy env file and fill in your Aruba Central token
cp .env.example .env
nano .env

# 2. Build and run
docker compose up --build

# 3. Open in browser
# http://localhost        (via Caddy)
# http://localhost:8000   (direct to FastAPI)
```

If you don't have Aruba credentials yet, leave the env vars empty and the app
will serve mock data so you can see the UI.

## Project structure

```
netlab/
├── app/
│   ├── main.py              # FastAPI entry
│   ├── config.py            # env settings
│   ├── routes/              # one file per top-level section
│   │   ├── home.py
│   │   ├── devices.py
│   │   ├── clients.py
│   │   ├── sites.py
│   │   └── lab.py
│   ├── vendors/             # API clients (Aruba, Juniper later)
│   │   └── aruba_central.py
│   └── templates/           # Jinja2 HTML
│       ├── base.html        # layout + dark theme
│       ├── home.html
│       ├── devices/
│       ├── clients/
│       ├── sites/
│       └── lab/
├── docker-compose.yml
├── Caddyfile
└── .env.example
```

## Architecture

**Drill-down navigation**: start broad, click to go deeper.

- Home → high-level stats
- Devices → list → click a device → full detail page with ports, clients, config
- Clients → list → click a client → connection path, history, actions
- Sites → grid → click a site → site map + devices at that site
- Lab → menu of experiments (chatbot, RAG search, MCP tester, self-healing sim)

No more flat nav with 14 items. Each entry point has everything you need for that
thing one click away.

## Adding a new Lab experiment

1. Add a route in `routes/lab.py`
2. Add a template in `templates/lab/<your_experiment>.html`
3. Add an entry to the experiments list in `lab_menu()`

That's it. Experiments are self-contained so one being broken never affects others.

## Adding a new vendor

1. Create `vendors/<vendor>.py` with a client class
2. Create a singleton instance at the bottom
3. Import into the routes that need it

## Development

With `--reload` in the Dockerfile, code changes restart FastAPI automatically.
Template changes don't need a restart - just refresh the browser.
