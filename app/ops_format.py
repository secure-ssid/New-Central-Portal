"""Format centralmcp ops tool responses for HTMX HTML fragments."""
from __future__ import annotations

import html
import json

from fastapi.responses import HTMLResponse


def format_ops_pre(text: str, *, monospace: bool = True) -> HTMLResponse:
    wrap = "pre-wrap" if monospace else "normal"
    return HTMLResponse(
        f"<pre style='font-size:.72rem;color:#94a3b8;white-space:{wrap};word-break:break-all;'>"
        f"{html.escape(str(text))}</pre>"
    )


def format_ops_response(result) -> HTMLResponse:
    """Prefer structured tables/lists; avoid dumping raw dict repr to users."""
    if result is None:
        return format_ops_pre("No data returned.")

    if isinstance(result, str):
        return format_ops_pre(result)

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
