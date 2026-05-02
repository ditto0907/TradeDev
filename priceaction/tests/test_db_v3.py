"""
Unit tests for the v3 database layer in :mod:`db`.

Covers:
  * Schema creation (bars/realtime_bars/ib_fetch_cache/validated_ranges/bar_revisions)
  * ``insert_bars`` validation, rank-guard, and revision audit
  * Realtime bar upsert/delete/get
  * IB cache contract_token model
  * validated_ranges per-contract_month tracking
  * bar_revisions audit trail

Run with::

    python -m pytest priceaction/tests/test_db_v3.py -v
or::
    python priceaction/tests/test_db_v3.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Make ``priceaction/`` importable when running this file directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _bar(ts, o, h, l, c, v, contract_month, source):
    return {
        "time": ts, "open": o, "high": h, "low": l, "close": c,
        "volume": v, "contract_month": contract_month, "source": source,
    }


class _DbTestCase(unittest.TestCase):
    """Base class — every test gets an isolated SQLite file."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmpdir.name) / "test.db"
        db._set_db_path_for_testing(self._db_path)
        db.init_db()

    def tearDown(self) -> None:
        # Drain pool before deleting tempdir
        db._set_db_path_for_testing(Path(self._tmpdir.name) / "noop.db")
        self._tmpdir.cleanup()


# ── Schema sanity ─────────────────────────────────────────────────────────────


class TestSchema(_DbTestCase):
    def test_bars_table_columns(self):
        with db._conn() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(bars)").fetchall()}
        for c in ("symbol", "contract_month", "timeframe", "ts",
                  "open", "high", "low", "close", "volume",
                  "source", "source_rank", "fetched_at"):
            self.assertIn(c, cols, f"bars missing column {c}")

    def test_bars_pk_is_per_contract(self):
        with db._conn() as conn:
            pk = [r[1] for r in conn.execute("PRAGMA table_info(bars)").fetchall() if r[5]]
        self.assertEqual(set(pk),
                         {"symbol", "contract_month", "timeframe", "ts"})

    def test_ib_fetch_cache_uses_contract_token(self):
        with db._conn() as conn:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(ib_fetch_cache)").fetchall()}
        self.assertIn("contract_token", cols)
        self.assertNotIn("contract_month", cols)

    def test_realtime_bars_pk_includes_contract_month(self):
        with db._conn() as conn:
            pk = [r[1] for r in conn.execute(
                "PRAGMA table_info(realtime_bars)").fetchall() if r[5]]
        self.assertEqual(set(pk),
                         {"symbol", "contract_month", "timeframe"})

    def test_bar_revisions_table_exists(self):
        with db._conn() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='bar_revisions'"
            ).fetchone()
        self.assertIsNotNone(row)

    def test_validated_ranges_has_contract_month(self):
        with db._conn() as conn:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(validated_ranges)").fetchall()}
        self.assertIn("contract_month", cols)


# ── Source rank table ─────────────────────────────────────────────────────────


class TestSourceRank(unittest.TestCase):
    def test_known_ranks(self):
        self.assertEqual(db.source_rank("ib_validated"), 100)
        self.assertEqual(db.source_rank("ib_monthly"),    80)
        self.assertEqual(db.source_rank("ib_historical"), 60)
        self.assertEqual(db.source_rank("realtime_completed"), 20)
        self.assertEqual(db.source_rank("ib_continuous"), 0)
        self.assertEqual(db.source_rank("unknown"),       0)

    def test_unknown_source_is_zero(self):
        self.assertEqual(db.source_rank("totally_made_up"), 0)


# ── insert_bars: validation ───────────────────────────────────────────────────


class TestInsertBarsValidation(_DbTestCase):
    def test_basic_insert(self):
        bars = [_bar(1000, 10, 11, 9, 10.5, 100, "202606", "ib_monthly")]
        out = db.insert_bars("MES", "5min", bars)
        self.assertEqual(out["inserted"], 1)
        self.assertEqual(out["replaced"], 0)
        self.assertEqual(out["rejected_validation"], 0)

        rows = db.get_bars("MES", "5min")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "ib_monthly")
        self.assertEqual(rows[0]["source_rank"], 80)
        self.assertEqual(rows[0]["contract_month"], "202606")

    def test_reject_empty_contract_month(self):
        bars = [_bar(1000, 10, 11, 9, 10.5, 100, "", "ib_monthly")]
        out = db.insert_bars("MES", "5min", bars)
        self.assertEqual(out["inserted"], 0)
        self.assertEqual(out["rejected_validation"], 1)
        self.assertEqual(len(db.get_bars("MES", "5min")), 0)

    def test_reject_ib_continuous_source(self):
        bars = [_bar(1000, 10, 11, 9, 10.5, 100, "202606", "ib_continuous")]
        out = db.insert_bars("MES", "5min", bars)
        self.assertEqual(out["inserted"], 0)
        self.assertEqual(out["rejected_validation"], 1)
        self.assertEqual(len(db.get_bars("MES", "5min")), 0)

    def test_reject_high_below_low(self):
        bars = [_bar(1000, 10, 9, 11, 10.5, 100, "202606", "ib_monthly")]
        out = db.insert_bars("MES", "5min", bars)
        self.assertEqual(out["rejected_validation"], 1)

    def test_reject_non_positive_price(self):
        bars = [_bar(1000, 0, 11, 9, 10.5, 100, "202606", "ib_monthly")]
        out = db.insert_bars("MES", "5min", bars)
        self.assertEqual(out["rejected_validation"], 1)

    def test_reject_negative_volume(self):
        bars = [_bar(1000, 10, 11, 9, 10.5, -1, "202606", "ib_monthly")]
        out = db.insert_bars("MES", "5min", bars)
        self.assertEqual(out["rejected_validation"], 1)

    def test_default_source_kwarg_applied(self):
        bars = [{"time": 1000, "open": 10, "high": 11, "low": 9,
                 "close": 10.5, "volume": 100, "contract_month": "202606"}]
        out = db.insert_bars("MES", "5min", bars, source="ib_validated")
        self.assertEqual(out["inserted"], 1)
        self.assertEqual(db.get_bars("MES", "5min")[0]["source"], "ib_validated")


# ── Rank-guard ────────────────────────────────────────────────────────────────


class TestRankGuard(_DbTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Seed an ib_validated bar (rank=100)
        db.insert_bars("MES", "5min",
                       [_bar(1000, 10, 11, 9, 10.5, 100, "202606", "ib_validated")])

    def test_lower_rank_blocked(self):
        # realtime_completed (20) cannot overwrite ib_validated (100)
        out = db.insert_bars("MES", "5min",
                             [_bar(1000, 99, 99, 99, 99, 999, "202606",
                                   "realtime_completed")])
        self.assertEqual(out["rejected_rank"], 1)
        self.assertEqual(out["replaced"], 0)
        # Original row preserved
        rows = db.get_bars("MES", "5min")
        self.assertEqual(rows[0]["close"], 10.5)
        self.assertEqual(rows[0]["source"], "ib_validated")

    def test_same_rank_overwrites(self):
        # Another ib_validated row with new values should overwrite
        out = db.insert_bars("MES", "5min",
                             [_bar(1000, 12, 13, 11, 12.5, 200, "202606",
                                   "ib_validated")],
                             reason="manual_fix")
        self.assertEqual(out["replaced"], 1)
        self.assertEqual(out["revisions"], 1)
        rows = db.get_bars("MES", "5min")
        self.assertEqual(rows[0]["close"], 12.5)

    def test_higher_rank_overwrites_lower(self):
        # First seed a low-rank row at a different ts
        db.insert_bars("MES", "5min",
                       [_bar(2000, 10, 11, 9, 10.5, 100, "202606",
                             "realtime_completed")])
        # ib_monthly should overwrite
        out = db.insert_bars("MES", "5min",
                             [_bar(2000, 12, 13, 11, 12.5, 200, "202606",
                                   "ib_monthly")])
        self.assertEqual(out["replaced"], 1)
        rows = db.get_bars("MES", "5min", from_ts=2000, to_ts=2000)
        self.assertEqual(rows[0]["source"], "ib_monthly")
        self.assertEqual(rows[0]["close"], 12.5)

    def test_identical_value_no_revision(self):
        # Re-inserting the exact same bar should be a no-op (no revision row)
        out = db.insert_bars("MES", "5min",
                             [_bar(1000, 10, 11, 9, 10.5, 100, "202606",
                                   "ib_validated")])
        self.assertEqual(out["replaced"], 0)
        self.assertEqual(out["revisions"], 0)
        revisions = db.get_bar_revisions(symbol="MES")
        self.assertEqual(len(revisions), 0)


# ── Bar revisions audit ───────────────────────────────────────────────────────


class TestBarRevisions(_DbTestCase):
    def test_revision_recorded_on_overwrite(self):
        db.insert_bars("MES", "5min",
                       [_bar(1000, 10, 11, 9, 10.5, 100, "202606",
                             "ib_monthly")])
        db.insert_bars("MES", "5min",
                       [_bar(1000, 12, 13, 11, 12.5, 200, "202606",
                             "ib_validated")],
                       reason="bg_validate")
        revisions = db.get_bar_revisions(symbol="MES", ts=1000)
        self.assertEqual(len(revisions), 1)
        rev = revisions[0]
        self.assertEqual(rev["prev_source"], "ib_monthly")
        self.assertEqual(rev["prev_rank"], 80)
        self.assertEqual(rev["new_source"], "ib_validated")
        self.assertEqual(rev["new_rank"], 100)
        self.assertEqual(rev["prev_close"], 10.5)
        self.assertIn("c:10.5->12.5", rev["diff_summary"])
        self.assertEqual(rev["reason"], "bg_validate")

    def test_no_revision_on_first_insert(self):
        db.insert_bars("MES", "5min",
                       [_bar(1000, 10, 11, 9, 10.5, 100, "202606",
                             "ib_monthly")])
        self.assertEqual(len(db.get_bar_revisions()), 0)

    def test_no_revision_on_blocked_overwrite(self):
        db.insert_bars("MES", "5min",
                       [_bar(1000, 10, 11, 9, 10.5, 100, "202606",
                             "ib_validated")])
        # Lower rank blocked → no revision
        db.insert_bars("MES", "5min",
                       [_bar(1000, 99, 99, 99, 99, 999, "202606",
                             "realtime_completed")])
        self.assertEqual(len(db.get_bar_revisions()), 0)


# ── Per-contract storage ──────────────────────────────────────────────────────


class TestPerContractStorage(_DbTestCase):
    def test_same_ts_two_contracts(self):
        # Both rows must coexist (rollover overlap)
        db.insert_bars("MES", "5min",
                       [_bar(1000, 10, 11, 9, 10.5, 100, "202603", "ib_monthly")])
        db.insert_bars("MES", "5min",
                       [_bar(1000, 20, 21, 19, 20.5, 200, "202606", "ib_monthly")])
        all_rows = db.get_bars("MES", "5min")
        self.assertEqual(len(all_rows), 2)
        cm_set = {r["contract_month"] for r in all_rows}
        self.assertEqual(cm_set, {"202603", "202606"})

    def test_filter_by_contract_month(self):
        db.insert_bars("MES", "5min",
                       [_bar(1000, 10, 11, 9, 10.5, 100, "202603", "ib_monthly")])
        db.insert_bars("MES", "5min",
                       [_bar(1000, 20, 21, 19, 20.5, 200, "202606", "ib_monthly")])
        rows = db.get_bars("MES", "5min", contract_month="202606")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["close"], 20.5)


# ── Realtime bars (per-contract) ──────────────────────────────────────────────


class TestRealtimeBars(_DbTestCase):
    def test_upsert_and_get(self):
        db.upsert_realtime_bar("MES", "5min",
                               {"time": 1000, "open": 10, "high": 11,
                                "low": 9, "close": 10.5, "volume": 50,
                                "contract_month": "202606"})
        bar = db.get_realtime_bar("MES", "202606", "5min")
        self.assertIsNotNone(bar)
        self.assertEqual(bar["close"], 10.5)

    def test_upsert_requires_contract_month(self):
        with self.assertRaises(ValueError):
            db.upsert_realtime_bar("MES", "5min",
                                   {"time": 1000, "open": 10, "high": 11,
                                    "low": 9, "close": 10.5, "volume": 50})

    def test_two_contracts_coexist(self):
        db.upsert_realtime_bar("MES", "5min",
                               {"time": 1000, "open": 10, "high": 11,
                                "low": 9, "close": 10.5, "volume": 50,
                                "contract_month": "202603"})
        db.upsert_realtime_bar("MES", "5min",
                               {"time": 1000, "open": 20, "high": 21,
                                "low": 19, "close": 20.5, "volume": 60,
                                "contract_month": "202606"})
        all_bars = db.get_all_realtime_bars()
        self.assertEqual(len(all_bars), 2)

    def test_delete_realtime_bar(self):
        db.upsert_realtime_bar("MES", "5min",
                               {"time": 1000, "open": 10, "high": 11,
                                "low": 9, "close": 10.5, "volume": 50,
                                "contract_month": "202606"})
        n = db.delete_realtime_bar("MES", "202606", "5min")
        self.assertEqual(n, 1)
        self.assertIsNone(db.get_realtime_bar("MES", "202606", "5min"))


# ── IB fetch cache (contract_token) ───────────────────────────────────────────


class TestIBFetchCache(_DbTestCase):
    def test_insert_with_token_kwarg(self):
        bars = [{"time": 1000, "open": 10, "high": 11, "low": 9,
                 "close": 10.5, "volume": 100}]
        n = db.insert_ib_cache_bars("MES", "5min", bars,
                                    contract_token="MONTH:202606")
        self.assertEqual(n, 1)
        rows = db.get_ib_cache_bars("MES", "5min",
                                     contract_token="MONTH:202606")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["contract_token"], "MONTH:202606")

    def test_insert_with_per_bar_token(self):
        bars = [{"time": 1000, "open": 10, "high": 11, "low": 9,
                 "close": 10.5, "volume": 100,
                 "contract_token": "CONT"}]
        n = db.insert_ib_cache_bars("MES", "5min", bars)
        self.assertEqual(n, 1)
        rows = db.get_ib_cache_bars("MES", "5min", contract_token="CONT")
        self.assertEqual(len(rows), 1)

    def test_reject_missing_token(self):
        bars = [{"time": 1000, "open": 10, "high": 11, "low": 9,
                 "close": 10.5, "volume": 100}]
        n = db.insert_ib_cache_bars("MES", "5min", bars)
        self.assertEqual(n, 0)

    def test_month_and_cont_coexist_at_same_ts(self):
        # Same ts, different tokens — both must be present
        db.insert_ib_cache_bars("MES", "5min",
            [{"time": 1000, "open": 10, "high": 11, "low": 9,
              "close": 10.5, "volume": 100}],
            contract_token="MONTH:202606")
        db.insert_ib_cache_bars("MES", "5min",
            [{"time": 1000, "open": 9.5, "high": 10.5, "low": 8.5,
              "close": 10.0, "volume": 80}],
            contract_token="CONT")
        all_rows = db.get_ib_cache_bars("MES", "5min")
        self.assertEqual(len(all_rows), 2)
        tokens = {r["contract_token"] for r in all_rows}
        self.assertEqual(tokens, {"MONTH:202606", "CONT"})

    def test_delete_by_token(self):
        db.insert_ib_cache_bars("MES", "5min",
            [{"time": 1000, "open": 10, "high": 11, "low": 9,
              "close": 10.5, "volume": 100}],
            contract_token="MONTH:202606")
        db.insert_ib_cache_bars("MES", "5min",
            [{"time": 1000, "open": 9.5, "high": 10.5, "low": 8.5,
              "close": 10.0, "volume": 80}],
            contract_token="CONT")
        n = db.delete_ib_cache_bars("MES", "5min", 0, db.MAX_TIMESTAMP,
                                     contract_token="CONT")
        self.assertEqual(n, 1)
        rows = db.get_ib_cache_bars("MES", "5min")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["contract_token"], "MONTH:202606")

    def test_coverage_filtered_by_token(self):
        db.insert_ib_cache_bars("MES", "5min",
            [{"time": 1000, "open": 10, "high": 11, "low": 9,
              "close": 10.5, "volume": 100},
             {"time": 1300, "open": 10, "high": 11, "low": 9,
              "close": 10.5, "volume": 100}],
            contract_token="MONTH:202606")
        db.insert_ib_cache_bars("MES", "5min",
            [{"time": 1300, "open": 10, "high": 11, "low": 9,
              "close": 10.5, "volume": 100}],
            contract_token="CONT")
        cov_month = db.get_ib_cache_coverage("MES", "5min", 0, 9999,
                                              contract_token="MONTH:202606")
        cov_cont = db.get_ib_cache_coverage("MES", "5min", 0, 9999,
                                             contract_token="CONT")
        self.assertEqual(cov_month, [1000, 1300])
        self.assertEqual(cov_cont, [1300])


# ── validated_ranges (per-contract) ───────────────────────────────────────────


class TestValidatedRanges(_DbTestCase):
    def test_per_contract_independence(self):
        db.insert_validated_range("MES", "5min", 1000, 2000,
                                   contract_month="202603")
        db.insert_validated_range("MES", "5min", 1000, 2000,
                                   contract_month="202606", mismatches=2)
        rows = db.get_validated_ranges("MES", "5min")
        self.assertEqual(len(rows), 2)

        self.assertTrue(db.is_range_validated("MES", "5min", 1000, 2000,
                                               contract_month="202603"))
        # 202606 has mismatches > 0 → not clean
        self.assertFalse(db.is_range_validated("MES", "5min", 1000, 2000,
                                                contract_month="202606"))

    def test_unchecked_ranges_per_contract(self):
        db.insert_validated_range("MES", "5min", 1000, 2000,
                                   contract_month="202606")
        # 202609 has no validated range → entire window unchecked
        unchecked = db.get_unchecked_ranges("MES", "5min", 500, 2500,
                                             contract_month="202609")
        self.assertEqual(unchecked, [{"from_ts": 500, "to_ts": 2500}])

        # 202606 has [1000,2000] clean → unchecked is [500,999]+[2001,2500]
        unchecked = db.get_unchecked_ranges("MES", "5min", 500, 2500,
                                             contract_month="202606")
        self.assertEqual(len(unchecked), 2)


# ── Distinct contracts ────────────────────────────────────────────────────────


class TestDistinctContracts(_DbTestCase):
    def test_distinct_contract_months(self):
        db.insert_bars("MES", "5min",
                       [_bar(1000, 10, 11, 9, 10.5, 100, "202603", "ib_monthly")])
        db.insert_bars("MES", "5min",
                       [_bar(2000, 10, 11, 9, 10.5, 100, "202606", "ib_monthly")])
        db.insert_bars("MES", "5min",
                       [_bar(3000, 10, 11, 9, 10.5, 100, "202606", "ib_monthly")])
        cms = db.get_distinct_contract_months("MES", "5min")
        self.assertEqual(cms, ["202603", "202606"])


if __name__ == "__main__":
    unittest.main()
