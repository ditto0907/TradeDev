"""
Realtime Builder — facade for real-time bar aggregation and persistence.

Today the heavy lifting (tick → 5-min bar aggregation, ``_rt_current`` state
management, new-bar callbacks) still lives inside ``IBDataFetcher`` where it
was consolidated during the v1 refactor — the "unified tick handler" path
described in ``doc/refactor.md``.  This module exposes a stable, narrow API
so ``server.py`` and any future extractors only couple to
``realtime_builder`` instead of the fetcher's internals.

Responsibilities (now and after the full refactor):

  · Persist a *completed* realtime bar exactly once via
    ``IBDataFetcher.persist_bars`` (the single DB-write proxy).
  · Upsert the currently-forming in-progress bar to ``realtime_bars`` so
    it survives a server restart.
  · Validate OHLCV integrity before any persistence using
    ``data_validator.validate_bar`` — invalid bars are dropped.

``server.on_new_bar`` calls ``persist_completed_bar`` / ``persist_inprogress_bar``
instead of ``db.insert_bars`` / ``db.upsert_realtime_bar`` directly, keeping
all DB writes funnelled through IBfetch.
"""
from __future__ import annotations

import logging
from typing import List, Optional

import db

logger = logging.getLogger(__name__)


def persist_completed_bar(
    fetcher,
    symbol: str,
    timeframe: str,
    bar: dict,
) -> int:
    """Validate and persist a just-completed realtime bar.

    Returns the number of rows saved (0 if the bar failed validation or
    the write was rejected).

    The bar is written with ``source="realtime_completed"`` so a later
    IB historical fetch can overwrite it with the authoritative value.
    """
    # Lazy import so this module is loadable in environments (tests,
    # scripts) that don't have ``ib_insync`` installed.
    try:
        import data_validator as _dv
        violations = _dv.validate_bar(bar, symbol)
    except Exception as e:
        logger.debug("validate_bar unavailable (%s) — skipping pre-check", e)
        violations = []
    if violations:
        logger.warning(
            "Rejecting realtime-completed bar %s/%s ts=%s: %s",
            symbol, timeframe, bar.get("time"), "; ".join(violations),
        )
        return 0
    try:
        return fetcher.persist_bars(
            symbol, timeframe, [bar], source="realtime_completed",
        )
    except Exception as e:
        logger.warning(
            "Failed to persist realtime-completed bar %s/%s: %s",
            symbol, timeframe, e,
        )
        return 0


def persist_inprogress_bar(
    symbol: str,
    timeframe: str,
    bar: dict,
) -> None:
    """Upsert an in-progress (currently-forming) realtime bar.

    Writes to the ``realtime_bars`` table — a separate table from the
    main ``bars`` store — so it can be reloaded after a server restart.
    Failures are swallowed at DEBUG level because high-frequency tick
    handlers must never raise.
    """
    try:
        db.upsert_realtime_bar(symbol, timeframe, bar)
    except Exception as e:
        logger.debug(
            "Failed to upsert in-progress bar for %s/%s: %s",
            symbol, timeframe, e,
        )
