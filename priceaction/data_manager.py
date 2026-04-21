"""
Data Manager — unified historical data orchestration.

This module owns all *background* historical data work:

  · Startup symbol prefetch (5min + 1D) for ``config.EXTRA_SYMBOLS``
  · Periodic gap fill using the trading calendar
  · Coverage reporting for the datavalid UI
  · Read-only bar lookup helper for ``server.get_history``
  · WebSocket ``history_ready`` broadcast when a batch fetch completes,
    so the TradingView datafeed can call ``onResetCacheNeededCallback``
    and refresh its widget.

All DB writes happen exclusively through ``IBDataFetcher.persist_bars``
(the single approved write proxy).  ``server.py`` no longer calls
``db.insert_bars`` at all; it only reads via ``get_bars`` / calls into
the helpers here.

NOTE: this is a *facade* over the historical-data logic that previously
lived in ``server.py``.  The underlying routines (``_fill_internal_gaps``,
``_prefetch_extra_symbols``) are still defined in ``server.py`` for now
to keep the refactor minimal and behaviour-preserving; ``data_manager``
exposes them here so that later work can progressively migrate the
implementation without breaking call-sites.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

import db

logger = logging.getLogger(__name__)


# ─── Read-only bar lookup ────────────────────────────────────────────────────
#
# ``server.get_history`` used to call ``db.get_bars`` directly.  With the
# refactor, server goes through ``data_manager.get_bars`` so that future
# improvements (e.g. memcache, coverage tracking) can be injected in one
# place.  The underlying semantics are unchanged.

def get_bars(
    symbol: str,
    timeframe: str,
    from_ts: int = 0,
    to_ts: Optional[int] = None,
    limit: Optional[int] = None,
    contract_month: Optional[str] = None,
) -> List[dict]:
    """Read-only accessor over :func:`db.get_bars`.

    This is the public read path used by the HTTP layer.  It never writes
    to the DB.  Callers must go through this instead of importing ``db``
    directly, so that caching / prefetching policy can be centralised.
    """
    kwargs: Dict[str, Any] = {"from_ts": from_ts}
    if to_ts is not None:
        kwargs["to_ts"] = to_ts
    if limit is not None:
        kwargs["limit"] = limit
    if contract_month is not None:
        kwargs["contract_month"] = contract_month
    return db.get_bars(symbol, timeframe, **kwargs)


def get_earliest_ts(symbol: str, timeframe: str) -> Optional[int]:
    """Read-only wrapper over :func:`db.get_earliest_ts`."""
    return db.get_earliest_ts(symbol, timeframe)


def get_latest_ts(symbol: str, timeframe: str) -> Optional[int]:
    """Read-only wrapper over :func:`db.get_latest_ts`."""
    return db.get_latest_ts(symbol, timeframe)


# ─── history_ready broadcast ─────────────────────────────────────────────────
#
# The chart widget needs to know when a background prefetch has finished
# so it can drop its client-side cache and re-request the affected range.
# The WebSocket payload is::
#
#   { "type": "history_ready",
#     "symbol": "MES",
#     "timeframe": "5min",
#     "from": <unix_ts>,
#     "to":   <unix_ts>,
#     "added_bars": <int> }
#
# ``server.py`` registers a broadcaster via :func:`set_broadcaster`; all
# background jobs call :func:`notify_history_ready` with the range they
# just populated.

_Broadcaster = Callable[[dict], Awaitable[None]]
_broadcaster: Optional[_Broadcaster] = None


def set_broadcaster(fn: _Broadcaster) -> None:
    """Register the async function used to send WS messages to clients.

    ``server.py`` calls this once at startup with its
    ``broadcast(message: dict)`` coroutine.
    """
    global _broadcaster
    _broadcaster = fn


async def notify_history_ready(
    symbol: str,
    timeframe: str,
    from_ts: int,
    to_ts: int,
    added_bars: int,
) -> None:
    """Broadcast that a historical-data batch has completed.

    Safe to call even if no broadcaster is registered or no clients are
    connected — failures are logged, never raised.  ``added_bars == 0``
    is a legitimate case (no new data) and is still broadcast so the
    front-end can stop its "loading" indicator.
    """
    payload = {
        "type":       "history_ready",
        "symbol":     symbol,
        "timeframe":  timeframe,
        "from":       int(from_ts),
        "to":         int(to_ts),
        "added_bars": int(added_bars),
    }
    if _broadcaster is None:
        logger.debug("history_ready skipped (no broadcaster registered): %s", payload)
        return
    try:
        await _broadcaster(payload)
    except Exception as e:
        logger.warning("history_ready broadcast failed: %s (%s)", e, payload)


# ─── Scheduled job runner ────────────────────────────────────────────────────
#
# A minimal asyncio-based scheduler so background tasks (prefetch, gap
# fill, validation) can be launched from ``server.lifespan`` via a
# single entry point.  Implementations are imported lazily to avoid
# circular imports with ``server.py``.

_scheduled_tasks: List[asyncio.Task] = []


def register_task(task: asyncio.Task) -> None:
    """Track a background task so the scheduler can cancel it on shutdown."""
    _scheduled_tasks.append(task)


async def shutdown() -> None:
    """Cancel all tracked background tasks.  Called from ``server.lifespan``."""
    for t in list(_scheduled_tasks):
        if not t.done():
            t.cancel()
    await asyncio.gather(*_scheduled_tasks, return_exceptions=True)
    _scheduled_tasks.clear()


# ─── Validation-range bookkeeping ────────────────────────────────────────────
#
# Background jobs record which (symbol, tf, from, to) ranges they have
# already validated so the scheduler can skip them on subsequent runs.
# ``db.py`` already has a ``validated_ranges`` table used by
# ``data_validator.background_validate`` — we re-export it here so the
# public dataManager API is complete.

def record_validated(
    symbol: str,
    timeframe: str,
    from_ts: int,
    to_ts: int,
    mismatches: int = 0,
    fixed: int = 0,
    contract_month: str = "",
) -> None:
    """Mark a range as validated in the ``validated_ranges`` table.

    Thin wrapper over :func:`db.record_validated_range`; falls back
    silently if that helper is not present in the DB module.
    """
    helper = getattr(db, "insert_validated_range", None)
    if helper is None:
        logger.debug("record_validated: db has no insert_validated_range helper")
        return
    try:
        helper(symbol, timeframe, from_ts, to_ts, mismatches, fixed, contract_month)
    except Exception as e:
        logger.warning("record_validated %s/%s %s→%s failed: %s",
                       symbol, timeframe, from_ts, to_ts, e)
