# Migration v1 → v2 (service-oriented refactor)

> **Scope**: `priceaction/` — splits historical-data management,
> validation, realtime aggregation, and IB + DB writes into dedicated
> services with one clear owner per responsibility.

## Summary

The v2 refactor establishes this layering (see also `doc/refactor.md`):

```
server.py  →  data_manager + data_validator + realtime_builder  →  IBDataFetcher  →  IB / SQLite
(read-only)    (historical) (validation)    (realtime bars)        (sole write proxy)
```

Concretely:

* **Server** no longer calls `db.insert_bars` — all writes go through
  `IBDataFetcher.persist_bars`.
* **`data_manager.py`** exposes read-only history lookups and a
  `history_ready` WS broadcast for batch-prefetch completion.
* **`realtime_builder.py`** owns real-time bar completion (validates
  via `data_validator.validate_bar`, persists via `fetcher.persist_bars`).
* **`data_validator.py`** gains `validate_bar` / `classify_gaps` /
  `data_gaps_only` as the single validation entry point.
* **`contract_calendar.py`** replaces the old "day ≤ 10 of month"
  rollover heuristic with the official exchange rules declared in
  `config.INSTRUMENTS[symbol]["rollover_rule"]`.

## Schema impact

**None required.**  The `bars`, `ib_fetch_cache`, `realtime_bars`, and
`validated_ranges` tables are unchanged between v1 and v2.  What *does*
change is the value of the existing `bars.contract_month` column for
rows whose timestamp falls near a rollover boundary — the v2 calendar
derives contract months differently than the v1 heuristic.

A `PRAGMA user_version` bump (0/1 → 2) signals that the v2 migration
has been applied.

## Running the migration

```
cd priceaction
python scripts/migrate_v2.py            # apply (idempotent)
python scripts/migrate_v2.py --dry-run  # report pending changes only
python scripts/migrate_v2.py --force    # re-apply even if already v2
```

The script walks every row in the `bars` table and, for each, recomputes
`contract_month` via `contract_calendar.active_contract`.  Only rows
whose derived month differs from the stored value are rewritten.  On
success `PRAGMA user_version` is set to 2.

Typical output on a fresh v2 install (no historical drift):

```
Scanned N bars, 0 need update.
v2 migration complete: {'scanned': N, 'changed': 0, 'written': 0}
```

## Rollback

Downgrading is supported for cases where the caller needs to roll back
to the v1 heuristic (e.g. to preserve bit-exact compatibility with a
tool that encodes the day-10 convention):

```
python scripts/migrate_v2.py --downgrade
```

This rewrites `contract_month` back to the legacy derivation and resets
`PRAGMA user_version` to 1.  The rest of the v2 code paths
(`persist_bars`, `data_manager`, `realtime_builder`) remain active —
only the tagged contract months change.

## Verification checklist

1. `python priceaction/tests/test_contract_calendar.py` → 13 passes.
2. `python scripts/migrate_v2.py --dry-run` → report sane.
3. Start the server, open a chart — bars still load; the console shows
   `DataFeed WebSocket connected`.
4. Trigger a large range scroll → after the background fetch completes,
   observe `DataFeed: history_ready …` in the browser console and the
   chart refreshing without a manual reload.
5. Confirm `grep 'db\.insert_bars' priceaction/server.py` returns no
   matches — server is now read-only w.r.t. the `bars` table.

## Safety properties

* **Idempotent** — repeat runs are no-ops.
* **Reversible** — `--downgrade` fully reverts the only schema-visible
  change (the `contract_month` column values).
* **Transactional** — all updates commit in a single batch; a crashed
  migration leaves the DB unchanged except for rows already written.
* **Non-blocking** — can be run while the server is offline; the server
  should be restarted afterwards to pick up the new `contract_calendar`
  behaviour in live contract lookups.
