import logging
from html import escape

from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
import httpx
import json
from config import settings
import db
import security
from routes import assistant as assistant_routes
from routes.devices import _parse_show_commands
from templates_shared import templates

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/")
async def lab_menu(request: Request):
    """Lab home - menu of experiments."""
    experiments = [
        # Badged requires-token, not live: GITHUB_TOKEN is the literal
        # placeholder your_token_here, so this can only answer "not configured".
        {"slug": "chat", "name": "Network Chatbot",
         "desc": "Ask Claude about your network. Uses MCP + RAG. Needs a GitHub token.",
         "status": "active", "color": "green", "badge": "requires-token"},
        {"slug": "rag", "name": "Doc Search",
         "desc": "Hybrid search across network docs (LanceDB). No AI.",
         "status": "active", "color": "blue", "badge": "live"},
        {"slug": "doc-api", "name": "API Lookup",
         "desc": "Exact OpenAPI schema, field, and enum lookup.",
         "status": "new", "color": "indigo", "badge": "live"},
        {"slug": "doc-ask", "name": "Doc Q&A",
         "desc": "Compact cited answers from local docs and API indexes.",
         "status": "new", "color": "sky", "badge": "live"},
        {"slug": "mcp-tester", "name": "MCP Tool Tester",
         "desc": "Poke at MCP tools to see what they return.",
         "status": "active", "color": "purple", "badge": "live"},
        {"slug": "device-scope", "name": "Device Deep-Dive",
         "desc": "CPU, memory and throughput trends; plus temperature, PoE budget, VLANs and interface errors on switches.",
         "status": "new", "color": "blue", "badge": "live"},
        {"slug": "app-visibility", "name": "Application Visibility",
         "desc": "What is consuming the network, how Aruba's DPI rates it, and which client talked to what.",
         "status": "new", "color": "purple", "badge": "live"},
        {"slug": "compliance", "name": "Compliance Board",
         "desc": "Firmware drift, config sync state, and Central's own recommendations.",
         "status": "new", "color": "amber", "badge": "live"},
        {"slug": "health-report", "name": "Network Health Report",
         "desc": "AI-generated summary of device/alert/client health using live data.",
         "status": "new", "color": "green", "badge": "requires-token"},
        {"slug": "config", "name": "Config Viewer",
         "desc": "Run show commands on any device and inspect output.",
         "status": "new", "color": "blue", "badge": "live"},
        {"slug": "ping", "name": "Ping Tester",
         "desc": "Test reachability from any online device to any destination.",
         "status": "new", "color": "purple", "badge": "live"},
        {"slug": "alerts", "name": "Central Alerts",
         "desc": "Live alerts with severity breakdown and device/site grouping.",
         "status": "new", "color": "amber", "badge": "live"},
        {"slug": "fingerprints", "name": "Client Fingerprints",
         "desc": "Browse client devices grouped by category, vendor, and OS.",
         "status": "new", "color": "teal", "badge": "live"},
        {"slug": "greenlake", "name": "GreenLake Platform",
         "desc": "GLP inventory, subscriptions, users, and audit log from the HPE GreenLake workspace.",
         "status": "new", "color": "green", "badge": "requires-token"},
        {"slug": "assistant", "name": "AI Assistant",
         "desc": "Choose the AI backend (Claude subscription or GitHub Models), pick a model, and test it.",
         "status": "new", "color": "purple", "badge": "requires-token"},
        {"slug": "activity", "name": "Activity & History",
         "desc": "Device up/down timeline, portal audit trail, client onboarding events, and cleared-alert post-mortems.",
         "status": "new", "color": "sky", "badge": "live"},
        {"slug": "securessid", "name": "Vendor CLI Translator",
         "desc": "Side-by-side equivalent CLI commands across Aruba AOS-CX/AOS-S, Juniper, Cisco, Ruckus, and Mist.",
         "status": "new", "color": "teal", "badge": "demo"},
        {"slug": "password", "name": "Change Password",
         "desc": "Update the portal login password (stored hashed in the database).",
         "status": "new", "color": "amber", "badge": "live"},
    ]
    return templates.TemplateResponse(
        request,
        "lab/menu.html",
        {"experiments": experiments, "active": "lab"},
    )


async def _assistant_ctx(request: Request, **extra) -> dict:
    # Both resolvers do a blocking psycopg2 read; one threadpool hop for the pair.
    backend, model = await run_in_threadpool(
        lambda: (assistant_routes._resolve_backend(), assistant_routes._resolve_model())
    )
    ctx = {
        "active": "lab",
        "backend": backend,
        "model": model,
        "backends": assistant_routes.VALID_BACKENDS,
        "labels": assistant_routes.BACKEND_LABELS,
    }
    ctx.update(extra)
    return ctx


@router.get("/assistant")
async def assistant_settings(request: Request):
    """AI assistant backend configuration (UI for ASSISTANT_BACKEND)."""
    return templates.TemplateResponse(
        request,
        "lab/assistant.html",
        await _assistant_ctx(request, saved=request.query_params.get("saved") == "1"),
    )


@router.post("/assistant")
async def assistant_settings_save(
    request: Request,
    backend: str = Form(...),
    model: str = Form(""),
):
    backend = (backend or "").strip().lower()
    if backend not in assistant_routes.VALID_BACKENDS:
        backend = "claude_cli"
    model = (model or "").strip()
    if model.startswith("-"):  # never let a model value be parsed as a CLI flag
        model = ""
    def _save():
        db.set_setting("assistant_backend", backend)
        db.set_setting("assistant_model", model)

    try:
        await run_in_threadpool(_save)
    except Exception as exc:
        logger.error("Failed to save assistant settings: %s", exc)
        return templates.TemplateResponse(
            request,
            "lab/assistant.html",
            await _assistant_ctx(
                request, backend=backend, model=model,
                error="Could not save — the database is unavailable.",
            ),
        )
    return RedirectResponse(url="/lab/assistant?saved=1", status_code=303)


@router.post("/assistant/test")
async def assistant_settings_test(request: Request):
    """HTMX: run a quick prompt through the currently-configured backend."""
    reply = await assistant_routes.generate_reply(
        "Reply in one short sentence confirming you are online and what you "
        "help with.",
        [],
    )
    return HTMLResponse(
        '<div class="text-sm text-slate-200 whitespace-pre-wrap leading-relaxed">'
        f"{escape(reply)}</div>"
    )


@router.get("/securessid")
async def securessid_page(request: Request):
    """Open the vendored SecureSSID CLI-translator tool (served from /static)."""
    return RedirectResponse("/static/securessid/index.html")


@router.get("/password")
async def password_page(request: Request):
    """Change the portal login password."""
    return templates.TemplateResponse(request, "lab/password.html", {"active": "lab"})


@router.post("/password")
async def password_change(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    def render(error=None, saved=False):
        return templates.TemplateResponse(
            request, "lab/password.html",
            {"active": "lab", "error": error, "saved": saved},
        )

    if not security.verify_password(current_password):
        return render(error="Current password is incorrect.")
    if len(new_password) < 8:
        return render(error="New password must be at least 8 characters.")
    if new_password != confirm_password:
        return render(error="New password and confirmation do not match.")
    try:
        security.set_portal_password(new_password)
    except Exception as exc:
        logger.error("Password change failed: %s", exc)
        return render(error="Could not save — the database is unavailable.")
    logger.info("Portal password changed via UI")
    return render(saved=True)


@router.get("/chat")
async def chat_page(request: Request):
    """Network chatbot experiment - ask Claude about your network."""
    from config import is_placeholder
    github_token_set = not is_placeholder(settings.github_token)
    return templates.TemplateResponse(
        request,
        "lab/chat.html",
        {"active": "lab", "github_token_set": github_token_set},
    )


@router.post("/chat")
async def chat_submit(request: Request, message: str = Form(...)):
    """Handle chat messages with RAG context + MCP tool calling."""
    from config import is_placeholder
    if is_placeholder(settings.github_token):
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
    logger.info("[RAG] starting search for: %s", message[:50])
    try:
        docs = await search_docs(message, top_k=8)
        logger.info("[RAG] returned %s docs, first: %s", len(docs) if docs else 0, docs[0] if docs else 'none')
        if docs and "error" not in docs[0]:
            # Keep results scoring within half of the best hit. The absolute
            # scale differs per RAG backend (LanceDB RRF fusion ≈ 0.01–0.03,
            # Redis cosine+boost ≈ 0–1), so a fixed cutoff like the old 0.60
            # silently discards everything under LanceDB.
            top_score = max((d.get("score", 0) for d in docs), default=0)
            good_docs = [d for d in docs if d.get("score", 0) >= top_score * 0.5] if top_score > 0 else []
            logger.info("[RAG] good_docs count: %s", len(good_docs))
            if good_docs:
                snippets = []
                for d in good_docs:
                    src = d.get("file_path", d.get("source", "doc"))
                    snippets.append(f"[{src}]: {d.get('text', '')[:500]}")
                rag_context = "\n\n".join(snippets)
                tools_used.append({"name": "search_docs", "summary": f"{len(good_docs)} doc snippets retrieved"})
    except Exception as exc:
        logger.error("[RAG] ERROR: %s", exc)

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
                    try:
                        tool_result = await run_tool(fn_name, fn_args)
                    except Exception as exc:
                        logger.exception("[chat] tool %s failed: %s", fn_name, exc)
                        tool_result = {"status": "error", "error": str(exc), "output": ""}

                    # Truncate large outputs to stay within token limits
                    output_str = json.dumps(tool_result.get("output", ""), default=str)
                    if len(output_str) > 8000:
                        output_str = output_str[:8000] + "... (truncated)"

                    tools_used.append({
                        "name": fn_name,
                        "summary": f"{'✅' if tool_result.get('status') == 'success' else '❌'} {tool_result.get('error') or 'OK'}",
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
        logger.exception("[chat] LLM call failed: %s", e)
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
    """Search Aruba docs via centralmcp RAG (LanceDB by default)."""
    from vendors.central_bridge import search_docs

    import re
    error = None
    try:
        raw = await search_docs(query, top_k=8)
    except Exception as exc:
        logger.exception("[RAG] search failed for %r: %s", query, exc)
        raw = []
        error = "Doc search is unavailable right now (centralmcp RAG index may be missing). Please try again later."

    if raw and "error" in raw[0]:
        logger.error("[RAG] search returned error: %s", raw[0]["error"])
        results = []
        error = raw[0]["error"]
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
        {"query": query, "results": results, "error": error},
    )


@router.get("/doc-api")
async def doc_api_page(request: Request):
    return templates.TemplateResponse(request, "lab/doc-api.html", {"active": "lab"})


@router.post("/doc-api")
async def doc_api_search(request: Request, query: str = Form(...)):
    from vendors.central_bridge import lookup_api

    error = None
    try:
        raw = await lookup_api(query, top_k=10)
    except Exception as exc:
        logger.exception("[API lookup] failed for %r: %s", query, exc)
        raw = []
        error = "API lookup is unavailable (centralmcp specs index may be missing)."

    if raw and isinstance(raw[0], dict) and "error" in raw[0]:
        error = raw[0]["error"]
        results = []
    else:
        results = []
        for r in raw:
            if not isinstance(r, dict):
                continue
            text = str(r.get("text") or r.get("content") or r.get("summary") or "")
            results.append({
                "title": r.get("path") or r.get("operation_id") or r.get("method") or "API match",
                "excerpt": text[:240] + ("..." if len(text) > 240 else ""),
                "detail": text[:1200],
                "score": r.get("score", 0),
                "source": r.get("source") or "openapi_specs",
                "file_path": r.get("file_path") or r.get("path") or "",
            })

    return templates.TemplateResponse(
        request,
        "lab/partials/rag_results.html",
        {"query": query, "results": results, "error": error},
    )


@router.get("/doc-ask")
async def doc_ask_page(request: Request):
    return templates.TemplateResponse(request, "lab/doc-ask.html", {"active": "lab"})


@router.post("/doc-ask")
async def doc_ask_submit(request: Request, question: str = Form(...)):
    from vendors.central_bridge import ask_docs

    error = None
    try:
        payload = await ask_docs(question, top_k=3)
    except Exception as exc:
        logger.exception("[Doc Q&A] failed for %r: %s", question, exc)
        payload = {"answer": "", "citations": [], "mode": "error"}
        error = "Doc Q&A is unavailable right now."

    answer = str(payload.get("answer") or "").strip()
    if not error and not answer:
        error = "No answer found in local documentation indexes."

    return templates.TemplateResponse(
        request,
        "lab/partials/doc_ask_result.html",
        {
            "question": question,
            "answer": answer,
            "citations": payload.get("citations") or [],
            "mode": payload.get("mode") or "",
            "error": error,
        },
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
    try:
        result = await run_tool(tool, params)
    except Exception as exc:
        logger.exception("[mcp-tester] tool %s failed: %s", tool, exc)
        result = {
            "tool": tool,
            "params": {},
            "output": None,
            "status": "error",
            "error": f"Tool execution failed: {exc}",
        }
    return templates.TemplateResponse(
        request,
        "lab/partials/mcp_result.html",
        {"result": result},
    )


@router.get("/health-report")
async def health_report_page(request: Request):
    return templates.TemplateResponse(request, "lab/health-report.html", {"active": "lab"})


@router.post("/health-report")
async def health_report_generate(request: Request):
    import asyncio, json
    from vendors.central_bridge import get_devices, get_clients, get_alerts, get_device_events
    from vendors.aruba_central import _norm_device, _norm_client

    try:
        raw_devices, raw_clients, alerts = await asyncio.gather(
            get_devices(limit=100), get_clients(limit=200), get_alerts(limit=50)
        )
    except Exception as exc:
        logger.exception("[health-report] failed to gather live data: %s", exc)
        return HTMLResponse(
            '<div class="empty-state">'
            '<p style="color:#f87171;font-weight:600;">Live network data is unavailable right now.</p>'
            '<p style="font-size:.78rem;">Aruba Central could not be reached. Check connectivity and try again.</p>'
            '</div>'
        )
    devices = [_norm_device(d) for d in raw_devices]
    clients = [_norm_client(c) for c in raw_clients]

    offline = [d for d in devices if d["status"] == "offline"]
    critical_alerts = [a for a in alerts if (a.get("severity") or "").lower() == "critical"]

    # Gather recent events for offline devices (up to 3)
    event_summaries = []
    for d in offline[:3]:
        try:
            evs = await get_device_events(d["serial"], hours=48, limit=5)
        except Exception as exc:
            logger.warning("[health-report] events lookup failed for %s: %s", d["serial"], exc)
            continue
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
                # Escape first so device/alert names echoed by the LLM cannot
                # inject HTML; our own markdown→HTML tags are added afterward.
                md = escape(md)
                # Basic markdown → HTML
                md = re.sub(r'^### (.+)$', r'<h3 style="font-size:.9rem;font-weight:700;color:#f97316;margin:18px 0 8px;">\1</h3>', md, flags=re.M)
                md = re.sub(r'^## (.+)$', r'<h2 style="font-size:1rem;font-weight:700;color:#f1f5f9;margin:20px 0 8px;">\1</h2>', md, flags=re.M)
                md = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', md)
                md = re.sub(r'^- (.+)$', r'<li style="margin:4px 0;color:#94a3b8;">\1</li>', md, flags=re.M)
                md = md.replace('\n', '<br>')
                report_html = md
        except Exception as e:
            logger.exception("[health-report] Anthropic call failed: %s", e)
            report_html = f"<p class='text-red-400'>Report generation failed: {e}</p>"

    return HTMLResponse(f'<div style="font-size:.85rem;line-height:1.7;color:#cbd5e1;">{report_html}</div>')


# ── Config Viewer ─────────────────────────────────────────────────────────────

# ── Device Deep-Dive ──────────────────────────────────────────────────────────
#
# Loading is tiered by cost, because these calls are not equal:
#   Tier 0 (first paint)  cached trend + detail reads, gathered — this is the
#                         page's reason for existing, so deferring it to HTMX
#                         would buy an extra round trip for nothing.
#   Tier 1 (hx on load)   secondary switch panels, so a 404 on VLANs cannot
#                         blank the CPU chart.
#   Tier 2 (click only)   ops show-commands. These poll an async job and
#                         centralmcp sleeps 5s BEFORE its first poll, so each
#                         costs 5-60s while holding a thread-pool worker and an
#                         upstream semaphore slot. Auto-firing them would do
#                         that on every page view, forever, uncached.

TREND_WINDOWS = ((6, "6 hours"), (12, "12 hours"), (24, "24 hours"))


def _device_scope_error(message: str) -> HTMLResponse:
    return HTMLResponse(
        f'<div class="card" style="border-color:rgba(239,68,68,.35);">'
        f'<p class="text-sm text-red-400">{escape(message)}</p></div>'
    )


@router.get("/device-scope")
async def device_scope(request: Request, serial: str = "", hours: int = 6):
    """Trends for any device, plus the physical layer for switches."""
    import asyncio

    from svg_chart import DEFAULT_GEOM, build_chart, build_meter
    from timeseries import (
        merge, normalize_device_trends, switch_snapshot, to_bits_per_second, window,
    )
    from vendors.aruba_central import _norm_device
    from vendors.central_bridge import (
        get_ap_trends, get_devices, get_switch_details, get_switch_hardware_trends,
    )

    hours = hours if hours in {h for h, _ in TREND_WINDOWS} else 6
    load_error = None
    devices: list[dict] = []
    try:
        devices = [_norm_device(d) for d in await get_devices(limit=200)]
    except Exception as exc:
        logger.exception("[device-scope] device list failed: %s", exc)
        load_error = "Could not load the device list — Aruba Central appears to be unavailable."

    devices.sort(key=lambda d: (d.get("type") or "", (d.get("name") or "").lower()))
    device = next((d for d in devices if d.get("serial") == serial), None)

    ctx: dict = {
        "active": "lab", "devices": devices, "device": device, "serial": serial,
        "hours": hours, "windows": TREND_WINDOWS, "load_error": load_error,
        "charts": [], "snapshot": None, "poe_meter": None, "trend_error": None,
    }
    if device is None:
        return templates.TemplateResponse(request, "lab/device-scope.html", ctx)

    start_iso, end_iso = window(hours)
    is_switch = device.get("type") == "switch"
    ctx["is_switch"] = is_switch

    if is_switch:
        # ONE call carries cpu, memory, temperature, PoE and power: centralmcp
        # maps the cpu, memory and hardware metrics to the same endpoint.
        raw_trends, raw_details = await asyncio.gather(
            get_switch_hardware_trends(serial, start_iso, end_iso),
            get_switch_details(serial),
            return_exceptions=True,
        )
        trends = normalize_device_trends(
            None if isinstance(raw_trends, BaseException) else raw_trends,
            serial=serial, kind="switch")
        details = None if isinstance(raw_details, BaseException) else raw_details
        ctx["snapshot"] = switch_snapshot(details)
        ctx["details"] = details if isinstance(details, dict) else {}
        snap = ctx["snapshot"]
        ctx["poe_meter"] = build_meter(snap.get("poe_consumption"), snap.get("poe_available"))
    else:
        cpu, memory, throughput = await asyncio.gather(
            get_ap_trends(serial, "cpu", start_iso, end_iso),
            get_ap_trends(serial, "memory", start_iso, end_iso),
            get_ap_trends(serial, "throughput", start_iso, end_iso),
            return_exceptions=True,
        )
        trends = merge(*[
            normalize_device_trends(None if isinstance(r, BaseException) else r,
                                    serial=serial, kind="ap")
            for r in (cpu, memory, throughput)
        ])

    if not trends.ok:
        ctx["trend_error"] = trends.error or "No trend data available for this device."
        return templates.TemplateResponse(request, "lab/device-scope.html", ctx)

    charts = []
    # CPU and memory share a 0-100% axis. Temperature and power get their own
    # charts rather than riding along: mixing % with degrees or watts on one
    # axis is a dual-axis chart in disguise, and it only looks harmless here
    # because this switch happens to sit at 21% / 25.5C.
    if trends.has("cpu") or trends.has("memory"):
        charts.append(build_chart(trends.pick("cpu", "memory"),
                                  title="CPU and memory", y_min=0, y_max=100))
    if trends.has("temperature"):
        charts.append(build_chart(trends.pick("temperature"), title="Temperature",
                                  baseline_zero=False))
    if trends.has("poe_consumption") or trends.has("poe_available"):
        charts.append(build_chart(trends.pick("poe_consumption", "poe_available"),
                                  title="PoE draw against budget", y_min=0))
    if trends.has("power") or trends.has("power_total"):
        charts.append(build_chart(trends.pick("power", "power_total"),
                                  title="Power draw", y_min=0))
    if trends.has("tx") or trends.has("rx"):
        # Bytes-per-bucket is meaningless on an axis without the bucket width.
        rate = [to_bits_per_second(s, trends.bucket_seconds)
                for s in trends.pick("tx", "rx")]
        charts.append(build_chart(rate, title="Throughput", y_min=0))

    ctx["charts"] = charts
    ctx["geom"] = DEFAULT_GEOM
    return templates.TemplateResponse(request, "lab/device-scope.html", ctx)


@router.get("/device-scope/{serial}/poe")
async def device_scope_poe(request: Request, serial: str):
    """Tier 1 fragment: per-port PoE draw."""
    from vendors.central_bridge import get_switch_interface_poe
    try:
        ports = await get_switch_interface_poe(serial)
    except Exception as exc:
        logger.exception("[device-scope] PoE fetch failed for %s: %s", serial, exc)
        return _device_scope_error("Could not read per-port PoE from this switch.")
    if ports is None:
        return _device_scope_error("This switch did not return PoE data.")
    return templates.TemplateResponse(
        request, "lab/partials/poe_table.html", {"ports": ports})


@router.get("/device-scope/{serial}/vlans")
async def device_scope_vlans(request: Request, serial: str):
    """Tier 1 fragment: VLAN membership."""
    from vendors.central_bridge import get_switch_vlans
    try:
        vlans = await get_switch_vlans(serial)
    except Exception as exc:
        logger.exception("[device-scope] VLAN fetch failed for %s: %s", serial, exc)
        return _device_scope_error("Could not read VLANs from this switch.")
    if vlans is None:
        return _device_scope_error("This switch did not return VLAN data.")
    return templates.TemplateResponse(
        request, "lab/partials/vlan_table.html", {"vlans": vlans})


@router.get("/device-scope/{serial}/interface")
async def device_scope_interface(request: Request, serial: str, port: str = "",
                                 hours: int = 6):
    """Tier 1 fragment: per-interface error counters as real series.

    Preferred over the ops `show interface statistics` path, which this switch
    refuses outright and which would cost 5-60s besides.
    """
    from svg_chart import SMALL_GEOM, build_bars
    from timeseries import downsample, error_counter_series, normalize_interface_trends, window
    from vendors.central_bridge import get_switch_interface_trends, get_switch_ports

    hours = hours if hours in {h for h, _ in TREND_WINDOWS} else 6
    start_iso, end_iso = window(hours)
    try:
        ports = await get_switch_ports(serial)
    except Exception:
        ports = []
    names = [p.get("name") or p.get("id") for p in ports if isinstance(p, dict)]
    names = [n for n in names if n]
    chosen = port or (names[0] if names else "")
    if not chosen:
        return _device_scope_error("No interfaces reported for this switch.")

    try:
        raw = await get_switch_interface_trends(serial, start_iso, end_iso,
                                                interface_id=chosen)
    except Exception as exc:
        logger.exception("[device-scope] interface trends failed for %s: %s", serial, exc)
        return _device_scope_error("Could not read interface counters from this switch.")

    trends = normalize_interface_trends(raw, serial=serial, interface_id=chosen)
    counters = error_counter_series(trends)
    charts = [build_bars(downsample(s, 120, how="max"), geom=SMALL_GEOM,
                         title=s.label) for s in counters]
    return templates.TemplateResponse(request, "lab/partials/interface_trends.html", {
        "serial": serial, "ports": names, "chosen": chosen, "hours": hours,
        "charts": charts, "trends": trends,
        "clean_count": len([k for k in trends.series if k.endswith(
            ("errors", "discards", "fcs", "collision", "runts", "giants", "fragmented"))]),
    })


@router.post("/device-scope/{serial}/diagnostic")
async def device_scope_diagnostic(request: Request, serial: str,
                                  kind: str = Form(...)):
    """Tier 2: an explicitly-requested show command. 5-60s, uncached."""
    from ops_format import format_ops_response
    from vendors.central_bridge import get_cx_arp_table, get_switch_spanning_tree

    runners = {"stp": get_switch_spanning_tree, "arp": get_cx_arp_table}
    runner = runners.get(kind)
    if runner is None:
        return _device_scope_error("Unknown diagnostic.")
    try:
        result = await runner(serial)
    except Exception as exc:
        logger.exception("[device-scope] %s failed on %s: %s", kind, serial, exc)
        return _device_scope_error(f"The {kind.upper()} command failed on this device.")
    # Already an HTMLResponse — reuses the shared ops renderer rather than
    # hand-rolling another <pre> block.
    return format_ops_response(result)


# ── Compliance Board ─────────────────────────────────────────────────────────

@router.get("/compliance")
async def compliance_board(request: Request):
    """Firmware drift, config health, and Central's own recommendations.

    The honest replacement for the Self-Healing Sim: it says what is actually
    wrong and what Central recommends, rather than remediating invented faults.
    """
    import asyncio

    from vendors.aruba_central import _norm_device
    from vendors.central_bridge import (
        get_devices, list_devices_config_health, list_firmware_upgrades, list_insights,
    )

    load_error = None
    firmware: list = []
    health: list = []
    insights: list = []
    raw_devices: list = []
    try:
        firmware, health, insights, raw_devices = await asyncio.gather(
            list_firmware_upgrades(),
            list_devices_config_health(),
            list_insights(),
            get_devices(limit=200),
            return_exceptions=True,
        )
    except Exception as exc:
        logger.exception("[compliance] fetch failed: %s", exc)
        load_error = "Could not reach Aruba Central."

    def _ok(value) -> list:
        """asyncio.gather(return_exceptions=True) hands back the exception."""
        return value if isinstance(value, list) else []

    firmware, health, insights = _ok(firmware), _ok(health), _ok(insights)
    if not (firmware or health or insights) and load_error is None:
        load_error = "Aruba Central returned no compliance data."

    # Running version comes from the device inventory; the recommendation comes
    # from the upgrade list. Neither one alone shows drift.
    running = {}
    for raw in _ok(raw_devices):
        if not isinstance(raw, dict):
            continue
        device = _norm_device(raw)
        if device.get("serial"):
            running[device["serial"]] = raw.get("firmwareVersion") or ""

    rows = []
    for item in firmware:
        if not isinstance(item, dict):
            continue
        serial = item.get("serialNumber") or ""
        recommended = (item.get("recommendedVersion") or "").strip()
        current = (running.get(serial) or item.get("firmwareVersion") or "").strip()
        classification = item.get("firmwareClassification") or ""
        rows.append({
            "serial": serial,
            "name": item.get("deviceName") or serial,
            "current": current,
            "recommended": recommended,
            "classification": classification,
            # Compare only when both are known; a blank recommendation is not drift.
            "drift": bool(recommended and current and
                          recommended not in current and current not in recommended),
            "status": item.get("upgradeStatus") or "",
            "last": item.get("lastUpgradedTimeAt") or "",
        })
    rows.sort(key=lambda r: (not r["drift"], r["name"].lower()))

    health_rows = []
    for item in health:
        if not isinstance(item, dict):
            continue
        issues = [i for i in (item.get("activeIssues") or []) if i]
        health_rows.append({
            # The real serial is in "serial"; this payload's serialNumber is null.
            "serial": item.get("serial") or "",
            "name": item.get("name") or item.get("serial") or "",
            "type": item.get("type") or "",
            "config_status": item.get("configStatus") or "",
            "synced": (item.get("configStatus") or "").upper() == "SYNCHRONIZED",
            "issues": issues,
            "top_issue": item.get("topPriorityIssue") or "",
            "action": item.get("recommendedAction") or "",
            "group": item.get("deviceGroupName") or "",
        })
    health_rows.sort(key=lambda r: (r["synced"], r["name"].lower()))

    insight_rows = [i for i in insights if isinstance(i, dict)]

    return templates.TemplateResponse(request, "lab/compliance.html", {
        "active": "lab", "load_error": load_error,
        "firmware": rows, "health": health_rows, "insights": insight_rows,
        "drift_count": sum(1 for r in rows if r["drift"]),
        "unsynced_count": sum(1 for r in health_rows if not r["synced"]),
    })


@router.get("/compliance/{serial}/issues")
async def compliance_issues(request: Request, serial: str):
    """On-demand detail for a device flagged with active config issues."""
    from vendors.central_bridge import get_device_config_issues
    try:
        issues = await get_device_config_issues(serial)
    except Exception as exc:
        logger.exception("[compliance] issue fetch failed for %s: %s", serial, exc)
        return HTMLResponse(
            f'<p class="text-sm text-red-400">{escape("Could not read config issues.")}</p>')
    buckets = []
    for key, label in (("invalidConfig", "Invalid configuration"),
                       ("configPushFailures", "Push failures"),
                       ("configPullFailures", "Pull failures"),
                       ("filteredConfig", "Filtered configuration")):
        entries = (issues or {}).get(key) or []
        if entries:
            buckets.append({"label": label, "entries": entries})
    return templates.TemplateResponse(request, "lab/partials/config_issues.html",
                                      {"buckets": buckets, "serial": serial})


# ── Activity & History ───────────────────────────────────────────────────────

_ALERT_KIND_ICON = {"Critical": "critical", "Major": "major", "Minor": "minor"}


def _alert_texts(alert: dict, field: str) -> list[str]:
    """Pull rootCause / solution text out of Central's action[] payload.

    Each entry is a JSON-encoded string containing {text, subTextItems, and a
    navigation deep-link}. Decode defensively — a payload change should cost a
    missing explanation, not a 500.
    """
    import json as _json

    out: list[str] = []
    for action in alert.get("action") or []:
        if not isinstance(action, dict):
            continue
        for raw in action.get(field) or []:
            if isinstance(raw, dict):
                text = raw.get("text")
            else:
                try:
                    text = (_json.loads(raw) or {}).get("text")
                except Exception:
                    text = str(raw)
            if text:
                out.append(str(text).strip())
    return out


@router.get("/activity")
async def activity_page(request: Request, hours: int = 24):
    """Device transitions, portal audit trail, client onboarding, and the
    cleared alerts the main alerts page filters out."""
    import asyncio

    from alert_severity import count_severities, normalize_severity
    from vendors.aruba_central import _norm_device
    from vendors.central_bridge import (
        get_devices, list_all_alerts, list_client_onboarding_events,
    )

    hours = hours if hours in (6, 24, 72) else 24

    # The DB reads are blocking psycopg2; keep them off the event loop. Track
    # failure separately from emptiness — "no transitions recorded yet" is the
    # normal state on a stable fleet and must not look like "database down".
    def _load_db():
        return db.get_device_status_history(limit=100), db.get_audit_log(limit=100)

    db_error = None
    transitions: list = []
    audit: list = []
    try:
        transitions, audit = await run_in_threadpool(_load_db)
    except Exception as exc:
        logger.exception("[activity] database read failed: %s", exc)
        db_error = "The database is unavailable, so portal history cannot be shown."

    alerts_raw, raw_devices = await asyncio.gather(
        list_all_alerts(limit=100), get_devices(limit=200), return_exceptions=True,
    )
    alerts_raw = alerts_raw if isinstance(alerts_raw, list) else []
    devices = [_norm_device(d) for d in (raw_devices if isinstance(raw_devices, list) else [])]

    alerts = []
    for a in alerts_raw:
        if not isinstance(a, dict):
            continue
        alerts.append({
            "id": a.get("id") or a.get("key") or "",
            "name": a.get("name") or "Alert",
            "summary": a.get("summary") or "",
            "severity": normalize_severity(a.get("severity")),
            "severity_raw": a.get("severity") or "",
            "status": a.get("status") or "",
            "category": a.get("category") or "",
            "priority": a.get("priority") or "",
            "device_type": a.get("deviceType") or "",
            "site": a.get("siteName") or "",
            "time": a.get("createdAt") or a.get("updatedAt") or "",
            "root_cause": _alert_texts(a, "rootCause"),
            "solution": _alert_texts(a, "solution"),
        })
    alerts.sort(key=lambda x: x.get("time") or "", reverse=True)

    # Onboarding events come from the switches — APs return none.
    switches = [d for d in devices if d.get("type") == "switch" and d.get("serial")]
    onboarding: list = []
    for switch in switches[:3]:
        try:
            rows = await list_client_onboarding_events(switch["serial"], hours=hours)
        except Exception as exc:
            logger.debug("[activity] onboarding fetch failed for %s: %s",
                         switch["serial"], exc)
            continue
        for row in rows:
            if isinstance(row, dict):
                row = dict(row)
                row["_device"] = switch.get("name") or switch["serial"]
                onboarding.append(row)
    onboarding.sort(key=lambda r: r.get("timeAt") or "", reverse=True)

    # Which clients are re-onboarding repeatedly? Same idea as
    # detect_client_flapping, computed from rows already fetched.
    repeat: dict[str, int] = {}
    for row in onboarding:
        mac = row.get("clientMacAddress") or ""
        if mac:
            repeat[mac] = repeat.get(mac, 0) + 1
    flapping = sorted(((m, c) for m, c in repeat.items() if c >= 5),
                      key=lambda pair: pair[1], reverse=True)

    return templates.TemplateResponse(request, "lab/activity.html", {
        "active": "lab", "hours": hours, "db_error": db_error,
        "transitions": transitions, "audit": audit,
        "alerts": alerts, "alert_summary": count_severities(alerts),
        "onboarding": onboarding[:60], "flapping": flapping,
        "switch_count": len(switches),
    })


# ── Application Visibility ───────────────────────────────────────────────────
#
# Windows the selector offers, in hours. The endpoint rejects any span wider
# than 7 days with a 400, so 168 is the ceiling and is verified to be accepted
# at exactly 168.
_APP_WINDOWS = ((1, "1h"), (6, "6h"), (24, "24h"), (72, "3d"), (168, "7d"))

# How many "flagged but recognised" rows to show before truncating. The
# unclassified group is never capped — it is short by nature and is the whole
# reason the watchlist is split in two.
_APP_WATCH_CAP = 25

# The byte figures come from DPI attribution, not from an interface counter,
# and on this tenant they do not survive scrutiny: one application is credited
# with ~8.6 GB received in a single hour, and the same row is byte-identical at
# 6h, 24h and 72h. The windowing is genuine (widen the window and totals only
# grow) so the RANKING is usable, but an absolute "you transferred N GB"
# headline would be a fabrication. Every byte figure on this page is therefore
# framed as relative, and this caveat renders on the page itself rather than
# living only in this comment.
_APP_BYTES_CAVEAT = (
    "Byte totals are deep-packet-inspection estimates, not interface counters. "
    "Spot-checks on this tenant found single applications credited with more "
    "traffic than the link plausibly carried, so read these as a ranking rather "
    "than as a measurement."
)


def _app_share_meter(value: int, largest: int) -> dict:
    """A share-of-largest bar for one table row.

    warn_at/crit_at are pushed above 1.0 deliberately. build_meter's defaults
    turn a bar red past 90% of its total, which is the right read for a PoE
    budget and the wrong one here — it would paint the single biggest talker
    red on every page load, implying a threshold that does not exist.
    """
    from svg_chart import build_meter
    return build_meter(float(value), float(largest or 1), unit="bytes",
                       warn_at=2.0, crit_at=3.0)


async def _app_site(site_param: str) -> tuple[list[dict], dict | None]:
    """(all sites, the selected one). site_id is a required argument upstream."""
    from vendors.central_bridge import get_sites
    from vendors.aruba_central import site_display_name, site_id_of

    raw = await get_sites(limit=100)
    sites = [{"id": site_id_of(s), "name": site_display_name(s) or site_id_of(s)}
             for s in (raw or []) if isinstance(s, dict) and site_id_of(s)]
    if not sites:
        return [], None
    chosen = next((s for s in sites if s["id"] == site_param), sites[0])
    return sites, chosen


@router.get("/app-visibility")
async def app_visibility(request: Request, hours: int = 24, site: str = ""):
    """What is talking on this network, and does Aruba's DPI trust it?

    Nothing else in the portal attributes a byte to an application or surfaces
    the risk classification — /lab/device-scope stops at per-device throughput.
    """
    import asyncio

    import app_risk
    from timeseries import window
    from vendors.central_bridge import list_applications, get_clients
    from vendors.aruba_central import _norm_client

    valid_hours = [h for h, _ in _APP_WINDOWS]
    hours = hours if hours in valid_hours else 24

    load_error = None
    apps: list[dict] = []
    clients: list[dict] = []
    sites: list[dict] = []
    chosen: dict | None = None

    try:
        sites, chosen = await _app_site(site)
    except Exception as exc:
        logger.exception("[app-visibility] site lookup failed: %s", exc)
        load_error = "Could not reach Aruba Central to list sites."

    if chosen is None and load_error is None:
        load_error = "Central returned no sites, and this view is scoped to one."

    if chosen is not None:
        start_iso, end_iso = window(hours=hours)
        raw_apps, raw_clients = await asyncio.gather(
            list_applications(chosen["id"], start_iso, end_iso),
            get_clients(limit=200),
            return_exceptions=True,
        )
        if isinstance(raw_apps, BaseException) or raw_apps is None:
            if isinstance(raw_apps, BaseException):
                logger.exception("[app-visibility] fetch failed: %s", raw_apps)
            # None is the wrapper's explicit "the fetch failed" — distinct from
            # an empty list, which means the window really was quiet.
            load_error = "Aruba Central did not return application data for this window."
        else:
            apps = app_risk.normalize_apps(raw_apps)
        if isinstance(raw_clients, list):
            # client_id is keyed on MAC upstream, so a client without one
            # cannot be looked up and does not belong in the picker.
            clients = sorted(
                (c for c in (_norm_client(c) for c in raw_clients
                             if isinstance(c, dict)) if c.get("mac")),
                key=lambda c: (c.get("hostname") or c.get("mac") or "").lower(),
            )

    unknown, known = app_risk.watchlist(apps)
    # Over a 7-day window "flagged but recognised" is ~135 rows, which is a wall
    # of table rather than a watchlist. Cap it — and render the count that was
    # cut, because a silently truncated list reads as a complete one.
    known_total, known = len(known), known[:_APP_WATCH_CAP]
    talkers = app_risk.top_talkers(apps, limit=25)
    categories = app_risk.category_rollup(apps, limit=12)
    largest = talkers[0]["total"] if talkers else 0
    largest_category = categories[0]["total"] if categories else 0

    return templates.TemplateResponse(request, "lab/app-visibility.html", {
        "active": "lab",
        "load_error": load_error,
        "hours": hours,
        "windows": _APP_WINDOWS,
        "sites": sites,
        "site_id": chosen["id"] if chosen else "",
        "site_name": chosen["name"] if chosen else "",
        "apps": apps,
        "risk_strip": app_risk.risk_strip(apps),
        "watch_unknown": unknown,
        "watch_known": known,
        "watch_known_total": known_total,
        "talkers": [(a, _app_share_meter(a["total"], largest)) for a in talkers],
        "categories": [(c, _app_share_meter(c["total"], largest_category))
                       for c in categories],
        "clients": clients,
        "bytes_caveat": _APP_BYTES_CAVEAT,
    })


@router.get("/app-visibility/client")
async def app_visibility_client(request: Request, mac: str = "", hours: int = 24,
                                site: str = ""):
    """Applications seen for one client. Deferred — one upstream call on click.

    Only this direction is offered. The API has no app-to-client index, so
    answering "who talked to that domain" would mean looping every client and
    filtering, which is one call per client.
    """
    import app_risk
    from timeseries import window
    from vendors.central_bridge import list_applications

    hours = hours if hours in [h for h, _ in _APP_WINDOWS] else 24
    mac = (mac or "").strip()
    if not mac:
        return HTMLResponse(
            '<div class="empty-state"><p>Pick a client to see what it talked to.</p></div>')

    error = None
    apps: list[dict] = []
    try:
        _, chosen = await _app_site(site)
        if chosen is None:
            error = "No site to scope the lookup to."
        else:
            start_iso, end_iso = window(hours=hours)
            raw = await list_applications(chosen["id"], start_iso, end_iso, client_id=mac)
            if raw is None:
                error = "Central did not return application data for this client."
            else:
                apps = app_risk.normalize_apps(raw)
    except Exception as exc:
        logger.exception("[app-visibility] client lookup failed for %s: %s", mac, exc)
        error = "The client lookup failed."

    largest = apps[0]["total"] if apps else 0
    return templates.TemplateResponse(request, "lab/partials/client_apps.html", {
        "mac": mac, "hours": hours, "error": error,
        "rows": [(a, _app_share_meter(a["total"], largest)) for a in apps[:25]],
    })


@router.get("/config")
async def config_page(request: Request):
    from vendors.central_bridge import get_devices
    from vendors.aruba_central import _norm_device
    load_error = None
    try:
        raw = await get_devices(limit=100)
    except Exception as exc:
        logger.exception("[config] device list failed: %s", exc)
        raw = []
        load_error = "Could not load the device list — Aruba Central appears to be unavailable."
    devices = [_norm_device(d) for d in raw]
    switches = [d for d in devices if d["type"] == "switch"]
    return templates.TemplateResponse(request, "lab/config.html", {"devices": devices, "switches": switches, "load_error": load_error, "active": "lab"})


@router.post("/config")
async def config_fetch(request: Request, serial: str = Form(...), command: str = Form("show running-config")):
    import html as html_mod
    from vendors.central_bridge import get_devices, run_show
    from vendors.aruba_central import _norm_device
    try:
        raw = await get_devices(limit=100)
    except Exception as exc:
        logger.exception("[config] device lookup failed: %s", exc)
        return HTMLResponse("<p class='text-red-400'>Aruba Central is unavailable — could not verify the device. Please try again later.</p>")
    devices = {_norm_device(d)["serial"]: _norm_device(d) for d in raw}
    device = devices.get(serial)
    if not device:
        return HTMLResponse("<p class='text-red-400'>Device not found.</p>")

    # This ran whatever was typed, split on ';', straight at the switch — while
    # /devices/{serial}/show, the same capability reached from the Devices page,
    # validated it. Reuse that validator rather than write a second one: it caps
    # the count and length, allows only a conservative character set (no quotes,
    # pipes, backticks, redirects or newlines) and requires each command to
    # start with "show".
    cmds, err = _parse_show_commands(command)
    if err:
        return HTMLResponse(f"<p class='text-red-400'>{escape(err)}</p>")
    try:
        result = await run_show(serial, device["type"], cmds)
        outputs = result.get("output", {}).get("results", [])
        html_parts = []
        for item in outputs:
            html_parts.append(
                f'<p style="font-size:.65rem;color:#f97316;margin-bottom:4px;font-weight:700;">{html_mod.escape(item.get("command", ""))}</p>'
                f'<pre style="font-size:.75rem;color:#94a3b8;white-space:pre-wrap;word-break:break-all;margin-bottom:16px;">{html_mod.escape(item.get("output", ""))}</pre>'
            )
        return HTMLResponse("".join(html_parts) or "<p class='text-gray-500'>No output.</p>")
    except Exception as e:
        logger.exception("[config] run_show failed on %s: %s", serial, e)
        return HTMLResponse(f"<p class='text-red-400'>Command failed: {html_mod.escape(str(e))}</p>")


# ── Ping Tester ───────────────────────────────────────────────────────────────

@router.get("/ping")
async def ping_page(request: Request):
    from vendors.central_bridge import get_devices
    from vendors.aruba_central import _norm_device
    load_error = None
    try:
        raw = await get_devices(limit=100)
    except Exception as exc:
        logger.exception("[ping] device list failed: %s", exc)
        raw = []
        load_error = "Could not load the device list — Aruba Central appears to be unavailable."
    devices = [_norm_device(d) for d in raw if _norm_device(d)["status"] == "online"]
    return templates.TemplateResponse(request, "lab/ping.html", {"devices": devices, "load_error": load_error, "active": "lab"})


@router.post("/ping")
async def ping_run(request: Request, serial: str = Form(...), destination: str = Form(...)):
    import html as html_mod
    from vendors.central_bridge import get_devices, run_ping
    from vendors.aruba_central import _norm_device
    try:
        raw = await get_devices(limit=100)
    except Exception as exc:
        logger.exception("[ping] device lookup failed: %s", exc)
        return HTMLResponse("<p class='text-red-400'>Aruba Central is unavailable — could not verify the device. Please try again later.</p>")
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
            f'<p style="color:{color};font-weight:700;margin-bottom:8px;">Status: {html_mod.escape(str(status))}</p>'
            f'<pre style="color:#94a3b8;white-space:pre-wrap;">{html_mod.escape(text)}</pre>'
            f'</div>'
        )
    except Exception as e:
        logger.exception("[ping] run_ping failed on %s -> %s: %s", serial, destination, e)
        return HTMLResponse(f"<p class='text-red-400'>Ping failed: {html_mod.escape(str(e))}</p>")


# ── Alert Dashboard ───────────────────────────────────────────────────────────

@router.get("/alerts")
async def alerts_page(request: Request):
    from vendors.central_bridge import get_alerts
    from collections import Counter
    load_error = None
    try:
        alerts = await get_alerts(limit=100)
    except Exception as exc:
        logger.exception("[alerts] alert fetch failed: %s", exc)
        alerts = []
        load_error = "Could not load alerts — Aruba Central appears to be unavailable."
    severity_counts = Counter((a.get("severity") or "Unknown") for a in alerts)
    device_counts = Counter((a.get("deviceType") or "Unknown") for a in alerts)
    return templates.TemplateResponse(request, "lab/alerts.html", {
        "alerts": alerts,
        "severity_counts": dict(severity_counts),
        "device_counts": dict(device_counts),
        "load_error": load_error,
        "active": "lab",
    })


# ── Client Fingerprint Explorer ───────────────────────────────────────────────

@router.get("/fingerprints")
async def fingerprints_page(request: Request):
    from vendors.central_bridge import get_clients
    from collections import defaultdict, Counter
    load_error = None
    try:
        raw = await get_clients(limit=500)
    except Exception as exc:
        logger.exception("[fingerprints] client fetch failed: %s", exc)
        raw = []
        load_error = "Could not load clients — Aruba Central appears to be unavailable."
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
        "load_error": load_error,
        "active": "lab",
    })


# ── GreenLake Platform ────────────────────────────────────────────────────────

@router.get("/greenlake")
async def greenlake_page(request: Request):
    import asyncio
    from vendors.central_bridge import get_glp_devices, get_glp_subscriptions, get_glp_users, get_glp_audit_logs

    async def _fetch_service_offers():
        try:
            from vendors.central_bridge import list_glp_service_offers
            return await list_glp_service_offers(limit=100)
        except Exception:
            return []

    devices, subscriptions, users, audit_logs, service_offers = await asyncio.gather(
        get_glp_devices(limit=200),
        get_glp_subscriptions(limit=200),
        get_glp_users(limit=300),
        get_glp_audit_logs(limit=50),
        _fetch_service_offers(),
        return_exceptions=True,
    )
    if isinstance(devices, Exception):
        logger.error("[greenlake] device fetch failed: %s", devices)
        devices = []
    if isinstance(subscriptions, Exception):
        logger.error("[greenlake] subscription fetch failed: %s", subscriptions)
        subscriptions = []
    if isinstance(users, Exception):
        logger.error("[greenlake] user fetch failed: %s", users)
        users = []
    if isinstance(audit_logs, Exception):
        logger.error("[greenlake] audit log fetch failed: %s", audit_logs)
        audit_logs = []
    if isinstance(service_offers, Exception):
        logger.error("[greenlake] service offers fetch failed: %s", service_offers)
        service_offers = []

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
        "service_offers": service_offers if isinstance(service_offers, list) else [],
        "active": "greenlake",
    })


async def _json_body(request: Request) -> tuple[dict | None, JSONResponse | None]:
    """Parse a JSON request body, or return a 400 instead of raising.

    An unparseable or non-object body used to propagate straight out of the
    handler as an unhandled 500 — a malformed request reported as a server
    fault, and a stack trace in the log for something the caller got wrong.
    """
    try:
        body = await request.json()
    except Exception:
        return None, JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
    if not isinstance(body, dict):
        return None, JSONResponse({"error": "Request body must be a JSON object."}, status_code=400)
    return body, None


@router.post("/greenlake/assign-subscription")
async def assign_subscription(request: Request):
    body, err = await _json_body(request)
    if err:
        return err
    serial = body.get("serial_number", "").strip()
    sub_id = body.get("subscription_id", "").strip()
    if not serial or not sub_id:
        return JSONResponse({"ok": False, "error": "serial_number and subscription_id are required"}, status_code=400)
    try:
        from vendors.central_bridge import assign_glp_subscription
        result = await assign_glp_subscription(serial, sub_id)
        return JSONResponse({"ok": True, "result": result})
    except Exception as e:
        logger.exception("[greenlake] assign subscription failed for %s: %s", serial, e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/greenlake/unassign-subscription")
async def unassign_subscription(request: Request):
    body, err = await _json_body(request)
    if err:
        return err
    serial = body.get("serial_number", "").strip()
    if not serial:
        return JSONResponse({"ok": False, "error": "serial_number is required"}, status_code=400)
    try:
        from vendors.central_bridge import unassign_glp_subscription
        result = await unassign_glp_subscription(serial)
        return JSONResponse({"ok": True, "result": result})
    except Exception as e:
        logger.exception("[greenlake] unassign subscription failed for %s: %s", serial, e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/greenlake/add-device")
async def add_device(request: Request):
    body, err = await _json_body(request)
    if err:
        return err
    serial = body.get("serial_number", "").strip()
    mac = body.get("mac_address", "").strip()
    if not serial or not mac:
        return JSONResponse({"ok": False, "error": "serial_number and mac_address are required"}, status_code=400)
    try:
        from vendors.central_bridge import add_glp_device
        result = await add_glp_device(serial, mac)
        return JSONResponse({"ok": True, "result": result})
    except Exception as e:
        logger.exception("[greenlake] add device failed for %s: %s", serial, e)
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
        logger.exception("[greenlake] bulk add failed (%s devices): %s", len(devices), e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/greenlake/assign-to-central")
async def assign_to_central(request: Request):
    body, err = await _json_body(request)
    if err:
        return err
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
            logger.exception("[greenlake] assign-to-central failed for %s: %s", s, e)
            results.append({"serial": s, "ok": False, "error": str(e)})
    all_ok = all(r["ok"] for r in results)
    return JSONResponse({"ok": all_ok, "results": results})

