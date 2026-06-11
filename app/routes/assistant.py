"""AI assistant drawer backend.

POST /assistant/chat  (JSON {message, history: [{role, content}]})  →  {reply}

Uses the same LLM mechanism as the Lab chatbot (lab.py): GitHub Models'
OpenAI-compatible chat-completions endpoint, authenticated with
settings.github_token. Live network context (device summary + client counts)
is injected into the system prompt via central_bridge, with failures tolerated
so the assistant still answers when Central is unreachable. If the LLM backend
itself is unreachable, returns a friendly reply with HTTP 200 so the UI can
render it as a normal message.
"""
import asyncio
import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

# Same endpoint/model the Lab chatbot uses (see routes/lab.py chat_submit).
LLM_URL = "https://models.inference.ai.azure.com/chat/completions"
LLM_MODEL = "gpt-4o"
LLM_MAX_TOKENS = 1024

MAX_MESSAGE_CHARS = 4000
MAX_HISTORY_TURNS = 10
MAX_DEVICE_LINES = 50

UNAVAILABLE_REPLY = (
    "Sorry — the assistant is unavailable right now (the language-model "
    "backend could not be reached). The rest of the portal still works; "
    "please try again in a few minutes."
)


def _sanitize_history(history) -> list[dict]:
    """Keep only well-formed user/assistant turns; cap length and turn count."""
    if not isinstance(history, list):
        return []
    cleaned = []
    for turn in history:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = turn.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        content = content.strip()[:MAX_MESSAGE_CHARS]
        if content:
            cleaned.append({"role": role, "content": content})
    return cleaned[-MAX_HISTORY_TURNS:]


async def _build_live_context() -> str:
    """Compact live-network summary for the system prompt. Never raises."""
    try:
        from vendors.aruba_central import _norm_device
        from vendors.central_bridge import get_clients, get_devices

        raw_devices, raw_clients = await asyncio.gather(
            get_devices(limit=100),
            get_clients(limit=300),
            return_exceptions=True,
        )

        parts: list[str] = []

        if isinstance(raw_devices, BaseException):
            logger.warning("[assistant] device fetch failed: %s", raw_devices)
            parts.append("Device inventory: unavailable right now.")
        else:
            devices = [_norm_device(d) for d in raw_devices if isinstance(d, dict)]
            online = sum(1 for d in devices if d["status"] == "online")
            parts.append(
                f"DEVICES ({len(devices)} total, {online} online, "
                f"{len(devices) - online} offline):"
            )
            for d in devices[:MAX_DEVICE_LINES]:
                parts.append(
                    f"- {d['name'] or d['serial']} | {d['type']} | "
                    f"{d['status']} | site: {d['site'] or 'unknown'}"
                )
            if len(devices) > MAX_DEVICE_LINES:
                parts.append(f"... and {len(devices) - MAX_DEVICE_LINES} more devices")

        if isinstance(raw_clients, BaseException):
            logger.warning("[assistant] client fetch failed: %s", raw_clients)
            parts.append("Connected clients: unavailable right now.")
        else:
            clients = [c for c in raw_clients if isinstance(c, dict)]
            wired = sum(
                1 for c in clients
                if str(c.get("clientConnectionType") or c.get("type") or "").lower() == "wired"
            )
            parts.append(
                f"CLIENTS: {len(clients)} connected "
                f"({wired} wired, {len(clients) - wired} wireless)"
            )

        return "\n".join(parts)
    except Exception as exc:
        logger.warning("[assistant] live context build failed: %s", exc)
        return ""


def _build_system_prompt(live_context: str) -> str:
    parts = [
        "You are the built-in AI assistant for the New Central Portal, a web "
        "dashboard for an HPE Aruba Networking Central (New Central) environment "
        "with AOS-10 access points and Aruba CX switches.",
        "Help the user understand and operate their network: device and client "
        "status, sites, topology, alerts, and general Aruba networking questions.",
        "Portal pages you can point users to: Dashboard (/), Devices (/devices/), "
        "Clients (/clients/), Topology (/topology/), Sites (/sites/), "
        "Notifications (/notifications/), and the Lab (/lab/) for experiments "
        "like the chatbot, doc search, config viewer, and ping tester.",
        "Be concise and practical. Use short paragraphs or bullet lists. "
        "Plain text only — no markdown tables.",
        "If you are not sure about something, say so rather than guessing.",
    ]
    if live_context:
        parts.append(
            "\n--- Live network snapshot (just fetched) ---\n"
            f"{live_context}\n"
            "--- End snapshot ---"
        )
    else:
        parts.append(
            "\nLive network data could not be fetched right now, so answer from "
            "general knowledge and let the user know live data is unavailable "
            "if they ask about specific devices or clients."
        )
    return "\n".join(parts)


@router.post("/chat")
async def chat(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    message = body.get("message")
    message = (message.strip() if isinstance(message, str) else "")[:MAX_MESSAGE_CHARS]
    if not message:
        return JSONResponse({"reply": "Please enter a message and try again."})

    history = _sanitize_history(body.get("history"))

    if not settings.github_token or settings.github_token == "your_github_pat_here":
        logger.warning("[assistant] no GitHub token configured — assistant disabled")
        return JSONResponse({"reply": UNAVAILABLE_REPLY})

    live_context = await _build_live_context()
    messages = (
        [{"role": "system", "content": _build_system_prompt(live_context)}]
        + history
        + [{"role": "user", "content": message}]
    )

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                LLM_URL,
                headers={
                    "Authorization": f"Bearer {settings.github_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": LLM_MODEL,
                    "max_tokens": LLM_MAX_TOKENS,
                    "messages": messages,
                },
            )
            r.raise_for_status()
            reply = r.json()["choices"][0]["message"]["content"] or ""
    except Exception as exc:
        logger.exception("[assistant] LLM call failed: %s", exc)
        return JSONResponse({"reply": UNAVAILABLE_REPLY})

    return JSONResponse({"reply": reply.strip() or UNAVAILABLE_REPLY})
