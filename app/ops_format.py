"""Format centralmcp ops tool responses for HTMX HTML fragments."""
from __future__ import annotations

import html
import json
import re

from fastapi.responses import HTMLResponse

_REDACTED = "« redacted »"

# Directives whose argument is a secret in AOS-CX / AOS-S / gateway configs.
# `show running-config` returns these; the value after the keyword is redacted
# while the directive itself stays, so the config is still readable and
# auditable without disclosing the credential. `ciphertext` covers every hashed
# secret (password/key/radius/tacacs) because AOS-CX renders them all that way.
_SECRET_ARG = re.compile(
    r"(?i)\b(ciphertext|plaintext|wpa-passphrase|pre-shared-key|passphrase|"
    r"psk|community|md5|sha256|sha512)\s+(\S+)"
)


def mask_config_secrets(text: str) -> str:
    """Redact credential material in a device running-config dump.

    Conservative and line-preserving: only the token following a known secret
    keyword is replaced. Non-secret config is untouched.
    """
    if not text:
        return text
    return _SECRET_ARG.sub(lambda m: f"{m.group(1)} {_REDACTED}", text)


def format_ops_pre(text: str, *, monospace: bool = True) -> HTMLResponse:
    wrap = "pre-wrap" if monospace else "normal"
    return HTMLResponse(
        f"<pre style='font-size:.72rem;color:#94a3b8;white-space:{wrap};word-break:break-all;'>"
        f"{html.escape(str(text))}</pre>"
    )


def _render_ops_job(result: dict) -> HTMLResponse | None:
    """Render the ops async-job envelope, or None if this isn't one.

    centralmcp's show/diagnostic tools return
    ``{status, output: {commands, results: [{command, output}]}, ...}`` — the
    command output is nested two levels down under ``output.results[].output``,
    not a top-level string. format_ops_response's string-key scan skips it
    (``output`` is a dict), so without this every LLDP/ARP/MAC/STP panel fell
    through to a raw JSON dump. Secrets are masked in case a show command
    carries them.
    """
    output = result.get("output")
    if not isinstance(output, dict):
        return None
    results = output.get("results")
    if not isinstance(results, list) or not results:
        return None
    parts = []
    for item in results:
        if not isinstance(item, dict):
            continue
        cmd = str(item.get("command", "")).strip()
        body = mask_config_secrets(str(item.get("output", "")).strip())
        if not cmd and not body:
            continue
        head = (
            f"<p style='font-size:.65rem;color:#f97316;margin-bottom:4px;"
            f"font-weight:700;'>{html.escape(cmd)}</p>"
        ) if cmd else ""
        parts.append(
            head + f"<pre style='font-size:.72rem;color:#94a3b8;white-space:pre-wrap;"
            f"word-break:break-all;margin-bottom:12px;'>"
            f"{html.escape(body or '(no output)')}</pre>"
        )
    return HTMLResponse("".join(parts)) if parts else None


def format_ops_response(result) -> HTMLResponse:
    """Prefer structured tables/lists; avoid dumping raw dict repr to users."""
    if result is None:
        return format_ops_pre("No data returned.")

    if isinstance(result, str):
        return format_ops_pre(result)

    if isinstance(result, dict):
        job = _render_ops_job(result)
        if job is not None:
            return job

    if isinstance(result, list):
        if not result:
            return format_ops_pre("No records returned.")
        if all(isinstance(row, dict) for row in result):
            return _table_from_dicts(result)
        return format_ops_pre("\n".join(str(x) for x in result))

    if not isinstance(result, dict):
        return format_ops_pre(str(result))

    for key in ("output", "raw", "text", "message", "config"):
        val = result.get(key)
        if isinstance(val, str) and val.strip():
            return format_ops_pre(val)

    for key in ("neighbors", "ports", "entries", "items", "results", "data", "records"):
        items = result.get(key)
        if isinstance(items, list) and items and all(isinstance(row, dict) for row in items):
            return _table_from_dicts(items)

    if len(result) <= 6 and all(not isinstance(v, (dict, list)) for v in result.values()):
        rows = "".join(
            f"<tr><td class='text-slate-500 pr-3'>{html.escape(str(k))}</td>"
            f"<td class='text-slate-200'>{html.escape(str(v))}</td></tr>"
            for k, v in result.items()
        )
        return HTMLResponse(
            f"<table class='tbl text-xs'><tbody>{rows}</tbody></table>"
        )

    try:
        pretty = json.dumps(result, indent=2, default=str)
    except (TypeError, ValueError):
        pretty = "Unrecognized response shape."
    return format_ops_pre(pretty)


def _table_from_dicts(rows: list[dict]) -> HTMLResponse:
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows[:50]:
        for k in row:
            if k not in seen:
                seen.add(k)
                keys.append(k)
            if len(keys) >= 8:
                break
        if len(keys) >= 8:
            break
    if not keys:
        return format_ops_pre("No displayable fields.")

    head = "".join(f"<th>{html.escape(k)}</th>" for k in keys)
    body_rows = []
    for row in rows[:50]:
        cells = "".join(
            f"<td class='text-xs text-slate-300'>{html.escape(str(row.get(k, '')))}</td>"
            for k in keys
        )
        body_rows.append(f"<tr class='tbl-row'>{cells}</tr>")
    suffix = ""
    if len(rows) > 50:
        suffix = f"<p class='text-xs text-slate-500 mt-2'>{len(rows) - 50} more rows not shown.</p>"
    return HTMLResponse(
        f"<div class='overflow-x-auto'><table class='tbl text-xs'><thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table></div>{suffix}"
    )
