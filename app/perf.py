"""Shared performance helpers: pagination, date bounds, slow-request logging."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any

from flask import g, request

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def parse_page(raw: Any, default: int = 1) -> int:
    try:
        page = int(raw or default)
    except (TypeError, ValueError):
        page = default
    return max(1, page)


def parse_page_size(raw: Any, default: int = DEFAULT_PAGE_SIZE) -> int:
    try:
        size = int(raw or default)
    except (TypeError, ValueError):
        size = default
    return max(1, min(size, MAX_PAGE_SIZE))


def pagination_meta(total: int, page: int, page_size: int) -> dict[str, Any]:
    total = max(0, int(total or 0))
    page_size = max(1, int(page_size or DEFAULT_PAGE_SIZE))
    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    page = min(max(1, int(page or 1)), total_pages)
    offset = (page - 1) * page_size
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "offset": offset,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_page": page - 1 if page > 1 else None,
        "next_page": page + 1 if page < total_pages else None,
    }


def as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def day_bounds(day) -> tuple[date, date]:
    """Inclusive calendar day as half-open [start, end) for index-friendly filters."""
    start = as_date(day) or date.today()
    return start, start + timedelta(days=1)


def through_exclusive(through_day) -> date:
    """Exclusive upper bound for 'on or before through_day' timestamp filters."""
    day = as_date(through_day) or date.today()
    return day + timedelta(days=1)


def register_perf_hooks(app) -> None:
    """Log slow requests so route regressions are visible in production logs."""

    @app.before_request
    def _perf_before():
        g._perf_t0 = time.perf_counter()

    @app.after_request
    def _perf_after(response):
        t0 = getattr(g, "_perf_t0", None)
        if t0 is not None:
            ms = (time.perf_counter() - t0) * 1000.0
            response.headers["X-Response-Time-Ms"] = f"{ms:.1f}"
            if ms >= 800 and request.endpoint:
                logger.warning(
                    "slow_request endpoint=%s method=%s path=%s status=%s ms=%.1f",
                    request.endpoint,
                    request.method,
                    request.path,
                    response.status_code,
                    ms,
                )
        return response
