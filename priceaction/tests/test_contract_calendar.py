"""
Unit tests for :mod:`contract_calendar`.

Run with::

    python -m pytest priceaction/tests/test_contract_calendar.py

or directly::

    python priceaction/tests/test_contract_calendar.py
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime, timezone

# Make ``priceaction/`` importable when running this file directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import contract_calendar as cc  # noqa: E402


def _ts(y, m, d, hh=12, mm=0):
    return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp())


class TestRolloverDates(unittest.TestCase):
    """CME / COMEX / OSE published rollover dates for reference years."""

    def test_mes_2024_quarterly_rolls(self):
        # Published CME "Quarterly Roll" dates for 2024.
        self.assertEqual(cc.rollover_date("MES", 2024, 3), date(2024, 3, 12))
        self.assertEqual(cc.rollover_date("MES", 2024, 6), date(2024, 6, 12))
        self.assertEqual(cc.rollover_date("MES", 2024, 9), date(2024, 9, 12))
        self.assertEqual(cc.rollover_date("MES", 2024, 12), date(2024, 12, 11))

    def test_mnq_follows_mes(self):
        # Same CME equity-index convention.
        for q in (3, 6, 9, 12):
            self.assertEqual(
                cc.rollover_date("MNQ", 2024, q),
                cc.rollover_date("MES", 2024, q),
            )

    def test_mgc_2024_bimonthly_rolls_are_business_days(self):
        for q in (2, 4, 6, 8, 10, 12):
            rd = cc.rollover_date("MGC", 2024, q)
            # Must be a weekday and in the correct month.
            self.assertLess(rd.weekday(), 5)
            self.assertEqual(rd.month, q)
            self.assertEqual(rd.year, 2024)

    def test_nk225mc_rolls_one_bday_before_second_friday(self):
        # Second Friday of Oct 2024 is 2024-10-11; rollover = Thu 10-10.
        self.assertEqual(cc.rollover_date("NK225MC", 2024, 10), date(2024, 10, 10))
        # Second Friday of Apr 2024 is 2024-04-12; rollover = Thu 04-11.
        self.assertEqual(cc.rollover_date("NK225MC", 2024, 4), date(2024, 4, 11))


class TestActiveContract(unittest.TestCase):

    def test_mes_before_and_after_march_rollover(self):
        # Rollover is Tue 2024-03-12 (local ET).  Use noon UTC to stay on
        # the target date regardless of DST shift.
        self.assertEqual(cc.active_contract(_ts(2024, 3, 11, 16), "MES"), "202403")
        self.assertEqual(cc.active_contract(_ts(2024, 3, 12, 16), "MES"), "202406")
        self.assertEqual(cc.active_contract(_ts(2024, 3, 13, 16), "MES"), "202406")

    def test_mes_january_points_to_march(self):
        # The full Jan+Feb window and up to the roll date should be March.
        self.assertEqual(cc.active_contract(_ts(2024, 1, 1), "MES"), "202403")
        self.assertEqual(cc.active_contract(_ts(2024, 2, 29), "MES"), "202403")

    def test_mes_year_end_wrap(self):
        # After Dec rollover, front month is March of next year.
        self.assertEqual(cc.active_contract(_ts(2024, 12, 20), "MES"), "202503")

    def test_mgc_bimonthly_cycle(self):
        # Mid-January belongs to the February contract (next listed month).
        self.assertEqual(cc.active_contract(_ts(2024, 1, 15), "MGC"), "202402")
        # Mid-May belongs to the June contract.
        self.assertEqual(cc.active_contract(_ts(2024, 5, 15), "MGC"), "202406")

    def test_nk225mc_monthly_cycle(self):
        # Before the 2nd-Friday roll (2024-04-11) we're still on April.
        self.assertEqual(cc.active_contract(_ts(2024, 4, 10), "NK225MC"), "202404")
        # On/after the roll, front month is May.
        self.assertEqual(cc.active_contract(_ts(2024, 4, 11), "NK225MC"), "202405")

    def test_no_day10_heuristic(self):
        # Regression: the old code flipped contracts whenever "day <= 10".
        # MES 2024-06-10 is BEFORE the real roll (2024-06-12), so it must
        # still be the June contract, NOT September.
        self.assertEqual(cc.active_contract(_ts(2024, 6, 10), "MES"), "202406")
        self.assertEqual(cc.active_contract(_ts(2024, 9, 10), "MES"), "202409")


class TestNeighborContracts(unittest.TestCase):

    def test_mes_neighbors(self):
        self.assertEqual(
            cc.neighbor_contracts("MES", "202406"),
            ["202403", "202406", "202409"],
        )

    def test_mes_wrap_across_year(self):
        self.assertEqual(
            cc.neighbor_contracts("MES", "202412"),
            ["202409", "202412", "202503"],
        )
        self.assertEqual(
            cc.neighbor_contracts("MES", "202403"),
            ["202312", "202403", "202406"],
        )

    def test_non_cycle_month_gracefully_handled(self):
        # MES cycle has no month 7 — neighbors should clamp to nearest.
        n = cc.neighbor_contracts("MES", "202407")
        self.assertIn("202407", n)
        self.assertIn("202406", n)
        self.assertIn("202409", n)


if __name__ == "__main__":
    unittest.main()
