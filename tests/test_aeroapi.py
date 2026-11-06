"""Exact flight selection, source separation, credential handling, and bounded usage."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

from delay_aware.aeroapi import AeroAPI, normalize
from delay_aware.assessment import combine, traveler_view
from delay_aware.core import DataError, LocalStore, NoRedirect, ProviderError, Settings, Transport

NOW = datetime(2026, 10, 23, 16, tzinfo=timezone.utc)
SELECTED = {"flight_number": "DL 5652", "origin": "SDF", "destination": "LGA",
            "departure_local": "2026-10-24T08:00:00"}


def operation(**changes):
    return {"fa_flight_id": "RPA4321-1-airline-0", "ident_iata": "YX4321", "operator_iata": "YX",
            "flight_number": "4321", "codeshares_iata": ["DL5652"], "blocked": False,
            "position_only": False, "cancelled": False, "diverted": False, "status": "Scheduled",
            "origin": {"code_iata": "SDF", "timezone": "America/New_York"},
            "destination": {"code_iata": "LGA", "timezone": "America/New_York"},
            "scheduled_out": "2026-10-24T12:00:00Z", "scheduled_in": "2026-10-24T14:00:00Z",
            **changes}


class AeroAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = [1000000.0]
        self.store = LocalStore(self.root / "cache.sqlite", clock=lambda: self.clock[0])
        self.settings = Settings(self.root, aeroapi_key="private-key")
        self.transport = Mock()
        self.transport.get_json.return_value = {"flights": [operation()], "links": None}
        self.api = AeroAPI(self.settings, self.transport, self.store, clock=lambda: self.clock[0])

    def lookup(self, flight=None):
        return self.api.lookup(flight or SELECTED, now=NOW)

    def test_codeshare_resolves_different_operating_number_by_route_and_local_time(self):
        result = self.lookup()
        self.assertTrue(result["selected_flight_verified"])
        self.assertEqual(result["selected"]["operating_ident"], "YX4321")
        url, params = self.transport.get_json.call_args.args
        self.assertTrue(url.endswith("/DL5652"))
        self.assertEqual(params["max_pages"], 1)
        self.assertEqual(params["ident_type"], "designator")
        self.assertRegex(params["start"], r"T\d\d:\d\d:\d\dZ$")
        self.assertEqual(self.transport.get_json.call_args.kwargs["headers"], {"x-apikey": "private-key"})
        self.assertNotIn("private-key", url + json.dumps(params) + json.dumps(result))

    def test_wrong_carrier_same_number_route_date_time_or_airports_never_verify(self):
        records = [operation(ident_iata="AA5652", operator_iata="AA", flight_number="5652", codeshares_iata=[]),
                   operation(scheduled_out="2026-10-23T12:00:00Z"),
                   operation(scheduled_out="2026-10-24T13:00:00Z"),
                   operation(origin={"code_iata": "BOS", "timezone": "America/New_York"}),
                   operation(destination={"code_iata": "ORD", "timezone": "America/Chicago"})]
        self.transport.get_json.return_value["flights"] = records
        self.assertFalse(self.lookup()["selected_flight_verified"])

    def test_recent_operations_do_not_verify_future_departure(self):
        self.transport.get_json.return_value["flights"] = [operation(scheduled_out="2026-10-22T12:00:00Z",
            scheduled_in="2026-10-22T14:00:00Z", actual_in="2026-10-22T14:20:00Z", arrival_delay=120)]
        result = self.lookup()
        self.assertFalse(result["selected_flight_verified"])
        group = result["recent"]["groups"][0]
        self.assertEqual(group["mean_lateness_minutes"], 20)  # seconds field deliberately disagrees
        self.assertEqual(group["observed_delay_rate"], 1)

    def test_estimates_and_runway_times_are_not_measured_gate_arrivals(self):
        self.transport.get_json.return_value["flights"] = [operation(scheduled_out="2026-10-22T12:00:00Z",
            scheduled_in="2026-10-22T14:00:00Z", estimated_in="2026-10-22T14:30:00Z",
            actual_on="2026-10-22T14:20:00Z", arrival_delay=1800)]
        group = self.lookup()["recent"]["groups"][0]
        self.assertEqual(group["measured_gate_arrivals"], 0)
        self.assertIsNone(group["observed_delay_rate"])
        self.assertIsNone(normalize(operation(scheduled_out=None, scheduled_off="2026-10-24T12:00:00Z")))

    def test_duplicate_flight_ids_are_counted_once_and_conflicts_are_excluded(self):
        past = operation(scheduled_out="2026-10-22T12:00:00Z", scheduled_in="2026-10-22T14:00:00Z", actual_in="2026-10-22T14:20:00Z")
        self.transport.get_json.return_value["flights"] = [past, deepcopy(past)]
        self.assertEqual(self.lookup()["recent"]["groups"][0]["measured_gate_arrivals"], 1)
        self.api.cache.clear()
        self.transport.get_json.return_value["flights"][1]["actual_in"] = "2026-10-22T14:00:00Z"
        result = self.lookup()
        self.assertFalse(result["recent"]["available"])
        self.assertEqual(result["conflicting_records_excluded"], 1)

    def test_multiple_exact_departures_remain_ambiguous(self):
        self.transport.get_json.return_value["flights"].append(operation(fa_flight_id="different-id"))
        result = self.lookup()
        self.assertEqual(result["reason_code"], "ambiguous_departure")
        self.assertFalse(result["selected_flight_verified"])
        self.assertNotIn("selected", result)

    def test_same_instance_with_a_changed_schedule_is_not_counted_twice(self):
        original = operation(scheduled_out="2026-10-22T12:00:00Z", scheduled_in="2026-10-22T14:00:00Z", actual_in="2026-10-22T14:20:00Z")
        changed = {**original, "scheduled_out": "2026-10-22T12:30:00Z"}
        self.transport.get_json.return_value["flights"] = [original, changed]
        result = self.lookup()
        self.assertFalse(result["recent"]["available"])
        self.assertEqual(result["conflicting_records_excluded"], 1)

    def test_conflicting_operating_designators_are_not_guessed(self):
        self.transport.get_json.return_value["flights"] = [operation(ident_iata="DL5652")]
        self.assertFalse(self.lookup()["selected_flight_verified"])

    def test_bad_provider_fields_are_skipped_without_crashing(self):
        for raw in [None, {}, operation(origin=[]), operation(origin={"code_iata": 123}),
                    operation(origin={"code_iata": "SDF", "timezone": "bad/zone"}),
                    operation(scheduled_out="2026-10-24T08:00:00"), operation(position_only=True)]:
            with self.subTest(raw=raw):
                self.assertIsNone(normalize(raw))
        self.assertIsNotNone(normalize(operation(codeshares_iata=10)))

    def test_cache_reuses_request_for_other_dates_and_never_persists_flight_payloads(self):
        self.assertFalse(self.lookup()["cached"])
        self.assertTrue(self.lookup({**SELECTED, "departure_local": "2026-11-01T08:00:00"})["cached"])
        self.assertEqual(self.transport.get_json.call_count, 1)
        contents = (self.root / "cache.sqlite").read_bytes()
        self.assertNotIn(b"private-key", contents)
        self.assertNotIn(b"RPA4321", contents)
        self.clock[0] += 901
        self.assertFalse(self.lookup()["cached"])
        self.assertEqual(self.transport.get_json.call_count, 2)

    def test_page_limit_is_visible_and_next_links_are_never_followed(self):
        self.transport.get_json.return_value["links"] = {"next": "https://other.example/private-key"}
        result = self.lookup()
        self.assertTrue(result["partial_results"])
        self.transport.get_json.assert_called_once()
        self.assertNotIn("other.example", json.dumps(result))

    def test_missing_key_provider_failures_and_request_caps_preserve_history_access(self):
        no_key = AeroAPI(Settings(self.root), self.transport, self.store)
        self.assertEqual(no_key.lookup(SELECTED)["reason_code"], "not_configured")
        self.transport.get_json.assert_not_called()
        self.transport.get_json.side_effect = ProviderError(401)
        result = self.lookup()
        self.assertEqual(result["reason_code"], "provider_error")
        self.assertIn("BTS history remains available", result["reason"])
        self.assertTrue(self.lookup()["cached"])
        self.transport.get_json.assert_called_once()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM aeroapi_requests").fetchone()[0], 1)
        with self.assertRaises(DataError):
            self.store.reserve_aeroapi(1)

    def test_minute_limit_is_independent_of_serpapi_and_never_exceeds_six(self):
        for _ in range(6):
            self.store.reserve_aeroapi(100)
        with self.assertRaises(DataError):
            self.store.reserve_aeroapi(100)
        self.store.reserve_search(250)
        self.clock[0] += 61
        self.store.reserve_aeroapi(100)

    def test_cancellation_flag_is_kept_separate_from_confirmed_cancellations(self):
        self.transport.get_json.return_value["flights"] = [operation(cancelled=True, scheduled_out="2026-10-22T12:00:00Z",
            scheduled_in="2026-10-22T14:00:00Z", actual_in="2026-10-22T14:20:00Z")]
        recent = self.lookup()["recent"]
        self.assertEqual(recent["groups"][0]["cancelled_or_untracked"], 1)
        self.assertEqual(recent["groups"][0]["measured_gate_arrivals"], 0)
        self.assertIn("not a confirmed airline cancellation", recent["notice"])

    def test_credentials_stay_in_headers_and_redirects_are_blocked(self):
        opener = Mock()
        opener.open.side_effect = HTTPError("https://example.com", 401, "private-key", {}, None)
        with patch("delay_aware.core.build_opener", return_value=opener):
            with self.assertRaises(DataError) as failure:
                Transport("test").get_json("https://example.com", headers={"x-apikey": "private-key"})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("X-apikey"), "private-key")
        self.assertNotIn("private-key", str(failure.exception))
        self.assertIsNone(NoRedirect().redirect_request(Request("https://example.com"), None, 302, "", {}, "https://other.example"))
        self.assertNotIn("private-key", repr(self.settings))


class AssessmentTests(unittest.TestCase):
    def test_traveler_summary_keeps_periods_distinct_and_omits_provider_names(self):
        stats = {"scheduled_flights": 100, "completed_with_arrival_delay": 90,
                 "delayed_arrivals_15_minutes_or_more": 9, "historical_delay_rate": 0.1,
                 "mean_arrival_lateness_minutes": 7, "cancelled_flights": 5}
        live = {"selected_flight_verified": True, "selected": {"operating_ident": "YX4321", "status": "Scheduled",
                "estimated_arrival_delay_minutes": -4}, "recent": {"groups": [{"operating_ident": "YX4321",
                "measured_gate_arrivals": 3, "arrived_15_minutes_or_more_late": 2}]}}
        result = traveler_view(SELECTED, {}, live, {"overall": stats})
        self.assertEqual(result["performance"]["delay_rate"], 0.1)
        self.assertEqual(result["performance"]["cancellation_rate"], 0.05)
        self.assertEqual(result["recent"]["measured_gate_arrivals"], 3)
        self.assertEqual(result["arrival_message"], "Arrival expected 4 min early")
        self.assertNotIn("BTS", result["summary_text"])
        self.assertNotIn("FlightAware", result["summary_text"])

    def test_unverified_numeric_history_is_not_presented_as_selected_flight_reliability(self):
        result = traveler_view(SELECTED, {"available": True, "overall": {"scheduled_flights": 100}},
                               {"selected_flight_verified": False, "recent": {"groups": []}}, None)
        self.assertIsNone(result["performance"])
        self.assertEqual(result["departure"]["kind"], "Scheduled")
        self.assertEqual(result["status"], "Live status unavailable")

    def test_cancelled_or_untracked_flag_cannot_show_an_optimistic_arrival_estimate(self):
        result = traveler_view(SELECTED, {}, {"selected_flight_verified": True,
            "selected": {"operating_ident": "YX4321", "cancelled_or_untracked": True,
                "estimated_arrival_delay_minutes": -4}, "recent": {"groups": []}}, None)
        self.assertEqual(result["status"], "Check with airline")
        self.assertIsNone(result["arrival_message"])

    def test_verified_operator_selects_its_own_bts_group_without_pooling_recent_rates(self):
        stats = {"scheduled_flights": 100, "completed_with_arrival_delay": 90,
                 "delayed_arrivals_15_minutes_or_more": 9, "historical_delay_rate": 0.1}
        history = {"source": "BTS", "available": True, "historical_identity_verified": True,
                   "operating_services": [{"operating_carrier": "YX", "operating_flight_number": "4321",
                       "observed_first_date": "2025-01-01", "observed_last_date": "2026-08-31", "overall": stats}]}
        api = {"selected_flight_verified": True, "selected": {"operating_ident": "YX4321"}, "recent": {"groups": []}}
        store = Mock()
        result = combine(SELECTED, history, api, store, 24)
        self.assertEqual(result["selected_operator_history"]["overall"], stats)
        self.assertFalse(result["pooled_delay_rate_available"])
        store.report.assert_not_called()

    def test_changed_operator_does_not_inherit_old_market_service_rate(self):
        history = {"source": "BTS", "available": True, "historical_identity_verified": True,
                   "overall": {"scheduled_flights": 400}, "operating_services": [{"operating_carrier": "OO", "operating_flight_number": "9999"}]}
        api = {"selected_flight_verified": True, "selected": {"operating_ident": "YX4321"}, "recent": {"groups": []}}
        store = Mock()
        store.report.return_value = {"available": False}
        result = combine(SELECTED, history, api, store, 24)
        self.assertIsNone(result["selected_operator_history"])
        self.assertIn("differs", result["cautions"][0])
        self.assertNotIn("400", result["summary"])
        self.assertEqual(store.report.call_args.args[:2], ("YX", "4321"))
        self.assertTrue(store.report.call_args.kwargs["match_carrier"])

    def test_recent_only_cannot_verify_selected_identity(self):
        result = combine(SELECTED, {"available": False}, {"selected_flight_verified": False, "recent": {"groups": []}}, Mock(), 24)
        self.assertFalse(result["selected_flight_verified"])
        self.assertIsNone(result["selected_operator_history"])

    def test_current_identity_cannot_override_conflicting_historical_records(self):
        store = Mock()
        result = combine(SELECTED, {"available": False, "reason_code": "marketing_records_conflicted"},
                         {"selected_flight_verified": True, "selected": {"operating_ident": "YX4321"}, "recent": {"groups": []}}, store, 24)
        store.report.assert_not_called()
        self.assertIsNone(result["selected_operator_history"])

    def test_api_key_validation_and_caps_do_not_expose_credentials(self):
        for values in ({"AEROAPI_REQUEST_LIMIT": "101"}, {"AEROAPI_REQUEST_LIMIT": "bad"},
                       {"AEROAPI_API_KEY": "secret with spaces"}):
            with self.subTest(values=values), tempfile.TemporaryDirectory() as directory:
                with patch.dict("os.environ", values), self.assertRaises(DataError) as error:
                    Settings.load(Path(directory))
                self.assertNotIn("secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
