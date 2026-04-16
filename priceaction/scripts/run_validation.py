#!/usr/bin/env python3
"""
Standalone data validation & fix script.
Connects directly to IB with its own clientId, independent of the server.

Usage:
    python3 scripts/run_validation.py              # validate only (report)
    python3 scripts/run_validation.py --fix        # validate and fix mismatches
    python3 scripts/run_validation.py --symbol MES --timeframe 5min  # specific pair
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

# Add parent directory to path so project modules can be imported
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Project imports
import config
import db
import data_validator
from ib_data_fetcher import _key_to_ib

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("run_validation")


async def run(args):
    db.init_db()

    from ib_insync import IB
    ib = IB()
    logger.info("Connecting to IB at %s:%s (clientId=%d)...",
                config.IB_HOST, config.IB_PORT, config.IB_CLIENT_ID + 80)
    await ib.connectAsync(config.IB_HOST, config.IB_PORT,
                          clientId=config.IB_CLIENT_ID + 80, timeout=20)
    logger.info("IB connected")

    # Skip data older than N days
    max_age_ts = int(time.time()) - args.max_age_days * 86400

    try:
        if args.symbol and args.timeframe:
            # Single pair mode
            pairs = [(args.symbol, args.timeframe)]
        else:
            # All pairs
            with db._conn() as conn:
                pairs = conn.execute(
                    "SELECT DISTINCT symbol, timeframe FROM bars"
                ).fetchall()

        total_mismatches = 0
        total_fixed = 0
        total_ib_only = 0
        results = []

        for sym, tf in pairs:
            earliest = db.get_earliest_ts(sym, tf)
            latest = db.get_latest_ts(sym, tf)
            if earliest is None or latest is None:
                continue

            _, interval = _key_to_ib(tf)

            # Use larger chunks for daily bars
            if tf == "1D":
                chunk_secs = 30 * 86400  # 30 days
            else:
                chunk_secs = 86400  # 1 day

            effective_earliest = max(earliest, max_age_ts)
            if effective_earliest > latest:
                logger.info("[%s/%s] All data older than %d days, skipping",
                            sym, tf, args.max_age_days)
                continue

            logger.info("=" * 60)
            logger.info("Scanning %s/%s  from %s to %s",
                        sym, tf,
                        datetime.fromtimestamp(effective_earliest, tz=timezone.utc).strftime("%Y-%m-%d"),
                        datetime.fromtimestamp(latest, tz=timezone.utc).strftime("%Y-%m-%d"))
            logger.info("=" * 60)

            sym_mismatches = 0
            sym_fixed = 0
            sym_ib_only = 0
            chunks_done = 0

            chunk_start = effective_earliest
            while chunk_start <= latest:
                chunk_end = min(chunk_start + chunk_secs, latest)

                try:
                    if args.fix:
                        r = await data_validator.fix_bars(
                            sym, tf, chunk_start, chunk_end, ib=ib)
                        sym_fixed += r["fixed_count"]
                        sym_ib_only += r.get("ib_only_inserted", 0)
                    else:
                        r = await data_validator.validate_bars(
                            sym, tf, chunk_start, chunk_end, ib=ib)

                    chunks_done += 1
                    sym_mismatches += r["mismatch_count"]

                    if r["mismatch_count"] > 0:
                        for m in r["mismatches"]:
                            ts_str = datetime.fromtimestamp(
                                m["time"], tz=timezone.utc
                            ).strftime("%Y-%m-%d %H:%M")
                            diffs_str = ", ".join(
                                f"{k}: DB={v[0]:.2f} IB={v[1]:.2f}"
                                for k, v in m["diffs"].items()
                            )
                            logger.warning("  MISMATCH %s: %s", ts_str, diffs_str)

                    if chunks_done % 10 == 0:
                        logger.info("  ... %d chunks done, %d mismatches so far",
                                    chunks_done, sym_mismatches)

                except Exception as e:
                    logger.warning("  Chunk %s→%s failed: %s",
                                   chunk_start, chunk_end, e)

                chunk_start = chunk_end + interval

                # IB pacing: ~2s between requests
                await asyncio.sleep(2)

            logger.info("[%s/%s] Done: %d chunks, %d mismatches, %d fixed, %d IB-only inserted",
                        sym, tf, chunks_done, sym_mismatches, sym_fixed, sym_ib_only)

            total_mismatches += sym_mismatches
            total_fixed += sym_fixed
            total_ib_only += sym_ib_only

            results.append({
                "symbol": sym, "timeframe": tf,
                "chunks": chunks_done, "mismatches": sym_mismatches,
                "fixed": sym_fixed, "ib_only": sym_ib_only,
            })

        logger.info("")
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info("=" * 60)
        for r in results:
            status = "OK" if r["mismatches"] == 0 else f"{r['mismatches']} MISMATCHES"
            fix_info = f", {r['fixed']} fixed" if args.fix else ""
            logger.info("  %s/%s: %s (%d chunks%s)",
                        r["symbol"], r["timeframe"], status, r["chunks"], fix_info)
        logger.info("Total: %d mismatches, %d fixed, %d IB-only inserted",
                    total_mismatches, total_fixed, total_ib_only)

    finally:
        ib.disconnect()
        logger.info("IB disconnected")


def main():
    parser = argparse.ArgumentParser(description="Validate/fix DB bars against IB")
    parser.add_argument("--fix", action="store_true",
                        help="Fix mismatches (overwrite DB with IB data)")
    parser.add_argument("--symbol", type=str, default=None,
                        help="Specific symbol (e.g. MES, MNQ)")
    parser.add_argument("--timeframe", type=str, default=None,
                        help="Specific timeframe (e.g. 5min, 1D)")
    parser.add_argument("--max-age-days", type=int, default=180,
                        help="Skip data older than N days (default: 180)")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
