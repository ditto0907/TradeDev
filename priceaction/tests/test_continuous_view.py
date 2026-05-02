"""
Unit tests for :mod:`continuous_view` — front/ratio/difference assembly.

Constructs a known per-contract dataset, drives ``assemble_continuous``,
and verifies:
  * front-month selection discards rollover overlap
  * cont_ratio scales earlier contracts so the rollover-day close matches
  * cont_difference shifts earlier contracts by the same boundary delta
  * parse_token handles every documented form
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import continuous_view as cv  # noqa: E402


def _b(ts, c, cm, source="ib_monthly"):
    return {"time": ts, "open": c, "high": c + 1,
            "low": c - 1, "close": c, "volume": 100,
            "contract_month": cm, "source": source}


class _DBBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db._set_db_path_for_testing(Path(self._tmp.name) / "t.db")
        db.init_db()

    def tearDown(self):
        db._set_db_path_for_testing(Path(self._tmp.name) / "noop.db")
        self._tmp.cleanup()


class TestParseToken(unittest.TestCase):
    def test_bare_symbol(self):
        r = cv.parse_token("MES")
        self.assertEqual(r["kind"], "continuous")
        self.assertEqual(r["method"], "front")

    def test_cont_front(self):
        r = cv.parse_token("MES@CONT_FRONT")
        self.assertEqual(r["method"], "front")

    def test_cont_ratio(self):
        r = cv.parse_token("MES@CONT_RATIO")
        self.assertEqual(r["method"], "cont_ratio")

    def test_cont_diff_alias(self):
        self.assertEqual(cv.parse_token("MES@CONT_DIFF")["method"],
                         "cont_difference")
        self.assertEqual(cv.parse_token("MES@CONT_DIFFERENCE")["method"],
                         "cont_difference")

    def test_month(self):
        r = cv.parse_token("MES@202606")
        self.assertEqual(r["kind"], "month")
        self.assertEqual(r["contract_month"], "202606")

    def test_invalid(self):
        with self.assertRaises(ValueError):
            cv.parse_token("MES@WHATEVER")
        with self.assertRaises(ValueError):
            cv.parse_token("")


class TestAssembleContinuous(_DBBase):
    def setUp(self):
        super().setUp()
        # Use a real MES rollover (2026 Mar→Jun); active_contract returns
        # '202603' before the rollover date and '202606' afterward.
        # Pick timestamps that straddle the actual rollover date so the
        # front-month selection is deterministic.
        # CME equity-index rollover for MES Jun 2026 is approximately
        # 2026-03-12 (8th business day of March).  Use a couple of
        # daily bars before and after.
        # Convert local-NY date to UTC ts at midnight UTC for simplicity.
        from datetime import datetime, timezone
        def _ts(y, m, d):
            return int(datetime(y, m, d, 18, tzinfo=timezone.utc).timestamp())

        # ── 202603 contract: bars before and across rollover ─────────
        # Real prices around 5500.
        self.bars_h = [
            _b(_ts(2026, 3, 1),  5400, "202603"),
            _b(_ts(2026, 3, 5),  5450, "202603"),
            _b(_ts(2026, 3, 11), 5500, "202603"),
            _b(_ts(2026, 3, 12), 5520, "202603"),  # last day before/at rollover
            _b(_ts(2026, 3, 13), 5530, "202603"),  # may or may not be front
        ]
        # ── 202606 contract: starts ~10 days before rollover, continues
        # for weeks afterward.  Reflect the typical contango: 202606 trades
        # ~30pts above 202603 around rollover.
        self.bars_m = [
            _b(_ts(2026, 3, 5),  5485, "202606"),  # overlap before rollover
            _b(_ts(2026, 3, 11), 5535, "202606"),
            _b(_ts(2026, 3, 12), 5555, "202606"),  # rollover boundary
            _b(_ts(2026, 3, 13), 5565, "202606"),
            _b(_ts(2026, 3, 16), 5580, "202606"),
            _b(_ts(2026, 3, 20), 5600, "202606"),
        ]
        db.insert_bars("MES", "1D", self.bars_h)
        db.insert_bars("MES", "1D", self.bars_m)

        self.from_ts = _ts(2026, 3, 1)
        self.to_ts = _ts(2026, 3, 21)

    def test_front_no_overlap(self):
        bars = cv.assemble_continuous("MES", "1D",
                                       self.from_ts, self.to_ts,
                                       method="front")
        # No two bars share a timestamp → overlap discarded.
        ts_list = [b["time"] for b in bars]
        self.assertEqual(len(ts_list), len(set(ts_list)),
                         f"duplicate timestamps in front output: {ts_list}")
        # Bars must be sorted ascending
        self.assertEqual(ts_list, sorted(ts_list))
        # Both contracts contributed at least one bar
        cms = {b["contract_month"] for b in bars}
        self.assertEqual(cms, {"202603", "202606"})

    def test_front_keeps_real_prices(self):
        bars = cv.assemble_continuous("MES", "1D",
                                       self.from_ts, self.to_ts,
                                       method="front")
        # Find one front-202603 bar and verify the close matches input
        for b in bars:
            if b["contract_month"] == "202603":
                # find original
                src = next(s for s in self.bars_h if s["time"] == b["time"])
                self.assertEqual(b["close"], src["close"])
                break
        else:
            self.fail("no 202603 front bar found")

    def test_ratio_adjusts_old_contract(self):
        bars = cv.assemble_continuous("MES", "1D",
                                       self.from_ts, self.to_ts,
                                       method="cont_ratio")
        # All 202606-front bars should be unchanged (latest contract = factor 1)
        for b in bars:
            if b["contract_month"] == "202606":
                src = next(s for s in self.bars_m if s["time"] == b["time"])
                self.assertAlmostEqual(b["close"], src["close"], places=4)

        # 202603-front bars should be scaled by ratio.  The boundary close is
        # the 202603 bar at or before the first 202606 front-bar timestamp,
        # and the 202606 close at that ts.  Build the same logic in tests.
        from_h = [b for b in bars if b["contract_month"] == "202603"]
        self.assertGreater(len(from_h), 0, "no 202603 front bars")

        # All adjusted 202603 closes should be > original (because 202606 > 202603)
        for b in from_h:
            src = next(s for s in self.bars_h if s["time"] == b["time"])
            self.assertGreater(b["close"], src["close"],
                               f"ratio adjustment did not lift 202603 close")

    def test_difference_adjusts_old_contract(self):
        bars = cv.assemble_continuous("MES", "1D",
                                       self.from_ts, self.to_ts,
                                       method="cont_difference")
        # 202606-front bars unchanged
        for b in bars:
            if b["contract_month"] == "202606":
                src = next(s for s in self.bars_m if s["time"] == b["time"])
                self.assertAlmostEqual(b["close"], src["close"], places=4)
        # 202603-front bars shifted up by a constant (positive)
        diffs = []
        for b in bars:
            if b["contract_month"] == "202603":
                src = next(s for s in self.bars_h if s["time"] == b["time"])
                diffs.append(b["close"] - src["close"])
        self.assertGreater(len(diffs), 0)
        # All diffs equal (same offset applied to every old-contract bar)
        for d in diffs[1:]:
            self.assertAlmostEqual(d, diffs[0], places=4)
        self.assertGreater(diffs[0], 0,
                           "difference adjustment should be positive (contango)")

    def test_invalid_method(self):
        with self.assertRaises(ValueError):
            cv.assemble_continuous("MES", "1D", self.from_ts, self.to_ts,
                                    method="bogus")  # type: ignore[arg-type]

    def test_empty_window(self):
        # No bars in 2099 → returns []
        from datetime import datetime, timezone
        far_from = int(datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp())
        far_to = far_from + 86400
        self.assertEqual(
            cv.assemble_continuous("MES", "1D", far_from, far_to),
            [],
        )


if __name__ == "__main__":
    unittest.main()
