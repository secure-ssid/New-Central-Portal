from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import httpx
import json
from config import settings

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/")
async def lab_menu(request: Request):
    """Lab home - menu of experiments."""
    experiments = [
        {"slug": "chat", "name": "Network Chatbot",
         "desc": "Ask Claude about your network. Uses MCP + RAG.",
         "status": "active", "color": "green"},
        {"slug": "rag", "name": "Doc Search",
         "desc": "Semantic search across network docs. No AI.",
         "status": "active", "color": "blue"},
        {"slug": "mcp-tester", "name": "MCP Tool Tester",
         "desc": "Poke at MCP tools to see what they return.",
         "status": "active", "color": "purple"},
        {"slug": "self-heal", "name": "Self-Healing Sim",
         "desc": "Auto-remediation in dry-run mode.",
         "status": "active", "color": "amber"},
        {"slug": "juniper", "name": "Juniper Corner",
         "desc": "Notes and experiments as I learn Junos.",
         "status": "active", "color": "teal"},
        {"slug": "health-report", "name": "Network Health Report",
         "desc": "AI-generated summary of device/alert/client health using live data.",
         "status": "new", "color": "green"},
        {"slug": "config", "name": "Config Viewer",
         "desc": "Run show commands on any device and inspect output.",
         "status": "new", "color": "blue"},
        {"slug": "ping", "name": "Ping Tester",
         "desc": "Test reachability from any online device to any destination.",
         "status": "new", "color": "purple"},
        {"slug": "alerts", "name": "Alert Dashboard",
         "desc": "Live alerts with severity breakdown and device/site grouping.",
         "status": "new", "color": "amber"},
        {"slug": "fingerprints", "name": "Client Fingerprints",
         "desc": "Browse client devices grouped by category, vendor, and OS.",
         "status": "new", "color": "teal"},
        {"slug": "greenlake", "name": "GreenLake Platform",
         "desc": "GLP inventory, subscriptions, users, and audit log from the HPE GreenLake workspace.",
         "status": "new", "color": "green"},
    ]
    return templates.TemplateResponse(
        request,
        "lab/menu.html",
        {"experiments": experiments, "active": "lab"},
    )


@router.get("/chat")
async def chat_page(request: Request):
    """Network chatbot experiment - ask Claude about your network."""
    github_token_set = bool(
        settings.github_token and settings.github_token != "your_github_pat_here"
    )
    return templates.TemplateResponse(
        request,
        "lab/chat.html",
        {"active": "lab", "github_token_set": github_token_set},
    )


@router.post("/chat")
async def chat_submit(request: Request, message: str = Form(...)):
    """Handle chat messages with RAG context + MCP tool calling."""
    if not settings.github_token or settings.github_token == "your_github_pat_here":
        return templates.TemplateResponse(
            request,
            "lab/partials/chat_message.html",
            {"message": message, "response": "⚠️ No GitHub token configured. Set GITHUB_TOKEN in your .env file.",
             "tools_used": []},
        )

    from vendors.central_bridge import search_docs, run_tool

    tools_used = []

    # ── 1. RAG: pull relevant docs for context ─────────────────────────
    rag_context = ""
    print(f"[RAG] starting search for: {message[:50]}", flush=True)
    try:
        docs = search_docs(message, top_k=8)
        print(f"[RAG] returned {len(docs) if docs else 0} docs, first: {docs[0] if docs else 'none'}", flush=True)
        if docs and "error" not in docs[0]:
            # Filter to only reasonably relevant results (score > 0.60)
            good_docs = [d for d in docs if d.get("score", 0) > 0.60]
            print(f"[RAG] good_docs count: {len(good_docs)}", flush=True)
            if good_docs:
                snippets = []
                for d in good_docs:
                    src = d.get("file_path", d.get("source", "doc"))
                    snippets.append(f"[{src}]: {d.get('text', '')[:500]}")
                rag_context = "\n\n".join(snippets)
                tools_used.append({"name": "search_docs", "summary": f"{len(good_docs)} doc snippets retrieved"})
    except Exception as exc:
        print(f"[RAG] ERROR: {exc}", flush=True)

    # ── 2. Build system prompt with RAG context ────────────────────────
    system_parts = [
        "You are a network operations assistant for HPE Aruba Networking Central (the NEW cloud-native Central platform, NOT Classic/Legacy Central).",
        "This environment runs New Central with AOS-10 access points and Aruba CX switches.",
        "",
        "KEY NEW CENTRAL CONCEPTS (these differ from Classic Central):",
        "- Configuration uses a PROFILE-BASED model. You create configuration profiles (WLAN, VLAN, Routing, Security, etc.) and assign them to sites or device groups.",
        "- WLAN SSIDs are configured via WLAN SSID profiles at the /wlan-ssids/{ssid} API path or via the Central UI under Configuration > Profiles.",
        "- Forward modes: FORWARD_MODE_L2 (bridge — traffic bridged locally at AP) or tunnel mode (traffic tunneled to gateway).",
        "- Security opmodes include WPA2_PERSONAL, WPA3_PERSONAL, WPA2_ENTERPRISE (802.1X), WPA3_ENTERPRISE, OPEN, OWE, etc.",
        "- For 802.1X enterprise auth: configure dot1x under the WLAN security settings, add RADIUS auth servers, and optionally RADIUS accounting.",
        "- Profiles are deployed to SITES. Devices inherit configuration from their site assignment.",
        "- Device groups must support Central (New Central) configuration — not Classic groups.",
        "- There is NO 'Devices > Access Points > Networks > + Add SSID' path. That is Classic Central. Do NOT reference Classic Central navigation.",
        "",
        "You have access to live network tools you can call. Use them when the user asks about specific devices, clients, sites, alerts, or wants to run operational commands.",
        "When documentation is provided below, use it to ground your answers with accurate details. You may also use your general knowledge of Aruba networking, but always frame advice in the context of New Central.",
        "If you're unsure about a specific New Central UI path, say so rather than guessing a Classic Central path.",
        "Be concise and helpful. Format data clearly.",
    ]
    if rag_context:
        system_parts.append(f"\n--- Relevant documentation ---\n{rag_context}\n--- End documentation ---")
    else:
        system_parts.append("\nNo relevant documentation was found for this query. You may use your tools to look up live data, or let the user know you don't have specific docs for their question.")

    # ── 3. Define MCP tools as OpenAI function-calling schema ──────────
    functions = [
        {"type": "function", "function": {"name": "list_sites", "description": "List all sites in Aruba Central", "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "Max results", "default": 100}}, "required": []}}},
        {"type": "function", "function": {"name": "list_devices", "description": "List APs, switches, and gateways", "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "Max results", "default": 50}}, "required": []}}},
        {"type": "function", "function": {"name": "list_clients", "description": "List connected clients", "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "Max results", "default": 50}}, "required": []}}},
        {"type": "function", "function": {"name": "find_device", "description": "Look up a device by serial number", "parameters": {"type": "object", "properties": {"serial_number": {"type": "string", "description": "Device serial number"}}, "required": ["serial_number"]}}},
        {"type": "function", "function": {"name": "find_client", "description": "Look up a client by MAC address or IP", "parameters": {"type": "object", "properties": {"mac_or_ip": {"type": "string", "description": "Client MAC or IP address"}}, "required": ["mac_or_ip"]}}},
        {"type": "function", "function": {"name": "list_alerts", "description": "List active alerts", "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "description": "Max results", "default": 20}}, "required": []}}},
        {"type": "function", "function": {"name": "list_events", "description": "Get events for a device over last N hours", "parameters": {"type": "object", "properties": {"serial_number": {"type": "string", "description": "Device serial"}, "hours": {"type": "integer", "description": "Lookback hours", "default": 24}}, "required": ["serial_number"]}}},
        {"type": "function", "function": {"name": "cx_ping", "description": "Run ping from a CX switch to a destination", "parameters": {"type": "object", "properties": {"serial_number": {"type": "string", "description": "CX switch serial"}, "destination": {"type": "string", "description": "IP or hostname to ping"}}, "required": ["serial_number", "destination"]}}},
        {"type": "function", "function": {"name": "cx_traceroute", "description": "Run traceroute from a CX switch", "parameters": {"type": "object", "properties": {"serial_number": {"type": "string", "description": "CX switch serial"}, "destination": {"type": "string", "description": "IP or hostname to trace"}}, "required": ["serial_number", "destination"]}}},
        {"type": "function", "function": {"name": "search_docs", "description": "Search Aruba documentation", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Search query"}, "top_k": {"type": "integer", "description": "Number of results", "default": 5}}, "required": ["query"]}}},
    ]

    # ── 4. First LLM call (may request tool calls) ────────────────────
    try:
        messages = [
            {"role": "system", "content": "\n".join(system_parts)},
            {"role": "user", "content": message},
        ]

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                "https://models.inference.ai.azure.com/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.github_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o",
                    "max_tokens": 1024,
                    "messages": messages,
                    "tools": functions,
                    "tool_choice": "auto",
                },
            )
            r.raise_for_status()
            result = r.json()
            choice = result["choices"][0]

            # ── 5. If model wants tool calls, execute them ─────────────
            if choice.get("finish_reason") == "tool_calls" or choice["message"].get("tool_calls"):
                tool_calls = choice["message"]["tool_calls"]
                messages.append(choice["message"])  # assistant message with tool_calls

                for tc in tool_calls:
                    fn_name = tc["function"]["name"]
                    fn_args = tc["function"]["arguments"]
                    tool_result = await run_tool(fn_name, fn_args)

                    # Truncate large outputs to stay within token limits
                    output_str = json.dumps(tool_result.get("output", ""), default=str)
                    if len(output_str) > 8000:
                        output_str = output_str[:8000] + "... (truncated)"

                    tools_used.append({
                        "name": fn_name,
                        "summary": f"{'✅' if tool_result['status'] == 'success' else '❌'} {tool_result.get('error') or 'OK'}",
                    })

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": output_str,
                    })

                # ── 6. Second LLM call with tool results ──────────────
                r2 = await client.post(
                    "https://models.inference.ai.azure.com/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.github_token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "gpt-4o",
                        "max_tokens": 1024,
                        "messages": messages,
                    },
                )
                r2.raise_for_status()
                result2 = r2.json()
                response = result2["choices"][0]["message"]["content"]
            else:
                response = choice["message"]["content"]

    except Exception as e:
        response = f"❌ Error: {str(e)}"

    return templates.TemplateResponse(
        request,
        "lab/partials/chat_message.html",
        {"message": message, "response": response, "tools_used": tools_used},
    )


@router.get("/rag")
async def rag_page(request: Request):
    """RAG-powered doc search experiment."""
    return templates.TemplateResponse(
        request,
        "lab/rag.html",
        {"active": "lab"},
    )


@router.post("/rag")
async def rag_search(request: Request, query: str = Form(...)):
    """Search Aruba docs via Qdrant + Ollama (centralmcp RAG pipeline)."""
    from vendors.central_bridge import search_docs
    raw = search_docs(query, top_k=8)

    import re
    if raw and "error" in raw[0]:
        results = [{"title": "Error", "excerpt": raw[0]["error"], "detail": raw[0]["error"], "score": 0}]
    else:
        results = []
        for r in raw:
            text = r.get("text", "")
            # Strip HTML comments, tags, and excessive whitespace
            clean = re.sub(r'<!--.*?-->', '', text)
            clean = re.sub(r'<[^>]+>', '', clean)
            clean = re.sub(r'#{1,6}\s*', '', clean)  # strip markdown headers
            clean = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', clean)  # [text](url) -> text
            clean = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', clean)  # bold/italic
            clean = re.sub(r'-{3,}', '', clean)  # horizontal rules
            clean = re.sub(r'\n{2,}', '\n', clean).strip()
            clean = re.sub(r'  +', ' ', clean)
            excerpt = clean[:200] + ('...' if len(clean) > 200 else '')
            detail = clean[:800] + ('...' if len(clean) > 800 else '')
            results.append({
                "title": r.get("file_path", "").split("/")[-1] or r.get("source", "Result"),
                "excerpt": excerpt,
                "detail": detail,
                "score": round(r.get("score", 0), 3),
                "source": r.get("source", ""),
                "file_path": r.get("file_path", ""),
            })

    return templates.TemplateResponse(
        request,
        "lab/partials/rag_results.html",
        {"query": query, "results": results},
    )


@router.get("/mcp-tester")
async def mcp_tester_page(request: Request):
    """MCP tool tester - execute real centralmcp tools."""
    from vendors.central_bridge import TOOL_REGISTRY
    return templates.TemplateResponse(
        request,
        "lab/mcp-tester.html",
        {"tool_registry": TOOL_REGISTRY, "active": "lab"},
    )


@router.post("/mcp-tester")
async def mcp_tool_run(request: Request, tool: str = Form(...), params: str = Form("")):
    """Execute a real centralmcp tool and return the result."""
    from vendors.central_bridge import run_tool
    result = await run_tool(tool, params)
    return templates.TemplateResponse(
        request,
        "lab/partials/mcp_result.html",
        {"result": result},
    )


@router.get("/self-heal")
async def self_heal_page(request: Request):
    """Self-healing simulation - auto-remediation in dry-run mode."""
    issues = [
        {
            "id": 1,
            "device": "BY-AP763",
            "issue": "High CPU usage (85%)",
            "remediation": "Restart AP services",
            "status": "detected",
        },
        {
            "id": 2,
            "device": "CX6300-CORE",
            "issue": "Port 1/1/12 flapping",
            "remediation": "Disable/re-enable port",
            "status": "detected",
        },
    ]
    return templates.TemplateResponse(
        request,
        "lab/self-heal.html",
        {"issues": issues, "active": "lab"},
    )


@router.post("/self-heal/{issue_id}/remediate")
async def self_heal_remediate(request: Request, issue_id: int):
    """Simulate auto-remediation action."""
    return HTMLResponse(
        f"""
        <div class="px-4 py-2 bg-green-500/20 text-green-400 border border-green-500/30 rounded-lg text-sm">
            ✓ Remediation #{issue_id} completed (dry-run mode - no actual changes made)
        </div>
        """
    )


@router.get("/juniper")
async def juniper_page(request: Request):
    """Juniper learning corner - notes and experiments."""
    notes = [
        {
            "title": "Junos CLI Basics",
            "content": "Configuration mode: `configure`, commit changes with `commit`, rollback with `rollback`",
            "date": "2026-04-15",
        },
        {
            "title": "Static Routes",
            "content": "set routing-options static route 0.0.0.0/0 next-hop 10.0.0.1",
            "date": "2026-04-10",
        },
        {
            "title": "Interface Configuration",
            "content": "set interfaces ge-0/0/0 unit 0 family inet address 10.0.0.1/24",
            "date": "2026-04-05",
        },
    ]
    return templates.TemplateResponse(
        request,
        "lab/juniper.html",
        {"notes": notes, "active": "lab"},
    )


# ── Health Report ─────────────────────────────────────────────────────────────

@router.get("/health-report")
async def health_report_page(request: Request):
    return templates.TemplateResponse(request, "lab/health-report.html", {"active": "lab"})


@router.post("/health-report")
async def health_report_generate(request: Request):
    import asyncio, json
    from vendors.central_bridge import get_devices, get_clients, get_alerts, get_device_events
    from vendors.aruba_central import _norm_device, _norm_client

    raw_devices, raw_clients, alerts = await asyncio.gather(
        get_devices(limit=100), get_clients(limit=200), get_alerts(limit=50)
    )
    devices = [_norm_device(d) for d in raw_devices]
    clients = [_norm_client(c) for c in raw_clients]

    offline = [d for d in devices if d["status"] == "offline"]
    critical_alerts = [a for a in alerts if (a.get("severity") or "").lower() == "critical"]

    # Gather recent events for offline devices (up to 3)
    event_summaries = []
    for d in offline[:3]:
        evs = await get_device_events(d["serial"], hours=48, limit=5)
        for e in evs[:2]:
            event_summaries.append(f"{d['name']}: {e.get('eventName','')} — {e.get('description','')[:80]}")

    # Client category breakdown
    from collections import Counter
    cat_counts = Counter(c.get("category") or "Unknown" for c in clients)

    prompt = f"""You are a network operations expert. Generate a concise health report for this network.

DEVICES ({len(devices)} total):
- Online: {sum(1 for d in devices if d['status']=='online')}
- Offline: {len(offline)} — {', '.join(d['name'] for d in offline) or 'none'}
- Switches: {sum(1 for d in devices if d['type']=='switch')}
- Access Points: {sum(1 for d in devices if d['type']=='access_point')}
- Gateways: {sum(1 for d in devices if d['type']=='gateway')}

ALERTS ({len(alerts)} total, {len(critical_alerts)} critical):
{chr(10).join(f"- [{a.get('severity')}] {a.get('name')}: {a.get('summary','')[:100]}" for a in alerts[:8]) or 'No alerts'}

RECENT EVENTS (offline devices):
{chr(10).join(event_summaries) or 'None'}

CLIENTS ({len(clients)} connected):
{chr(10).join(f"- {cat}: {cnt}" for cat, cnt in cat_counts.most_common(6))}

Write a structured health report with sections: Overall Status, Issues Requiring Attention, Client Activity, and Recommendations. Be specific and actionable. Use markdown formatting."""

    report_html = "<p class='text-red-400'>No Anthropic API key configured.</p>"
    if settings.anthropic_api_key and settings.anthropic_api_key != "your_key_here":
        try:
            async with httpx.AsyncClient(timeout=60) as client_http:
                r = await client_http.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": settings.anthropic_api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": "claude-3-5-sonnet-20241022", "max_tokens": 2048, "messages": [{"role": "user", "content": prompt}]},
                )
                r.raise_for_status()
                import re
                md = r.json()["content"][0]["text"]
                # Basic markdown → HTML
                md = re.sub(r'^### (.+)$', r'<h3 style="font-size:.9rem;font-weight:700;color:#f97316;margin:18px 0 8px;">\1</h3>', md, flags=re.M)
                md = re.sub(r'^## (.+)$', r'<h2 style="font-size:1rem;font-weight:700;color:#f1f5f9;margin:20px 0 8px;">\1</h2>', md, flags=re.M)
                md = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', md)
                md = re.sub(r'^- (.+)$', r'<li style="margin:4px 0;color:#94a3b8;">\1</li>', md, flags=re.M)
                md = md.replace('\n', '<br>')
                report_html = md
        except Exception as e:
            report_html = f"<p class='text-red-400'>Error: {e}</p>"

    return HTMLResponse(f'<div style="font-size:.85rem;line-height:1.7;color:#cbd5e1;">{report_html}</div>')


# ── Config Viewer ─────────────────────────────────────────────────────────────

@router.get("/config")
async def config_page(request: Request):
    from vendors.central_bridge import get_devices
    from vendors.aruba_central import _norm_device
    raw = await get_devices(limit=100)
    devices = [_norm_device(d) for d in raw]
    switches = [d for d in devices if d["type"] == "switch"]
    return templates.TemplateResponse(request, "lab/config.html", {"devices": devices, "switches": switches, "active": "lab"})


@router.post("/config")
async def config_fetch(request: Request, serial: str = Form(...), command: str = Form("show running-config")):
    from vendors.central_bridge import get_devices, run_show
    from vendors.aruba_central import _norm_device
    raw = await get_devices(limit=100)
    devices = {_norm_device(d)["serial"]: _norm_device(d) for d in raw}
    device = devices.get(serial)
    if not device:
        return HTMLResponse("<p class='text-red-400'>Device not found.</p>")
    cmds = [c.strip() for c in command.split(";") if c.strip()]
    try:
        result = await run_show(serial, device["type"], cmds)
        outputs = result.get("output", {}).get("results", [])
        html_parts = []
        for item in outputs:
            html_parts.append(
                f'<p style="font-size:.65rem;color:#f97316;margin-bottom:4px;font-weight:700;">{item["command"]}</p>'
                f'<pre style="font-size:.75rem;color:#94a3b8;white-space:pre-wrap;word-break:break-all;margin-bottom:16px;">{item.get("output","")}</pre>'
            )
        return HTMLResponse("".join(html_parts) or "<p class='text-gray-500'>No output.</p>")
    except Exception as e:
        return HTMLResponse(f"<p class='text-red-400'>Error: {e}</p>")


# ── Ping Tester ───────────────────────────────────────────────────────────────

@router.get("/ping")
async def ping_page(request: Request):
    from vendors.central_bridge import get_devices
    from vendors.aruba_central import _norm_device
    raw = await get_devices(limit=100)
    devices = [_norm_device(d) for d in raw if _norm_device(d)["status"] == "online"]
    return templates.TemplateResponse(request, "lab/ping.html", {"devices": devices, "active": "lab"})


@router.post("/ping")
async def ping_run(request: Request, serial: str = Form(...), destination: str = Form(...)):
    from vendors.central_bridge import get_devices, run_ping
    from vendors.aruba_central import _norm_device
    raw = await get_devices(limit=100)
    devices = {_norm_device(d)["serial"]: _norm_device(d) for d in raw}
    device = devices.get(serial)
    if not device:
        return HTMLResponse("<p class='text-red-400'>Device not found.</p>")
    try:
        result = await run_ping(serial, device["type"], destination, count=5)
        status = result.get("status", "")
        outputs = result.get("output", {}).get("results", [])
        text = outputs[0].get("output", "") if outputs else str(result)
        color = "#4ade80" if status == "COMPLETED" and "success" in text.lower() else "#f87171"
        return HTMLResponse(
            f'<div style="font-size:.75rem;">'
            f'<p style="color:{color};font-weight:700;margin-bottom:8px;">Status: {status}</p>'
            f'<pre style="color:#94a3b8;white-space:pre-wrap;">{text}</pre>'
            f'</div>'
        )
    except Exception as e:
        return HTMLResponse(f"<p class='text-red-400'>Error: {e}</p>")


# ── Alert Dashboard ───────────────────────────────────────────────────────────

@router.get("/alerts")
async def alerts_page(request: Request):
    from vendors.central_bridge import get_alerts
    from collections import Counter
    alerts = await get_alerts(limit=100)
    severity_counts = Counter(a.get("severity", "Unknown") for a in alerts)
    device_counts = Counter(a.get("deviceType", "Unknown") for a in alerts)
    return templates.TemplateResponse(request, "lab/alerts.html", {
        "alerts": alerts,
        "severity_counts": dict(severity_counts),
        "device_counts": dict(device_counts),
        "active": "lab",
    })


# ── Client Fingerprint Explorer ───────────────────────────────────────────────

@router.get("/fingerprints")
async def fingerprints_page(request: Request):
    from vendors.central_bridge import get_clients
    from collections import defaultdict, Counter
    raw = await get_clients(limit=500)
    # Group by category → vendor → OS
    by_category: dict = defaultdict(lambda: defaultdict(list))
    for c in raw:
        cat = c.get("clientCategory") or "Unclassified"
        vendor = c.get("clientVendor") or "Unknown"
        os_ = c.get("clientOperatingSystem") or "Unknown"
        by_category[cat][vendor].append(os_)
    # Flatten to sortable list
    rows = []
    for cat, vendors in sorted(by_category.items()):
        for vendor, oses in sorted(vendors.items()):
            os_counts = Counter(oses)
            rows.append({
                "category": cat,
                "vendor": vendor,
                "count": len(oses),
                "os_breakdown": ", ".join(f"{o} ({n})" for o, n in os_counts.most_common(3)),
            })
    rows.sort(key=lambda r: -r["count"])
    return templates.TemplateResponse(request, "lab/fingerprints.html", {
        "rows": rows,
        "total": len(raw),
        "active": "lab",
    })


# ── GreenLake Platform ────────────────────────────────────────────────────────

@router.get("/greenlake")
async def greenlake_page(request: Request):
    import asyncio
    from vendors.central_bridge import get_glp_devices, get_glp_subscriptions, get_glp_users, get_glp_audit_logs

    devices, subscriptions, users, audit_logs = await asyncio.gather(
        get_glp_devices(limit=200),
        get_glp_subscriptions(limit=200),
        get_glp_users(limit=300),
        get_glp_audit_logs(limit=50),
        return_exceptions=True,
    )
    if isinstance(devices, Exception): devices = []
    if isinstance(subscriptions, Exception): subscriptions = []
    if isinstance(users, Exception): users = []
    if isinstance(audit_logs, Exception): audit_logs = []

    # Flatten nested subscription list into device-level fields for the template.
    # GLP device.subscription is a list of {id, key, startTime, endTime, tier, ...}
    for dev in devices:
        subs_list = dev.get("subscription") or []
        first_sub = subs_list[0] if isinstance(subs_list, list) and subs_list else {}
        dev["_sub_key"] = first_sub.get("key", "")
        dev["_sub_tier"] = first_sub.get("tier", "")
        dev["_sub_start"] = first_sub.get("startTime", "")
        dev["_sub_end"] = first_sub.get("endTime", "")
        dev["_status"] = dev.get("assignedState", "")

    return templates.TemplateResponse(request, "lab/greenlake.html", {
        "devices": devices,
        "subscriptions": subscriptions,
        "users": users,
        "audit_logs": audit_logs,
        "active": "greenlake",
    })


@router.post("/greenlake/assign-subscription")
async def assign_subscription(request: Request):
    body = await request.json()
    serial = body.get("serial_number", "").strip()
    sub_id = body.get("subscription_id", "").strip()
    if not serial or not sub_id:
        return JSONResponse({"ok": False, "error": "serial_number and subscription_id are required"}, status_code=400)
    try:
        from vendors.central_bridge import assign_glp_subscription
        result = await assign_glp_subscription(serial, sub_id)
        return JSONResponse({"ok": True, "result": result})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/greenlake/unassign-subscription")
async def unassign_subscription(request: Request):
    body = await request.json()
    serial = body.get("serial_number", "").strip()
    if not serial:
        return JSONResponse({"ok": False, "error": "serial_number is required"}, status_code=400)
    try:
        from vendors.central_bridge import unassign_glp_subscription
        result = await unassign_glp_subscription(serial)
        return JSONResponse({"ok": True, "result": result})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/greenlake/add-device")
async def add_device(request: Request):
    body = await request.json()
    serial = body.get("serial_number", "").strip()
    mac = body.get("mac_address", "").strip()
    if not serial or not mac:
        return JSONResponse({"ok": False, "error": "serial_number and mac_address are required"}, status_code=400)
    try:
        from vendors.central_bridge import add_glp_device
        result = await add_glp_device(serial, mac)
        return JSONResponse({"ok": True, "result": result})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/greenlake/add-devices-csv")
async def add_devices_csv(request: Request, file: UploadFile = File(...)):
    import csv, io
    if not file.filename or not file.filename.lower().endswith(".csv"):
        return JSONResponse({"ok": False, "error": "Please upload a .csv file"}, status_code=400)
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return JSONResponse({"ok": False, "error": "File must be UTF-8 encoded"}, status_code=400)
    reader = csv.DictReader(io.StringIO(text))
    devices = []
    errors = []
    for i, row in enumerate(reader, start=2):
        serial = (row.get("serial_number") or row.get("serialNumber") or row.get("Serial Number") or row.get("Serial") or "").strip()
        mac = (row.get("mac_address") or row.get("macAddress") or row.get("MAC Address") or row.get("MAC") or row.get("Mac") or "").strip()
        if not serial or not mac:
            errors.append(f"Row {i}: missing serial or mac")
            continue
        devices.append({"serialNumber": serial, "macAddress": mac})
    if not devices:
        return JSONResponse({"ok": False, "error": "No valid devices found. " + "; ".join(errors[:5])}, status_code=400)
    try:
        from vendors.central_bridge import add_glp_devices_bulk
        result = await add_glp_devices_bulk(devices)
        return JSONResponse({"ok": True, "result": result, "parsed": len(devices), "errors": errors[:10]})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/greenlake/assign-to-central")
async def assign_to_central(request: Request):
    body = await request.json()
    serials = body.get("serial_numbers", [])
    if not serials:
        serial = body.get("serial_number", "").strip()
        if serial:
            serials = [serial]
    if not serials:
        return JSONResponse({"ok": False, "error": "serial_number(s) required"}, status_code=400)
    results = []
    for s in serials:
        try:
            from vendors.central_bridge import assign_glp_device_to_app
            r = await assign_glp_device_to_app(s.strip())
            results.append({"serial": s, "ok": True, "result": r})
        except Exception as e:
            results.append({"serial": s, "ok": False, "error": str(e)})
    all_ok = all(r["ok"] for r in results)
    return JSONResponse({"ok": all_ok, "results": results})

