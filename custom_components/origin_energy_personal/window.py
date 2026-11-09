"""Pure window-planning helpers (no Home Assistant imports, so unit-testable)."""
from __future__ import annotations

from datetime import datetime, timedelta

from .const import FIRST_RUN_HISTORY_DAYS, HOURLY_CHUNK_DAYS, TRAILING_DAYS


def plan_start(
    now: datetime, last_start: datetime | None, valid_from: datetime | None
) -> datetime:
    """Where this sync should start reading (UTC, floored to the hour).

    First run: back to the agreement start (nothing exists before it),
    capped at FIRST_RUN_HISTORY_DAYS.

    Later runs: TRAILING_DAYS before the newest stored hour. Measured from the
    newest *data*, not wall-clock now, because Origin restates the last day or
    two of what it has published, however late that publication was.
    """
    if last_start is None:
        start = now - timedelta(days=FIRST_RUN_HISTORY_DAYS)
    else:
        start = last_start - timedelta(days=TRAILING_DAYS)
    if valid_from is not None:
        start = max(start, valid_from)
    return start.replace(minute=0, second=0, microsecond=0)


def plan_chunks(
    start: datetime, end: datetime, days: int = HOURLY_CHUNK_DAYS
) -> list[tuple[datetime, datetime]]:
    """Contiguous query windows, each within Origin's sub-daily day limit."""
    windows: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        nxt = min(cursor + timedelta(days=days), end)
        windows.append((cursor, nxt))
        cursor = nxt
    return windows
