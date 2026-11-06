"""Offline checks for scope, quota protection, provenance, and honest statistics."""

import csv
from datetime import date, timedelta
from io import StringIO
import json
import math
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
import zipfile

from delay_aware.airports import Airports, EARTH_RADIUS_MILES
from delay_aware.core import DataError, LocalStore, Settings, Transport, read_env
from delay_aware.history import FlightHistory
from delay_aware.search import FlightSearch
from delay_aware.weather import Weather


AIRPORT_CSV = """iata_code,iso_country,latitude_deg,longitude_deg,name,ident,scheduled_service,type
BOS,US,42.3656,-71.0096,Logan,KBOS,yes,large_airport
ORD,US,41.9742,-87.9073,OHare,KORD,yes,large_airport
LHR,GB,51.47,-0.45,Heathrow,EGLL,yes,large_airport
"""


def offer(number="UA 123", *, origin="BOS", destination="ORD", departure="2026-10-25 08:00", connecting=False):
    leg = {"flight_number": number, "airline": "United",
           "departure_airport": {"id": origin, "time": departure},
           "arrival_airport": {"id": destination, "time": "2026-10-25 09:30"}, "duration": 150}
    return {"flights": [leg, leg] if connecting else [leg], "price": 200, "total_duration": 150}


class Setup(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)


class AirportTests(Setup):
    def write_airports(self, rows):
        path = self.root / "airports.csv"
        with path.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["iata_code", "iso_country", "latitude_deg", "longitude_deg", "name",
                             "ident", "scheduled_service", "type"])
            writer.writerows(rows)
        return Airports(path)

    def test_nearby_applies_unrounded_radius_and_excludes_non_passenger_and_foreign_airports(self):
        within = math.degrees(49.99 / EARTH_RADIUS_MILES)
        outside = math.degrees(50.01 / EARTH_RADIUS_MILES)
        airports = self.write_airports([
            ["AAA", "US", 0, 0, "Reference", "KAAA", "yes", "large_airport"],
            ["BBB", "US", 0, within, "Inside", "KBBB", "yes", "large_airport"],
            ["CCC", "US", 0, outside, "Outside", "KCCC", "yes", "large_airport"],
            ["DDD", "US", 0, 0.1, "Private", "KDDD", "no", "small_airport"],
            ["EEE", "GB", 0, 0.1, "Foreign", "EEEE", "yes", "large_airport"],
            ["FFF", "US", 0, 0.1, "Closed", "KFFF", "yes", "closed"],
            ["GGG", "US", 0, 0.1, "Closer", "KGGG", "yes", "large_airport"],
        ])
        result = airports.nearby(" aaa ")
        self.assertEqual([a["iata"] for a in result["nearby_airports"]], ["GGG", "BBB"])
        self.assertEqual(result["nearby_airports"][1]["distance_miles"], 50.0)
        self.assertEqual(result["distance_kind"], "straight_line")
        self.assertEqual(result["radius_miles"], 50)

    def test_nearby_handles_crossing_the_date_line(self):
        airports = self.write_airports([
            ["AAA", "US", 0, 179.9, "Reference", "KAAA", "yes", "large_airport"],
            ["BBB", "US", 0, -179.9, "Across date line", "KBBB", "yes", "large_airport"],
        ])
        self.assertAlmostEqual(airports.nearby("AAA")["nearby_airports"][0]["distance_miles"], 13.8, places=1)

    def test_nearby_can_be_empty_and_rejects_an_unsupported_reference_airport(self):
        airports = self.write_airports([
            ["AAA", "US", 0, 0, "Reference", "KAAA", "yes", "large_airport"],
        ])
        self.assertEqual(airports.nearby("AAA")["nearby_airports"], [])
        with self.assertRaises(DataError):
            airports.nearby("LHR")


class SearchTests(Setup):
    def setUp(self):
        super().setUp()
        path = self.root / "airports.csv"
        path.write_text(AIRPORT_CSV)
        self.airports = Airports(path)
        self.clock = [1_000_000.0]
        self.store = LocalStore(self.root / "cache.sqlite", clock=lambda: self.clock[0])
        self.transport = Mock()
        self.transport.get_json.return_value = {
            "search_metadata": {"status": "Success", "created_at": "2026-10-23 12:00:00 UTC"},
            "search_parameters": {"api_key": "secret-key"}, "best_flights": [offer()], "other_flights": []}
        self.settings = Settings(self.root, serpapi_key="secret-key")
        self.searcher = FlightSearch(self.settings, self.transport, self.store, self.airports)
        self.today = date(2026, 10, 23)

    def search(self, **kwargs):
        return self.searcher.search("BOS", "ORD", date(2026, 10, 25), today=self.today, **kwargs)

    def test_search_filters_connections_wrong_routes_wrong_dates_and_duplicates(self):
        self.transport.get_json.return_value["other_flights"] = [
            offer(), offer("UA 124", connecting=True), offer("UA 125", destination="LHR"),
            offer("UA 126", departure="2026-10-26 08:00"), offer("UA 127")]
        result = self.search(itinerary_type="nonstop")
        self.assertEqual([r["flight_number"] for r in result["flights"]], ["UA 123", "UA 127"])
        params = self.transport.get_json.call_args.args[1]
        self.assertEqual((params["type"], params["stops"], params["currency"]), (2, 1, "USD"))
        self.assertEqual(result["flights"][0]["history_match"], "unverified")

    def test_multiple_airports_are_filtered_to_the_requested_airport_sets(self):
        with self.airports.path.open("a") as output:
            output.write("MDW,US,41.786,-87.7524,Midway,KMDW,yes,large_airport\n")
        self.transport.get_json.return_value["other_flights"] = [
            offer("UA 124", destination="MDW"), offer("UA 125", destination="LHR")]
        result = self.searcher.search_airports(["BOS"], ["ORD", "MDW"], date(2026, 10, 25), today=self.today)
        self.assertEqual([f["destination"] for f in result["flights"]], ["ORD", "MDW"])
        self.assertEqual(self.transport.get_json.call_args.args[1]["arrival_id"], "ORD,MDW")

    def test_cache_reuses_results_without_exposing_credentials(self):
        first, second = self.search(), self.search()
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(self.transport.get_json.call_count, 1)
        self.assertNotIn("secret-key", json.dumps(second))
        self.assertNotIn(b"secret-key", (self.root / "cache.sqlite").read_bytes())

    def test_budget_is_enforced_locally_including_exact_price_boundary(self):
        offers = []
        for i, price in enumerate([199, 200, 201, None, "199", float("nan"), -1, True]):
            candidate = offer(f"UA {i + 1}")
            candidate["price"] = price
            offers.append(candidate)
        self.transport.get_json.return_value["best_flights"] = offers
        result = self.search(max_price=200)
        self.assertEqual([f["price"] for f in result["flights"]], [199, 200])
        self.assertEqual(self.transport.get_json.call_args.args[1]["max_price"], 200)

    def test_budget_cache_is_separate_from_unrestricted_and_other_budget_searches(self):
        self.search()
        self.search(max_price=200)
        self.search(max_price=250)
        self.assertEqual(self.transport.get_json.call_count, 3)
        self.assertTrue(self.search(max_price=200)["cached"])
        self.assertEqual(self.transport.get_json.call_count, 3)

    def test_invalid_budget_spends_no_search_allowance(self):
        for invalid in (0, -1, 200.5, "200", True, float("nan")):
            with self.subTest(budget=invalid), self.assertRaises(DataError):
                self.search(max_price=invalid)
        self.transport.get_json.assert_not_called()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)

    def test_no_budget_preserves_unpriced_flights_without_inventing_prices(self):
        self.transport.get_json.return_value["best_flights"][0].pop("price")
        result = self.search()
        self.assertEqual(len(result["flights"]), 1)
        self.assertIsNone(result["flights"][0]["price"])
        self.assertNotIn("max_price", self.transport.get_json.call_args.args[1])

    def test_no_within_budget_results_does_not_relax_price_limit(self):
        result = self.search(max_price=100)
        self.assertEqual(result["flights"], [])

    def test_stale_cache_uses_new_request(self):
        self.search()
        self.clock[0] += 901
        self.assertFalse(self.search()["cached"])
        self.assertEqual(self.transport.get_json.call_count, 2)

    def test_missing_key_does_not_call_provider_or_spend_allowance(self):
        self.searcher.settings = Settings(self.root)
        with self.assertRaisesRegex(DataError, "SERPAPI_API_KEY"):
            self.search()
        self.transport.get_json.assert_not_called()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)

    def test_dates_outside_window_and_international_airports_do_not_call_provider(self):
        for departure in (self.today - timedelta(days=1), self.today + timedelta(days=31)):
            with self.assertRaises(DataError):
                self.searcher.search("BOS", "ORD", departure, today=self.today)
        with self.assertRaises(DataError):
            self.searcher.search("BOS", "LHR", self.today, today=self.today)
        self.transport.get_json.assert_not_called()

    def test_empty_success_is_not_replaced_with_fake_flights(self):
        self.transport.get_json.return_value = {"search_metadata": {"status": "Success"}}
        self.assertEqual(self.search()["flights"], [])

    def test_provider_error_is_sanitized_and_not_cached(self):
        self.transport.get_json.return_value = {"error": "key=secret-key", "search_metadata": {"status": "Error"}}
        with self.assertRaises(DataError) as error:
            self.search()
        self.assertNotIn("secret-key", str(error.exception))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 0)

    def test_rolling_limit_blocks_attempt_then_releases_after_31_days(self):
        self.store.reserve_search(1)
        with self.assertRaises(DataError):
            self.store.reserve_search(1)
        self.clock[0] += 31 * 86400 + 1
        self.store.reserve_search(1)

    def test_hourly_limit_and_cache_still_work_when_quota_is_exhausted(self):
        self.search()
        for _ in range(49):
            self.store.reserve_search(250)
        with self.assertRaisesRegex(DataError, "hourly"):
            self.store.reserve_search(250)
        self.assertTrue(self.search()["cached"])


class WeatherTests(unittest.TestCase):
    def test_far_future_forecast_is_unavailable_without_network_access(self):
        transport = Mock()
        result = Weather(transport).for_date(42.36, -71.0, date(2026, 11, 15), today=date(2026, 10, 23))
        self.assertFalse(result["available"])
        transport.get_json.assert_not_called()

    def test_forecast_must_cover_requested_local_date(self):
        transport = Mock()
        transport.get_json.side_effect = [
            {"properties": {"forecastHourly": "https://api.weather.gov/gridpoints/BOX/70,76/forecast/hourly"}},
            {"properties": {"updateTime": "2026-10-23T12:00:00Z", "periods": [
                {"startTime": "2026-10-24T00:00:00-04:00", "shortForecast": "Clear"},
                {"startTime": "2026-10-25T00:00:00-04:00", "shortForecast": "Rain"}]}}]
        result = Weather(transport).for_date(42.36, -71.0, date(2026, 10, 25), today=date(2026, 10, 23))
        self.assertTrue(result["available"])
        self.assertEqual([p["shortForecast"] for p in result["periods"]], ["Rain"])

    def test_missing_station_data_is_empty_not_invented(self):
        transport = Mock()
        transport.get_json.return_value = None
        result = Weather(transport).aviation("KBOS")
        self.assertEqual(result["observations"], [])
        self.assertEqual(result["terminal_forecasts"], [])


class HistoryTests(Setup):
    def setUp(self):
        super().setUp()
        self.history = FlightHistory(self.root / "history.sqlite")
        self.csv = self.root / "flights.csv"
        self.csv.write_text("""FlightDate,Reporting_Airline,Flight_Number_Reporting_Airline,Origin,Dest,CRSDepTime,ArrDelay,Cancelled,Diverted
2024-09-01,UA,123,BOS,ORD,0800,60,0,0
2025-10-01,UA,123,BOS,ORD,0800,0,0,0
2026-09-01,UA,123,BOS,ORD,0800,-3,0,0
2026-09-02,UA,123,BOS,ORD,0800,15,0,0
2026-09-03,UA,123,BOS,ORD,0800,,1,0
2026-09-04,UA,123,BOS,ORD,0800,120,0,1
2026-09-05,UA,123,BOS,ORD,0800,,0,0
2026-09-06,UA,123,BOS,JFK,0800,90,0,0
2026-09-07,UA,456,BOS,ORD,0800,90,0,0
2026-11-01,UA,123,BOS,ORD,0800,90,0,0
2026-09-08,UA,123,BOS,ORD,2500,90,0,0
""")

    def report(self, **kwargs):
        return self.history.report("UA", "123", "BOS", "ORD", as_of=date(2026, 10, 23), **kwargs)

    def test_delay_denominator_separates_cancellations_diversions_and_missing_outcomes(self):
        imported = self.history.import_file(self.csv)
        self.assertEqual(imported["rejected"], 1)
        result = self.report()
        self.assertEqual(result["window_start"], "2024-10-01")
        self.assertEqual(result["dataset_latest_date"], "2026-09-07")
        overall = result["overall"]
        self.assertEqual(overall["scheduled_flights"], 6)
        self.assertEqual(overall["completed_with_arrival_delay"], 3)
        self.assertEqual(overall["delayed_arrivals_15_minutes_or_more"], 1)
        self.assertAlmostEqual(overall["historical_delay_rate"], 1 / 3)
        self.assertEqual((overall["cancelled_flights"], overall["diverted_flights"], overall["completed_without_delay_measurement"]), (1, 1, 1))
        self.assertEqual(result["same_calendar_month"]["scheduled_flights"], 1)

    def test_reimport_is_idempotent_and_missing_flight_has_no_made_up_rate(self):
        first = self.history.import_file(self.csv)
        second = self.history.import_file(self.csv)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["duplicates"], first["inserted"])
        missing = self.history.report("DL", "999", "BOS", "ORD", as_of=date(2026, 10, 23))
        self.assertFalse(missing["available"])
        self.assertNotIn("overall", missing)

    def test_numeric_lookup_ignores_requested_airline_but_preserves_route(self):
        self.history.import_file(self.csv)
        result = self.history.report("DL", "123", "BOS", "ORD", as_of=date(2026, 10, 23))
        self.assertTrue(result["available"])
        self.assertEqual(result["identity"]["requested_airline"], "DL")
        self.assertEqual(result["matched_reporting_carrier"], "UA")
        self.assertEqual(result["overall"]["scheduled_flights"], 6)
        self.assertEqual(result["match_method"], "flight_number_route")
        strict = self.history.report("DL", "123", "BOS", "ORD", as_of=date(2026, 10, 23), match_carrier=True)
        self.assertFalse(strict["available"])

    def test_duplicate_numbers_across_carriers_are_separate_candidate_histories(self):
        with self.csv.open("a") as output:
            output.write("2026-09-09,DL,123,BOS,ORD,0900,90,0,0\n")
        self.history.import_file(self.csv)
        result = self.report()
        self.assertFalse(result["available"])
        self.assertEqual(result["reason_code"], "ambiguous_flight_number")
        self.assertNotIn("overall", result)
        candidates = {row["reporting_carrier"]: row for row in result["carrier_candidates"]}
        self.assertEqual(candidates["DL"]["overall"]["scheduled_flights"], 1)
        self.assertEqual(candidates["DL"]["overall"]["historical_delay_rate"], 1)
        self.assertEqual(candidates["UA"]["overall"]["scheduled_flights"], 6)
        filtered = self.report(departure_hour=8)
        self.assertTrue(filtered["available"])
        self.assertEqual(filtered["matched_reporting_carrier"], "UA")

    def test_monthly_summaries_are_rejected(self):
        self.csv.write_text("year,month,carrier,airport,arr_flights,arr_del15\n2026,9,UA,BOS,100,20\n")
        with self.assertRaisesRegex(DataError, "Monthly"):
            self.history.import_file(self.csv)

    def test_zip_is_read_without_extracting_paths(self):
        archive = self.root / "flights.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("../flights.csv", self.csv.read_bytes())
        self.assertGreater(self.history.import_file(archive)["inserted"], 0)

    def test_hour_filter_never_substitutes_different_departure_time(self):
        self.history.import_file(self.csv)
        self.assertFalse(self.report(departure_hour=9)["available"])


class ConfigurationTests(Setup):
    def test_configuration_representation_does_not_expose_key(self):
        self.assertNotIn("private-key", repr(Settings(self.root, serpapi_key="private-key")))

    def test_https_certificate_and_hostname_verification_are_enabled(self):
        context = Transport.tls_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_env_values_are_read_without_shell_execution(self):
        env = self.root / ".env"
        env.write_text('SERPAPI_API_KEY="$(do-not-execute)"\nNWS_USER_AGENT="Delay Aware" # comment\n')
        self.assertEqual(read_env(env)["SERPAPI_API_KEY"], "$(do-not-execute)")

    def test_no_configuration_can_raise_search_limit_above_free_allowance(self):
        with patch.dict("os.environ", {"SERPAPI_SEARCH_LIMIT": "251"}):
            with self.assertRaises(DataError):
                Settings.load(self.root)

    def test_http_errors_do_not_expose_request_key(self):
        with patch("delay_aware.core.urlopen", side_effect=HTTPError("https://example.com/?api_key=secret", 403, "secret", {}, None)):
            with self.assertRaises(DataError) as error:
                Transport("test").get_json("https://example.com/", {"api_key": "secret"})
        self.assertNotIn("secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
