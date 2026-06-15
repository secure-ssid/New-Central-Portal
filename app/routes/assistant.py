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
import os
import shutil

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config import settings
import db

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
ASSISTANT_OFF_REPLY = (
    "The AI assistant is turned off. An administrator can enable it under "
    "Lab → AI Assistant."
)

# Available LLM backends, configurable per-deployment (DB setting overrides env).
#   claude_cli — local Claude Code CLI using the operator's subscription
#   github     — GitHub Models (OpenAI-compatible), needs GITHUB_TOKEN
#   off        — assistant disabled
VALID_BACKENDS = ("claude_cli", "github", "off")
BACKEND_LABELS = {
    "claude_cli": "Claude CLI (subscription)",
    "github": "GitHub Models (gpt-4o)",
    "off": "Disabled",
}


def _resolve_backend() -> str:
    """Effective backend: DB setting → ASSISTANT_BACKEND env → 'github'."""
    try:
        v = (db.get_setting("assistant_backend") or "").strip().lower()
        if v in VALID_BACKENDS:
            return v
    except Exception:
        pass
    v = (os.environ.get("ASSISTANT_BACKEND") or "github").strip().lower()
    return v if v in VALID_BACKENDS else "github"


def _resolve_model() -> str:
    """Effective model override: DB setting → ASSISTANT_CLAUDE_MODEL env → ''."""
    try:
        v = (db.get_setting("assistant_model") or "").strip()
        if v:
            return v
    except Exception:
        pass
    return os.environ.get("ASSISTANT_CLAUDE_MODEL", "").strip()


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


# ── Claude Code CLI backend (uses the operator's Claude subscription) ─────────
# Enabled with ASSISTANT_BACKEND=claude_cli. Shells out to the `claude` binary
# in headless (-p) mode with our own system prompt and no tools/MCP, so it
# behaves as a plain chat completion. Auth comes from
# CLAUDE_CONFIG_DIR/.credentials.json or CLAUDE_CODE_OAUTH_TOKEN in the env.
CLAUDE_TIMEOUT_SECONDS = 120
_EMPTY_MCP = '{"mcpServers":{}}'

# Warn loudly at import if the CLI backend is selected but the binary is absent,
# instead of deferring the failure to the first user request.
if (os.environ.get("ASSISTANT_BACKEND") or "").strip().lower() == "claude_cli":
    _cli = os.environ.get("CLAUDE_BIN", "claude")
    if not (os.path.isfile(_cli) or shutil.which(_cli)):
        logger.warning(
            "[assistant] ASSISTANT_BACKEND=claude_cli but CLAUDE_BIN is not "
            "available in this container: %s", _cli,
        )


def _claude_prompt(messages: list[dict]) -> tuple[str, str]:
    """Split chat messages into (system_prompt, conversation_text)."""
    system = ""
    convo: list[str] = []
    for m in messages:
        content = (m.get("content") or "").strip()
        if not content:
            continue
        role = m.get("role")
        # Collapse whitespace (incl. newlines) on user/assistant turns so a
        # message can't inject fake "User:"/"Assistant:" turns into the prompt.
        if role == "system":
            system = content
        elif role == "assistant":
            convo.append(f"Assistant: {' '.join(content.split())}")
        else:
            convo.append(f"User: {' '.join(content.split())}")
    return system, "\n\n".join(convo)


async def _claude_cli_complete(messages: list[dict], model: str = "") -> str:
    """Generate a reply via the local Claude Code CLI. Raises on failure."""
    system, prompt = _claude_prompt(messages)
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    args = [
        claude_bin, "-p", prompt,
        "--output-format", "text",
        "--allowed-tools", "",
        "--strict-mcp-config", "--mcp-config", _EMPTY_MCP,
    ]
    if system:
        args += ["--system-prompt", system]
    if model and not model.startswith("-"):
        args += ["--model", model]

    env = os.environ.copy()
    # Use the Claude subscription (OAuth creds in CLAUDE_CONFIG_DIR), not an API
    # key: a set ANTHROPIC_API_KEY would override OAuth and fail with "Invalid
    # API key". A present-but-empty OAuth token also breaks auth, so drop it
    # unless a real one was provided.
    env.pop("ANTHROPIC_API_KEY", None)
    if not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=CLAUDE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("claude CLI timed out")
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip()[:500]
        raise RuntimeError(f"claude CLI exited {proc.returncode}: {detail}")
    return stdout.decode(errors="replace").strip()


async def generate_reply(message: str, history: list[dict]) -> str:
    """Produce an assistant reply using the configured backend.

    Always returns a user-safe string (never raises) so callers can render it
    directly. The backend is resolved from the DB setting (UI-configurable),
    falling back to the ASSISTANT_BACKEND env var.
    """
    backend = _resolve_backend()
    if backend == "off":
        return ASSISTANT_OFF_REPLY

    live_context = await _build_live_context()
    messages = (
        [{"role": "system", "content": _build_system_prompt(live_context)}]
        + history
        + [{"role": "user", "content": message}]
    )

    # Backend: local Claude Code CLI (operator's subscription).
    if backend == "claude_cli":
        try:
            reply = await _claude_cli_complete(messages, _resolve_model())
        except Exception as exc:
            logger.exception("[assistant] claude CLI call failed: %s", exc)
            return UNAVAILABLE_REPLY
        return reply.strip() or UNAVAILABLE_REPLY

    # Backend: GitHub Models (OpenAI-compatible chat completions).
    if backend != "github":
        logger.warning("[assistant] unknown backend %r — refusing to answer", backend)
        return UNAVAILABLE_REPLY
    if not settings.github_token or settings.github_token == "your_github_pat_here":
        logger.warning("[assistant] no GitHub token configured — assistant disabled")
        return UNAVAILABLE_REPLY

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                LLM_URL,
                headers={
                    "Authorization": f"Bearer {settings.github_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": _resolve_model() or LLM_MODEL,
                    "max_tokens": LLM_MAX_TOKENS,
                    "messages": messages,
                },
            )
            r.raise_for_status()
            reply = r.json()["choices"][0]["message"]["content"] or ""
    except Exception as exc:
        logger.exception("[assistant] LLM call failed: %s", exc)
        return UNAVAILABLE_REPLY

    return reply.strip() or UNAVAILABLE_REPLY


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
    reply = await generate_reply(message, history)
    return JSONResponse({"reply": reply})
