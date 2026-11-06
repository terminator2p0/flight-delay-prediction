"""Signed arrival timing, measured outcomes, and distinct comparison periods."""

from datetime import timedelta
import unittest

from delay_aware.aeroapi import normalize, recent_summary
from delay_aware.assessment import traveler_view
from delay_aware.history import FlightHistory
from delay_aware.timing import arrival_timing
from test_aeroapi import NOW, SELECTED, operation


class TimingTests(unittest.TestCase):
    def test_early_on_time_minor_and_15_minute_boundary(self):
        stats = arrival_timing([-30, -10, 0, 14, 15, 101])
        self.assertEqual(stats["mean_minutes"], 15)
        self.assertEqual(stats["median_minutes"], 7)
        self.assertEqual([stats[k] for k in ("early_arrivals", "on_time_arrivals",
                         "under_15_minutes_late", "at_least_15_minutes_late")], [2, 1, 1, 2])
        self.assertEqual(arrival_timing([-20, -10])["mean_minutes"], -15)
        self.assertEqual(arrival_timing([-20, -10, 60])["median_minutes"], -10)

    def test_empty_arrivals_do_not_imply_on_time_performance(self):
        stats = arrival_timing([])
        self.assertEqual(stats["measured_arrivals"], 0)
        self.assertIsNone(stats["mean_minutes"])
        self.assertIsNone(stats["median_minutes"])

    def test_historical_timings_exclude_cancelled_diverted_and_missing(self):
        rows = [("2026-01-01", 8, delay, cancelled, diverted, "DL")
                for delay, cancelled, diverted in [(-30, 0, 0), (-10, 0, 0), (10, 0, 0),
                                                   (None, 0, 0), (-120, 1, 0), (200, 0, 1)]]
        stats = FlightHistory.summarize(rows)
        self.assertEqual(stats["arrival_timing"]["mean_minutes"], -10)
        self.assertEqual(stats["arrival_timing"]["median_minutes"], -10)
        self.assertEqual(stats["arrival_timing"]["measured_arrivals"], 3)
        self.assertAlmostEqual(stats["mean_arrival_lateness_minutes"], 10 / 3)

    def recent(self):
        records = []
        for day, delay in enumerate([-30, -20, -10, 0, 14, 15], 1):
            scheduled = NOW - timedelta(days=day)
            records.append(normalize(operation(fa_flight_id=f"past-{day}", scheduled_out=scheduled.isoformat(),
                scheduled_in=(scheduled + timedelta(hours=1)).isoformat(),
                actual_in=(scheduled + timedelta(hours=1, minutes=delay)).isoformat())))
        for flag in ({"cancelled": True}, {"diverted": True}, {"actual_in": None}):
            records.append(normalize(operation(scheduled_out="2026-10-23T12:00:00Z",
                scheduled_in="2026-10-23T14:00:00Z", actual_in="2026-10-23T13:00:00Z",
                estimated_in="2026-10-23T13:00:00Z") | flag))
        return recent_summary(list(reversed(records)), NOW)["groups"][0]

    def test_recent_keeps_signed_gate_times_and_only_latest_five_actuals(self):
        group = self.recent()
        timing = group["arrival_timing"]
        self.assertEqual(timing["measured_arrivals"], 6)
        self.assertAlmostEqual(timing["mean_minutes"], -31 / 6)
        self.assertEqual(timing["median_minutes"], -5)
        self.assertEqual(timing["early_arrivals"], 3)
        self.assertEqual([r["minutes"] for r in group["latest_arrivals"]], [-30, -20, -10, 0, 14])
        self.assertTrue(group["first_measured_departure"].startswith("2026-10-17"))
        self.assertTrue(group["last_measured_departure"].startswith("2026-10-22"))

    def test_summary_compares_distinct_periods_and_retains_early_arrivals(self):
        history = FlightHistory.summarize([("2026-01-01", 8, 30, 0, 0, "YX")])
        result = traveler_view(SELECTED, {}, {"selected_flight_verified": True,
            "selected": {"operating_ident": "YX4321"}, "recent": {"groups": [self.recent()]}}, {"overall": history})
        self.assertEqual(result["performance"]["arrival_timing"]["mean_minutes"], 30)
        self.assertLess(result["recent"]["arrival_timing"]["mean_minutes"], 0)
        self.assertIn("Historical average arrival: 30 min late", result["summary_text"])
        self.assertIn("Recent average arrival: 5 min early", result["summary_text"])
        self.assertIn("3 of 6 arrived early", result["summary_text"])
