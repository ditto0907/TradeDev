"""Tests for strategy.signal_detector — pure-function unit tests."""
import sys
import os
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from strategy.signal_detector import (  # noqa: E402
    detect_signals,
    SignalRecord,
    _coh_l,
    _overlap_pct,
    _classify_overlap,
    _bar_cnt_for_ts,
    _signal_direction,
    _ft_classify,
    _classify_gap,
    STRONG_THRESHOLD,
)

ET = ZoneInfo("America/New_York")


def ts_at(date_str: str, hour: int, minute: int) -> int:
    """Build a Unix ts for a given ET clock time."""
    y, m, d = map(int, date_str.split("-"))
    dt = datetime(y, m, d, hour, minute, tzinfo=ET)
    return int(dt.timestamp())


def bar(ts, o, h, l, c, v=100):
    return {"time": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


# ─── Helpers ───────────────────────────────────────────────────────────────

class TestCohL(unittest.TestCase):
    def test_strong_bull(self):
        # Body in top: o=10, l=9, h=11, c=10.95
        b = bar(0, 10, 11, 9, 10.95)
        coh = _coh_l(b, "Bull")
        self.assertGreater(coh, STRONG_THRESHOLD)  # close near high

    def test_weak_bull(self):
        # Body small in middle
        b = bar(0, 10, 11, 9, 10.05)
        coh = _coh_l(b, "Bull")
        self.assertLess(coh, STRONG_THRESHOLD)

    def test_strong_bear(self):
        b = bar(0, 11, 11, 9, 9.05)  # close near low
        coh = _coh_l(b, "Bear")
        self.assertGreater(coh, STRONG_THRESHOLD)

    def test_doji_zero_range(self):
        b = bar(0, 10, 10, 10, 10)
        self.assertEqual(_coh_l(b, "Bull"), 0.5)


class TestOverlap(unittest.TestCase):
    def test_no_overlap(self):
        b1 = bar(0, 10, 11, 9, 10.5)
        b2 = bar(0, 12, 13, 11.5, 12.5)
        self.assertEqual(_overlap_pct(b1, b2), 0.0)
        self.assertEqual(_classify_overlap(0.0), "small")

    def test_full_overlap(self):
        b1 = bar(0, 10, 12, 8, 11)
        b2 = bar(0, 10, 12, 8, 11)
        self.assertEqual(_overlap_pct(b1, b2), 1.0)
        self.assertEqual(_classify_overlap(1.0), "large")

    def test_partial_overlap(self):
        # b1: [9, 11], b2: [10, 12] — overlap [10, 11] = 1, union = 3 → 0.333
        b1 = bar(0, 10, 11, 9, 10.5)
        b2 = bar(0, 10.5, 12, 10, 11.5)
        pct = _overlap_pct(b1, b2)
        self.assertAlmostEqual(pct, 1.0 / 3.0, places=2)
        self.assertEqual(_classify_overlap(pct), "medium")


class TestBarCnt(unittest.TestCase):
    def test_b1_at_open(self):
        ts = ts_at("2026-04-01", 9, 30)
        self.assertEqual(_bar_cnt_for_ts(ts), "B1")

    def test_b2_at_935(self):
        ts = ts_at("2026-04-01", 9, 35)
        self.assertEqual(_bar_cnt_for_ts(ts), "B2")

    def test_intra_bar_938_is_b2(self):
        ts = ts_at("2026-04-01", 9, 38)
        self.assertEqual(_bar_cnt_for_ts(ts), "B2")

    def test_b7_at_10(self):
        ts = ts_at("2026-04-01", 10, 0)
        self.assertEqual(_bar_cnt_for_ts(ts), "B7")


class TestGap(unittest.TestCase):
    def test_gap_up(self):
        self.assertEqual(_classify_gap(100.5, 100.0), "GapUp")

    def test_gap_down(self):
        self.assertEqual(_classify_gap(99.5, 100.0), "GapDown")

    def test_no_gap(self):
        self.assertEqual(_classify_gap(100.001, 100.0), "None")

    def test_no_prev_close(self):
        self.assertEqual(_classify_gap(100, None), "None")


class TestSignalDirection(unittest.TestCase):
    def test_bull_pair(self):
        b1 = bar(0, 10, 11, 9.5, 10.8)
        b2 = bar(0, 10.8, 12, 10.5, 11.8)
        self.assertEqual(_signal_direction(b1, b2), "Bull")

    def test_bear_pair(self):
        b1 = bar(0, 11, 11.2, 10, 10.2)
        b2 = bar(0, 10.2, 10.5, 9, 9.2)
        self.assertEqual(_signal_direction(b1, b2), "Bear")

    def test_doji_breaks_chain(self):
        b1 = bar(0, 10, 11, 9, 10.5)
        b2 = bar(0, 10.5, 11, 10, 10.5)  # doji
        self.assertIsNone(_signal_direction(b1, b2))

    def test_mixed_direction(self):
        b1 = bar(0, 10, 11, 9, 10.5)
        b2 = bar(0, 10.5, 11, 9.5, 10)  # bear
        self.assertIsNone(_signal_direction(b1, b2))


class TestFT(unittest.TestCase):
    def test_immediate_y(self):
        b1 = bar(0, 10, 11, 9, 10.8)
        ft = _ft_classify("Bull", [b1])
        self.assertEqual(ft, "Y")

    def test_2nd_bar(self):
        opp = bar(0, 11, 11, 10.5, 10.8)  # bear
        same = bar(0, 10.8, 11.5, 10.5, 11.3)  # bull
        ft = _ft_classify("Bull", [opp, same])
        self.assertEqual(ft, "2nd")

    def test_no_ft(self):
        opp1 = bar(0, 11, 11, 10.5, 10.8)
        opp2 = bar(0, 10.8, 10.9, 10, 10.2)  # both bear
        ft = _ft_classify("Bull", [opp1, opp2])
        self.assertEqual(ft, "N")

    def test_empty_after(self):
        self.assertEqual(_ft_classify("Bull", []), "N")


# ─── Integration: detect_signals ───────────────────────────────────────────

class TestDetectSignals(unittest.TestCase):
    def setUp(self):
        # Build a synthetic 2026-04-01 RTH session
        self.day = "2026-04-01"

    def test_simple_bull_signal_with_ft(self):
        # Bars: B1 doji, B2 strong bull, B3 strong bull (signal=B3), B4 bull (FT=Y)
        bars = [
            bar(ts_at(self.day, 9, 30), 100, 100.5, 99.8, 100.0),  # doji B1
            bar(ts_at(self.day, 9, 35), 100, 101, 99.9, 100.95),   # strong bull B2
            bar(ts_at(self.day, 9, 40), 100.95, 102, 100.9, 101.95),  # strong bull B3
            bar(ts_at(self.day, 9, 45), 101.95, 103, 101.9, 102.95),  # bull B4
        ]
        sigs = detect_signals(bars)
        # B2-B3 forms a bull pair → signal at B3
        self.assertEqual(len(sigs), 2)  # B3 and B4 both have prior bull bar → both signals
        s = sigs[0]
        self.assertEqual(s.direction, "Bull")
        self.assertEqual(s.bar_cnt, "B3")
        self.assertEqual(s.signal_strength, "Strong")
        self.assertEqual(s.ft, "Y")  # B4 is bull

    def test_bear_signal_with_2nd_ft(self):
        # B1 bear, B2 bear (signal), B3 doji, B4 bear → FT=2nd
        bars = [
            bar(ts_at(self.day, 9, 30), 100, 100.1, 98.9, 99.0),    # bear B1
            bar(ts_at(self.day, 9, 35), 99, 99.1, 97.9, 98.0),      # bear B2 (signal)
            bar(ts_at(self.day, 9, 40), 98, 98.5, 97.5, 98.0),      # doji B3
            bar(ts_at(self.day, 9, 45), 98, 98.1, 96.9, 97.0),      # bear B4
        ]
        sigs = detect_signals(bars)
        self.assertEqual(len(sigs), 1)
        s = sigs[0]
        self.assertEqual(s.direction, "Bear")
        self.assertEqual(s.bar_cnt, "B2")
        self.assertEqual(s.ft, "2nd")

    def test_no_signal_when_alternating(self):
        bars = [
            bar(ts_at(self.day, 9, 30), 100, 101, 99, 100.8),   # bull
            bar(ts_at(self.day, 9, 35), 100.8, 101, 99.5, 99.8),  # bear
            bar(ts_at(self.day, 9, 40), 99.8, 100.5, 99, 100.3),  # bull
        ]
        sigs = detect_signals(bars)
        self.assertEqual(len(sigs), 0)

    def test_gap_up_detection(self):
        prev_close = 99.0
        bars = [
            bar(ts_at(self.day, 9, 30), 100.0, 101, 99.9, 100.8),  # bull (gap up)
            bar(ts_at(self.day, 9, 35), 100.8, 101.5, 100.5, 101.4),
        ]
        sigs = detect_signals(bars, prev_day_close=prev_close)
        self.assertEqual(len(sigs), 1)
        self.assertEqual(sigs[0].gap, "GapUp")

    def test_overlap_classification(self):
        # Two bars with ~50% overlap
        bars = [
            bar(ts_at(self.day, 9, 30), 100, 102, 99, 101.5),
            bar(ts_at(self.day, 9, 35), 101.5, 103, 100.5, 102.5),
        ]
        sigs = detect_signals(bars)
        self.assertEqual(len(sigs), 1)
        self.assertIn(sigs[0].overlapping, ("small", "medium", "large"))

    def test_pullback_count(self):
        # 2 bear bars (PB), then 2 bull bars (signal)
        bars = [
            bar(ts_at(self.day, 9, 30), 102, 102, 100.5, 101),    # bear
            bar(ts_at(self.day, 9, 35), 101, 101.2, 99.5, 100),   # bear
            bar(ts_at(self.day, 9, 40), 100, 101.5, 99.9, 101.3), # bull
            bar(ts_at(self.day, 9, 45), 101.3, 102.5, 101, 102.3),  # bull (signal)
        ]
        sigs = detect_signals(bars)
        self.assertGreaterEqual(len(sigs), 1)
        s = sigs[-1]
        self.assertEqual(s.direction, "Bull")
        self.assertEqual(s.pb_bars, 2)

    def test_no_cross_day_signal(self):
        # Last bar of day1 + first bar of day2 should NOT form a signal
        bars = [
            bar(ts_at("2026-04-01", 15, 55), 100, 101, 99.5, 100.8),  # bull day1
            bar(ts_at("2026-04-02", 9, 30), 100.8, 101.5, 100.5, 101.3),  # bull day2
        ]
        sigs = detect_signals(bars)
        self.assertEqual(len(sigs), 0)

    def test_empty_input(self):
        self.assertEqual(detect_signals([]), [])
        self.assertEqual(detect_signals([bar(0, 1, 2, 0, 1)]), [])

    def test_serialization(self):
        bars = [
            bar(ts_at(self.day, 9, 30), 100, 101, 99, 100.8),
            bar(ts_at(self.day, 9, 35), 100.8, 101.5, 100.5, 101.4),
        ]
        sigs = detect_signals(bars)
        self.assertTrue(all(isinstance(s, SignalRecord) for s in sigs))
        d = sigs[0].to_dict()
        self.assertIn("date", d)
        self.assertIn("direction", d)
        self.assertIn("coh_l", d)


if __name__ == "__main__":
    unittest.main()
