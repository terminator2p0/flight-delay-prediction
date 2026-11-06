"""History sync coverage, atomicity and resume checks without provider calls."""

from datetime import date
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock
import zipfile

from delay_aware.core import DataError
from delay_aware.history import FlightHistory
from delay_aware.history_sync import HistorySync, PREFIX, available_archives, select_months, sync_status

HEADER = "FlightDate,Reporting_Airline,Flight_Number_Reporting_Airline,Origin,Dest,CRSDepTime,ArrDelay,Cancelled,Diverted\n"


class HistorySyncTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.transport = Mock(user_agent="Test")
        self.history = FlightHistory(self.root / "data" / "flight_history.sqlite")
        self.sync = HistorySync(self.root, self.transport, self.history)

    def archive(self, month, rows=None):
        path = self.root / f"{month}.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("flights.csv", HEADER + (rows or f"{month}-01,UA,123,BOS,ORD,0800,15,0,0\n"))
        return path

    def test_discovery_filters_current_future_invalid_and_other_datasets(self):
        html = " ".join(f"{PREFIX}{suffix}.zip" for suffix in
                        ("2026_8", "2026_9", "2026_10", "2027_1", "2026_13", "1986_1"))
        found = available_archives(html + "Airline_Delay_Cause.csv", date(2026, 10, 23))
        self.assertEqual(sorted(found), [(2026, 8), (2026, 9)])

    def test_window_is_contiguous_across_year_boundary(self):
        available = {(2026, 1): "jan.zip", (2025, 12): "dec.zip", (2025, 11): "nov.zip"}
        self.assertEqual(select_months(available, 3),
                         [("2026-01", "jan.zip"), ("2025-12", "dec.zip"), ("2025-11", "nov.zip")])
        del available[(2025, 12)]
        with self.assertRaisesRegex(DataError, "2025-12"):
            select_months(available, 3)

    def test_unreasonable_window_and_empty_directory_are_rejected(self):
        for months in (0, 25):
            with self.assertRaises(DataError):
                select_months({(2026, 8): "aug.zip"}, months)
        with self.assertRaises(DataError):
            select_months({}, 24)

    def test_full_month_commit_records_checksum_and_resume_skips_download(self):
        self.transport.get_bytes.return_value = f"{PREFIX}2026_8.zip".encode()
        archive = self.archive("2026-08")
        self.sync.download = Mock(return_value=archive)
        result = self.sync.run(months=1, today=date(2026, 10, 23), progress=Mock())
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["completed_months"], ["2026-08"])
        self.assertEqual(result["coverage"]["synced_records"], 1)
        with sqlite3.connect(self.history.path) as db:
            checksum = db.execute("SELECT sha256 FROM synced_months").fetchone()[0]
        self.assertEqual(checksum, self.sync.checksum(archive))
        self.sync.download.reset_mock()
        self.sync.run(months=1, today=date(2026, 10, 23), progress=Mock())
        self.sync.download.assert_not_called()
        self.assertEqual(self.history.coverage()["synced_records"], 1)
        self.assertEqual(sync_status(self.root)["state"], "ready")

    def test_mislabeled_archive_rolls_back_already_flushed_rows_and_month_ledger(self):
        rows = "2026-08-01,UA,123,BOS,ORD,0800,15,0,0\n" * 5000
        rows += "2026-07-01,UA,123,BOS,ORD,0800,15,0,0\n"
        with self.assertRaisesRegex(DataError, "outside"):
            self.history.import_file(self.archive("2026-08", rows), source_month="2026-08")
        self.assertFalse(self.history.coverage()["has_records"])
        self.assertEqual(self.history.coverage()["loaded_months"], [])

    def test_failure_is_visible_and_does_not_mark_a_month_complete(self):
        self.transport.get_bytes.return_value = f"{PREFIX}2026_8.zip".encode()
        self.sync.download = Mock(side_effect=DataError("Download interrupted"))
        with self.assertRaisesRegex(DataError, "interrupted"):
            self.sync.run(months=1, today=date(2026, 10, 23), progress=Mock())
        self.assertEqual(sync_status(self.root)["state"], "error")
        self.assertEqual(sync_status(self.root)["completed_months"], [])

    def test_reports_distinguish_missing_imports_from_unmatched_flights(self):
        args = ("DL", "999", "BOS", "ORD")
        self.assertEqual(self.history.report(*args, as_of=date(2026, 10, 23))["reason_code"], "history_not_loaded")
        self.history.import_file(self.archive("2026-08"), source_month="2026-08")
        missing = self.history.report(*args, as_of=date(2026, 10, 23))
        self.assertEqual(missing["reason_code"], "flight_not_matched")
        self.assertNotIn("overall", missing)
