"""Shared server-side pagination for list routes."""
from urllib.parse import urlencode

from starlette.requests import Request

DEFAULT_PER_PAGE = 50
MAX_PER_PAGE = 200


def filter_items(items: list, q: str, *fields: str) -> list:
    """Case-insensitive substring filter across named dict keys."""
    query = q.strip().lower()
    if not query:
        return items
    return [
        item for item in items
        if isinstance(item, dict)
        and any(item.get(f) and query in str(item[f]).lower() for f in fields)
    ]


def paginate(request: Request, items: list) -> dict:
    """Slice ``items`` for the current request's ``page``/``per_page`` params.

    ``page`` is 1-based (default 1, clamped into range); ``per_page`` defaults
    to 50 and is clamped to 1..200. Invalid/non-numeric values fall back to
    the defaults instead of erroring. Returns the page slice plus
    template-ready metadata, including ``base_qs`` — the current query string
    minus ``page`` — so pagination links preserve every other parameter.
    """
    try:
        per_page = int(request.query_params.get("per_page", DEFAULT_PER_PAGE))
    except (TypeError, ValueError):
        per_page = DEFAULT_PER_PAGE
    per_page = max(1, min(MAX_PER_PAGE, per_page))

    try:
        page = int(request.query_params.get("page", 1))
    except (TypeError, ValueError):
        page = 1

    total = len(items)
    total_pages = max(1, -(-total // per_page))  # ceil div, min 1
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page

    base_qs = urlencode(
        [(k, v) for k, v in request.query_params.multi_items() if k != "page"]
    )
    return {
        "items": items[start:start + per_page],
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "base_qs": base_qs,
    }
