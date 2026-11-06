"""Local application and agent checks without network requests or inference."""

from datetime import date
from email.message import Message
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock

from delay_aware.agent import Ollama, answer
from delay_aware.application import Application
from delay_aware.core import DataError, Settings
from delay_aware.server import handler_for

AIRPORTS = """iata_code,iso_country,latitude_deg,longitude_deg,name,ident,scheduled_service,type
BOS,US,42.3656,-71.0096,Logan,KBOS,yes,large_airport
ORD,US,41.9742,-87.9073,OHare,KORD,yes,large_airport
MDW,US,41.786,-87.7524,Midway,KMDW,yes,large_airport
"""
FLIGHT = {"flight_number": "UA 123", "origin": "BOS", "destination": "ORD",
          "departure_local": "2026-10-23T08:00:00", "arrival_local": "2026-10-23T10:00:00",
          "price": 200, "airline": "United", "currency": "USD"}


class AgentTests(unittest.TestCase):
    def client(self, *messages):
        client = Mock()
        client.lock = threading.Lock()
        client.chat.side_effect = messages
        return client

    def test_agent_calls_tool_and_does_not_expose_private_reasoning(self):
        client = self.client(
            {"content": "", "thinking": "PRIVATE REASONING", "tool_calls": [{"function": {"name": "get_flight_history", "arguments": {}}}]},
            {"content": "BTS has no records anywhere for this flight."})
        execute = Mock(return_value={"source": "BTS", "available": False})
        result, _ = answer(client, "gpt-oss:20b", {"flight": FLIGHT}, [], "Check history", execute)
        execute.assert_called_once_with("get_flight_history")
        self.assertNotIn("PRIVATE REASONING", json.dumps(result))
        self.assertEqual(result["tools_used"][0]["source"], "BTS")
        self.assertIn("in this app", result["answer"])
        self.assertNotIn("no records anywhere", result["answer"])
        self.assertFalse(client.lock.locked())

    def test_unapproved_tool_cannot_execute(self):
        client = self.client(
            {"tool_calls": [{"function": {"name": "run_shell", "arguments": {"command": "anything"}}}]},
            {"content": "I can only use flight-data tools."})
        execute = Mock()
        answer(client, "gpt-oss:20b", {}, [], "Do something else", execute)
        execute.assert_not_called()

    def test_unmatched_flight_does_not_become_an_empty_database_claim(self):
        client = self.client(
            {"tool_calls": [{"function": {"name": "get_flight_history", "arguments": {}}}]},
            {"content": "You need to load all BTS history first."})
        result, turns = answer(client, "gpt-oss:20b", {"flight": FLIGHT}, [], "Check history",
                               Mock(return_value={"source": "BTS", "available": False,
                                                  "reason_code": "flight_not_matched"}))
        self.assertIn("BTS history is loaded", result["answer"])
        self.assertIn("no records match", result["answer"])
        self.assertEqual(turns[-1]["content"], result["answer"])

    def test_ambiguous_number_does_not_become_an_empty_database_claim(self):
        client = self.client(
            {"tool_calls": [{"function": {"name": "get_flight_history", "arguments": {}}}]},
            {"content": "No history has been loaded."})
        result, _ = answer(client, "gpt-oss:20b", {"flight": FLIGHT}, [], "Check history",
                           Mock(return_value={"source": "BTS", "available": False,
                                              "reason_code": "ambiguous_flight_number",
                                              "carrier_candidates": [{"reporting_carrier": "OO"}, {"reporting_carrier": "YX"}]}))
        self.assertIn("OO, YX", result["answer"])
        self.assertIn("cannot be combined", result["answer"])
        self.assertNotIn("No history has been loaded", result["answer"])

    def test_repeated_tool_is_not_executed_twice_in_one_turn(self):
        call = {"function": {"name": "find_alternatives", "arguments": {}}}
        client = self.client({"tool_calls": [call, call]}, {"content": "Here are the available options."})
        execute = Mock(return_value={"source": "SerpApi", "flights": []})
        answer(client, "gpt-oss:20b", {}, [], "Compare options", execute)
        execute.assert_called_once()

    def test_tool_limit_releases_model_lock(self):
        client = self.client({"tool_calls": [{}] * 5})
        with self.assertRaises(DataError):
            answer(client, "gpt-oss:20b", {}, [], "Compare", Mock())
        self.assertFalse(client.lock.locked())

    def test_ollama_cannot_be_redirected_to_a_remote_base_address(self):
        for address in ("https://remote.example", "http://remote.example", "http://localhost:11434/secret", "http://user:pass@localhost:11434"):
            with self.subTest(address=address), self.assertRaises(DataError):
                Ollama(address)


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "airports.csv").write_text(AIRPORTS)
        self.app = Application(Settings(self.root))
        self.app.today = Mock(return_value=date(2026, 10, 23))
        self.session = {"result": {"query": {"adults": 1, "max_price": 250}, "flights": [FLIGHT]},
                        "alternatives": {}}

    def test_alternatives_include_nearby_airports_respect_budget_and_skip_past_dates(self):
        def search(origins, destinations, departure, **kwargs):
            self.assertEqual(origins, ["BOS"])
            self.assertIn("MDW", destinations)
            self.assertEqual(kwargs["max_price"], 250)
            option = {**FLIGHT, "flight_number": "UA 456", "destination": "MDW",
                      "departure_local": departure.isoformat() + "T09:00:00", "price": 180}
            return {"flights": [option], "cached": False, "fetched_at": "2026-10-23"}
        self.app.searcher.search_airports = Mock(side_effect=search)
        result = self.app.alternatives_for(self.session, FLIGHT)
        self.assertEqual(self.app.searcher.search_airports.call_count, 2)
        self.assertEqual([row["date"] for row in result["dates_checked"]], ["2026-10-23", "2026-10-24"])
        self.assertEqual(result["flights"][0]["price_difference"], -20)
        self.assertTrue(result["flights"][0]["changed_destination"])
        self.assertFalse(result["reliability_ranking_available"])
        self.app.alternatives_for(self.session, FLIGHT)
        self.assertEqual(self.app.searcher.search_airports.call_count, 2)

    def test_selected_flight_must_come_from_saved_search(self):
        with self.assertRaises(DataError):
            self.app.selected({"search_id": "invented", "flight_index": 0, "flight": FLIGHT})

    def test_empty_history_cannot_become_a_verified_probability(self):
        report = self.app.history_for(FLIGHT)
        self.assertFalse(report["available"])
        self.assertFalse(report["identity_verified"])
        self.assertNotIn("overall", report)
        self.assertIn("No matching reporting record has been established", report["identity_note"])

    def test_status_exposes_actual_coverage_separately_from_target(self):
        self.app.ollama.models = Mock(return_value=["gpt-oss:20b"])
        status = self.app.status()
        self.assertEqual(status["history_months"], 24)
        self.assertFalse(status["history_ready"])
        self.assertEqual(status["history_coverage"]["loaded_months"], [])
        self.assertEqual(status["history_sync"]["state"], "not_started")

    def test_application_splits_prefix_and_reports_actual_bts_carrier(self):
        path = self.root / "regional.csv"
        path.write_text("FlightDate,Reporting_Airline,Flight_Number_Reporting_Airline,Origin,Dest,CRSDepTime,ArrDelay,Cancelled,Diverted\n2026-08-01,YX,5652,BOS,ORF,0800,20,0,0\n")
        self.app.history.import_file(path)
        result = self.app.history_for({**FLIGHT, "flight_number": "DL 5652", "destination": "ORF"})
        self.assertTrue(result["available"])
        self.assertEqual(result["identity"]["flight_number"], "5652")
        self.assertEqual(result["matched_reporting_carrier"], "YX")
        self.assertFalse(result["identity_verified"])
        self.assertIn("prefix was not used", result["identity_note"])

    def test_past_weather_coverage_is_unavailable_without_fetching(self):
        flight = {**FLIGHT, "departure_local": "2026-11-06T08:00:00", "arrival_local": "2026-11-06T10:00:00"}
        self.app.weather.transport = Mock()
        result = self.app.weather_for(flight)
        self.assertTrue(all(not item["available"] for item in result["airports"]))
        self.app.weather.transport.get_json.assert_not_called()

    def test_marketing_records_verify_historical_link_but_not_future_operator(self):
        reporting = self.root / "reporting.csv"
        reporting.write_text("FlightDate,Reporting_Airline,Flight_Number_Reporting_Airline,Origin,Dest,CRSDepTime,ArrDelay,Cancelled,Diverted\n2026-08-01,YX,4321,BOS,ORF,0800,20,0,0\n")
        marketing = self.root / "marketing.csv"
        marketing.write_text("FlightDate,Operating_Airline,Flight_Number_Operating_Airline,Origin,Dest,CRSDepTime,ArrDelay,Cancelled,Diverted,IATA_Code_Marketing_Airline,Flight_Number_Marketing_Airline\n2026-08-01,YX,4321,BOS,ORF,0800,20,0,0,DL,5652\n")
        self.app.history.import_file(reporting)
        self.app.history.import_file(marketing, dataset="marketing")
        result = self.app.history_for({**FLIGHT, "flight_number": "DL 5652", "destination": "ORF"})
        self.assertTrue(result["available"])
        self.assertTrue(result["historical_identity_verified"])
        self.assertFalse(result["identity_verified"])
        self.assertEqual(result["join_coverage"]["matched_reporting_operations"], 1)
        self.assertIn("verified historical relationships", result["identity_note"])

    def test_combined_assessment_keeps_bts_history_when_aeroapi_is_unavailable(self):
        self.app.aeroapi.lookup = Mock(return_value={"source": "FlightAware AeroAPI", "available": False,
            "selected_flight_verified": False, "reason": "FlightAware is temporarily unavailable.",
            "recent": {"available": False, "groups": []}})
        historical = {"source": "BTS", "available": True, "historical_identity_verified": True,
                      "overall": {"scheduled_flights": 50}}
        self.app.history_for = Mock(return_value=historical)
        result = self.app.analysis_for(FLIGHT)
        self.assertEqual(result["history"], historical)
        self.assertFalse(result["analysis"]["selected_flight_verified"])
        self.assertIn("50 operations", result["analysis"]["summary"])

    def test_combined_identity_note_agrees_with_exact_api_match(self):
        stats = {"scheduled_flights": 50, "completed_with_arrival_delay": 50,
                 "delayed_arrivals_15_minutes_or_more": 5, "historical_delay_rate": 0.1}
        self.app.history_for = Mock(return_value={"source": "BTS", "available": True,
            "historical_identity_verified": True, "identity_verified": False,
            "operating_services": [{"operating_carrier": "UA", "operating_flight_number": "123",
                "observed_first_date": "2025-01-01", "observed_last_date": "2026-08-31", "overall": stats}]})
        self.app.aeroapi.lookup = Mock(return_value={"selected_flight_verified": True,
            "selected": {"operating_ident": "UA123"}, "recent": {"groups": []}})
        result = self.app.analysis_for(FLIGHT)
        self.assertTrue(result["history"]["identity_verified"])
        self.assertTrue(result["history"]["selected_departure_identity_verified"])
        self.assertIn("currently identifies", result["history"]["identity_note"])
        self.assertNotIn("not verified", result["history"]["identity_note"])


class ServerBoundaryTests(unittest.TestCase):
    def request(self, method, path, *, host="127.0.0.1:8765", origin="http://127.0.0.1:8765", custom_header=True):
        app = Mock()
        app.search.return_value = {"flights": []}
        handler = handler_for(app).__new__(handler_for(app))
        handler.server = SimpleNamespace(server_address=("127.0.0.1", 8765))
        handler.request_version = "HTTP/1.1"
        handler.command, handler.path = method, path
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.headers = Message()
        for key, value in {"Host": host, "Origin": origin, "Content-Type": "application/json", "Content-Length": "2"}.items():
            handler.headers[key] = value
        if custom_header:
            handler.headers["X-Delay-Aware"] = "local"
        handler.rfile, handler.wfile = BytesIO(b"{}"), BytesIO()
        getattr(handler, "do_" + method)()
        return handler.wfile.getvalue(), app

    def test_project_files_are_never_served(self):
        for path in ("/.env", "/../.env", "/delay_aware/core.py"):
            with self.subTest(path=path):
                response, _ = self.request("GET", path)
                self.assertTrue(response.startswith(b"HTTP/1.0 404"))

    def test_foreign_origin_or_rebound_hostname_cannot_run_a_search(self):
        for kwargs in ({"origin": "https://remote.example"}, {"host": "remote.example:8765"}, {"custom_header": False}):
            with self.subTest(kwargs=kwargs):
                response, app = self.request("POST", "/api/search", **kwargs)
                self.assertTrue(response.startswith(b"HTTP/1.0 403"))
                app.search.assert_not_called()

    def test_same_origin_search_works_with_the_application_header(self):
        response, app = self.request("POST", "/api/search")
        self.assertTrue(response.startswith(b"HTTP/1.0 200"))
        app.search.assert_called_once_with({})


if __name__ == "__main__":
    unittest.main()
