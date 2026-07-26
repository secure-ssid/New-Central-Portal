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


_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")


def parse_mac_table(text: str) -> list[dict]:
    """Parse `show mac-address-table` text into rows.

    AOS-CX columns are `MAC  VLAN  Type  Port` (Port is the last token). Only
    lines whose first token is a MAC are kept, so the header, separators and
    summary lines are skipped.
    """
    rows: list[dict] = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 4 and _MAC_RE.match(parts[0]):
            rows.append({"mac": parts[0], "vlan": parts[1],
                         "type": parts[2], "port": parts[-1]})
    return rows


def _num(v):
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return None


def _fmt_num(n, suffix: str = "") -> str:
    if n is None:
        return "—"
    s = f"{n:.0f}" if n == int(n) else f"{n:.3f}".rstrip("0").rstrip(".")
    return s + suffix


def format_ping_response(result) -> HTMLResponse:
    """Render a ping result.

    AOS-CX ping returns a STRUCTURED stats dict under `output`
    (transmitted/received counts, loss %, RTT) — not command text — so the old
    handler found no `output.results`, dumped the raw Python dict, and coloured
    it red because the literal word "success" never appears in a real ping. Read
    the stats and colour by actual reachability; fall back to text for platforms
    that return CLI output instead.
    """
    if not isinstance(result, dict):
        return format_ops_pre(str(result))

    out = result.get("output")
    if isinstance(out, dict) and ("receivedPacketsCount" in out or "transmittedPacketsCount" in out):
        tx, rx = _num(out.get("transmittedPacketsCount")), _num(out.get("receivedPacketsCount"))
        loss = _num(out.get("packetLossPercent"))
        dest = str(out.get("destination") or "")
        resolved = str(out.get("resolvedIp") or "")
        reachable = (rx or 0) > 0 and (loss is None or loss < 100)
        color = "#4ade80" if reachable else "#f87171"
        target = html.escape(dest)
        if resolved and resolved != dest:
            target += f" ({html.escape(resolved)})"
        rows = [
            ("Destination", target),
            ("Packets", f"{_fmt_num(tx)} sent &middot; {_fmt_num(rx)} received "
                        f"&middot; {_fmt_num(loss, '%')} loss"),
            ("Round-trip", f"min {_fmt_num(_num(out.get('minimumRoundTripTimeMilliseconds')), ' ms')} "
                           f"&middot; avg {_fmt_num(_num(out.get('averageRoundTripTimeMilliseconds')), ' ms')} "
                           f"&middot; max {_fmt_num(_num(out.get('maximumRoundTripTimeMilliseconds')), ' ms')}"),
        ]
        body = "".join(
            f"<tr><td style='color:#64748b;padding-right:12px;'>{k}</td>"
            f"<td style='color:#e2e8f0;'>{v}</td></tr>" for k, v in rows
        )
        return HTMLResponse(
            f"<p style='font-size:.8rem;color:{color};font-weight:700;margin-bottom:8px;'>"
            f"{'Reachable' if reachable else 'Unreachable'}</p>"
            f"<table class='tbl text-xs'><tbody>{body}</tbody></table>"
        )

    # Text fallback: CLI output (output.results / rawOutput), or a fail reason.
    text = ""
    if isinstance(out, dict) and isinstance(out.get("results"), list):
        text = "\n".join(str(r.get("output", "")) for r in out["results"] if isinstance(r, dict))
    if not text and isinstance(result.get("rawOutput"), str):
        text = result["rawOutput"]
    if not text and result.get("failReason"):
        text = str(result["failReason"])
    if not text:
        return format_ops_response(result)
    low = text.lower()
    ok = (("0% packet loss" in low) or ("bytes from" in low) or ("reply from" in low)) \
        and "100% packet loss" not in low
    color = "#4ade80" if ok else "#f87171"
    return HTMLResponse(
        f"<pre style='font-size:.72rem;color:{color};white-space:pre-wrap;"
        f"word-break:break-all;'>{html.escape(text)}</pre>"
    )


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
