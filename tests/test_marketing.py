"""Operation-level identity joins, provenance, conflicts, and import recovery."""

from datetime import date
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock
import zipfile

from delay_aware.core import DataError
from delay_aware.history import FlightHistory
from delay_aware.history_sync import HistorySync, MARKETING_PREFIX, PREFIX, available_archives, sync_status
from delay_aware.marketing import audit_join

REPORTING = "FlightDate,Reporting_Airline,Flight_Number_Reporting_Airline,Origin,Dest,CRSDepTime,ArrDelay,Cancelled,Diverted\n"
MARKETING = "FlightDate,Operating_Airline ,Flight_Number_Operating_Airline,Origin,Dest,CRSDepTime,ArrDelay,Cancelled,Diverted,IATA_Code_Marketing_Airline,Flight_Number_Marketing_Airline,Duplicate\n"


class MarketingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.history = FlightHistory(self.root / "data" / "flight_history.sqlite")

    def load(self, reporting="", marketing="", *, month=None):
        if reporting:
            path = self.root / "reporting.csv"
            path.write_text(REPORTING + reporting)
            self.history.import_file(path, source_month=month)
        if marketing:
            path = self.root / "marketing.csv"
            path.write_text(MARKETING + marketing)
            return self.history.import_file(path, source_month=month, dataset="marketing")

    def report(self, carrier="DL", number="5652", **kwargs):
        return self.history.report(carrier, number, "SDF", "LGA", as_of=date(2026, 10, 31), **kwargs)

    def test_marketed_number_can_differ_from_operating_number(self):
        self.load("2026-08-01,YX,4321,SDF,LGA,0800,20,0,0\n",
                  "2026-08-01,YX,4321,SDF,LGA,0800,20,0,0,DL,5652,N\n")
        result = self.report()
        self.assertTrue(result["available"])
        self.assertTrue(result["historical_identity_verified"])
        self.assertFalse(result["identity_verified"])
        self.assertEqual(result["overall"]["scheduled_flights"], 1)
        self.assertEqual(result["operating_services"][0]["operating_flight_number"], "4321")
        self.assertEqual(result["join_coverage"]["matched_reporting_operations"], 1)
        self.assertEqual(result["match_method"], "bts_marketing_operating_key")

    def test_each_join_key_component_is_required_and_outcomes_do_not_cross_join(self):
        baseline = ["2026-08-01", "YX", "4321", "SDF", "LGA", "0800", "90", "0", "0"]
        alternatives = ["2026-08-02", "OO", "9999", "BOS", "ORD", "0900"]
        rows = []
        for index, value in enumerate(alternatives):
            altered = baseline.copy()
            altered[index] = value
            rows.append(",".join(altered) + "\n")
        self.load("".join(rows), "2026-08-01,YX,4321,SDF,LGA,0800,5,0,0,DL,5652,N\n")
        result = self.report()
        self.assertEqual(result["join_coverage"]["matched_reporting_operations"], 0)
        self.assertEqual(result["join_coverage"]["marketing_only_operations"], 1)
        self.assertEqual(result["overall"]["historical_delay_rate"], 0)

    def test_codeshares_and_reimport_do_not_multiply_operations(self):
        row = "2026-08-01,YX,4321,SDF,LGA,0800,20,0,0,DL,5652,N\n"
        imported = self.load("2026-08-01,YX,4321,SDF,LGA,0800,20,0,0\n",
                             row + row + row.replace(",DL,5652,", ",UA,7777,"), month="2026-08")
        self.assertEqual(imported["duplicates"], 1)
        self.assertEqual(self.history.coverage()["marketing"]["synced_records"], 2)
        again = self.history.import_file(self.root / "marketing.csv", dataset="marketing")
        self.assertEqual(again["inserted"], 0)
        self.assertEqual(self.report()["overall"]["scheduled_flights"], 1)
        audit = audit_join(self.history.path, "2026-08")
        self.assertEqual(audit["marketing_relationships"], 2)
        self.assertEqual(audit["distinct_operating_records"], 1)
        self.assertEqual(audit["matched_reporting_operations"], 1)

    def test_outcome_conflicts_are_excluded_without_falling_back_to_numeric_match(self):
        self.load("2026-08-01,YX,4321,SDF,LGA,0800,5,0,0\n",
                  "2026-08-01,YX,4321,SDF,LGA,0800,90,0,0,DL,5652,N\n")
        result = self.report()
        self.assertFalse(result["available"])
        self.assertEqual(result["reason_code"], "marketing_records_conflicted")
        self.assertNotIn("overall", result)
        self.assertEqual(result["join_coverage"]["outcome_conflicts_excluded"], 1)
        self.assertEqual(audit_join(self.history.path, "2026-08")["outcome_conflicts"], 1)

    def test_cancellation_and_diversion_conflicts_are_excluded(self):
        for index, outcome in enumerate(("0,1", "1,0")):
            with self.subTest(outcome=outcome):
                operating, marketed = str(4321 + index), str(5652 + index)
                self.load(f"2026-08-01,YX,{operating},SDF,LGA,0800,,0,0\n",
                          f"2026-08-01,YX,{operating},SDF,LGA,0800,,{outcome},DL,{marketed},N\n")
                self.assertFalse(self.report(number=marketed)["available"])

    def test_marketing_only_coverage_and_missing_measurements_are_explicit(self):
        self.load(marketing="2026-08-01,YX,4321,SDF,LGA,0800,,0,0,DL,5652,N\n")
        result = self.report()
        self.assertTrue(result["available"])
        self.assertIsNone(result["overall"]["historical_delay_rate"])
        self.assertEqual(result["overall"]["completed_without_delay_measurement"], 1)
        self.assertEqual(result["join_coverage"]["marketing_only_operations"], 1)

    def test_conflicting_operators_for_one_marketed_departure_are_not_blended(self):
        self.load(marketing="2026-08-01,YX,4321,SDF,LGA,0800,5,0,0,DL,5652,N\n"
                  "2026-08-01,OO,9876,SDF,LGA,0800,90,0,0,DL,5652,N\n")
        result = self.report()
        self.assertFalse(result["available"])
        self.assertEqual(result["join_coverage"]["ambiguous_departures_excluded"], 1)

    def test_operator_changes_over_time_keep_explicit_service_groups(self):
        self.load(marketing="2026-07-01,YX,4321,SDF,LGA,0800,5,0,0,DL,5652,N\n"
                  "2026-08-01,OO,9876,SDF,LGA,0900,20,0,0,DL,5652,N\n")
        result = self.report()
        self.assertEqual(len(result["operating_services"]), 2)
        self.assertIsNone(result["matched_reporting_carrier"])
        self.assertEqual(result["overall"]["historical_delay_rate"], 0.5)
        self.assertEqual(self.report(departure_hour=8)["overall"]["scheduled_flights"], 1)
        self.assertFalse(self.report("AA")["available"])

    def test_swap_duplicates_bad_rows_and_wrong_month_do_not_corrupt_import(self):
        result = self.load(marketing="2026-08-01,YX,4321,SDF,LGA,0800,5,0,0,DL,5652,Y\n"
                           "2026-08-02,YX,4321,SDF,LGA,0800,5,0,0,DL,5652,N\n"
                           "2026-08-03,YX,4321,SDF,LGA,2560,5,0,0,DL,5652,N\n", month="2026-08")
        self.assertEqual((result["inserted"], result["excluded_source_duplicates"], result["rejected"]), (1, 1, 1))
        row = "2026-07-01,YX,4321,SDF,LGA,0800,5,0,0,DL,5652,N\n"
        path = self.root / "rollback.csv"
        path.write_text(MARKETING + row * 5000 + row.replace("2026-07-01", "2026-06-01"))
        with self.assertRaisesRegex(DataError, "outside"):
            self.history.import_file(path, dataset="marketing", source_month="2026-07")
        with sqlite3.connect(self.history.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM marketing_flights").fetchone()[0], 1)
        self.assertEqual(self.history.coverage()["marketing"]["loaded_months"], ["2026-08"])

    def test_marketing_sync_has_separate_ledger_and_resumes_without_downloading(self):
        self.load(reporting="2026-08-01,YX,4321,SDF,LGA,0800,20,0,0\n", month="2026-08")
        transport = Mock(user_agent="Test")
        transport.get_bytes.return_value = (f"{PREFIX}2026_8.zip {MARKETING_PREFIX}2026_8.zip "
                                           f"{MARKETING_PREFIX}2026_9.zip").encode()
        archive = self.root / "marketing.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("marketing.csv", MARKETING + "2026-08-01,YX,4321,SDF,LGA,0800,20,0,0,DL,5652,N\n")
        sync = HistorySync(self.root, transport, self.history, dataset="marketing")
        sync.download = Mock(return_value=archive)
        result = sync.run(months=1, today=date(2026, 10, 23), progress=Mock())
        self.assertEqual(result["completed_months"], ["2026-08"])
        self.assertEqual(sync_status(self.root, "marketing")["state"], "ready")
        sync.download.reset_mock()
        sync.run(months=1, today=date(2026, 10, 23), progress=Mock())
        sync.download.assert_not_called()
        self.assertEqual(self.history.coverage()["synced_records"], 1)
        found = available_archives(transport.get_bytes.return_value.decode(), date(2026, 10, 23), "marketing")
        self.assertEqual(len(found), 2)


if __name__ == "__main__":
    unittest.main()
