"""Resumable, verified downloads of official BTS monthly flight-level archives."""

from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile

from .core import DataError, Transport, utc_now
from .history import FlightHistory

ARCHIVE_ROOT = "https://transtats.bts.gov/PREZIP/"
PREFIX = "On_Time_Reporting_Carrier_On_Time_Performance_1987_present_"
PATTERN = re.compile(re.escape(PREFIX) + r"(\d{4})_(\d{1,2})\.zip")
MARKETING_PREFIX = "On_Time_Marketing_Carrier_On_Time_Performance_Beginning_January_2018_"


def month_label(year, month):
    return f"{year:04d}-{month:02d}"


def available_archives(html: str, today: date, dataset="reporting"):
    prefix = MARKETING_PREFIX if dataset == "marketing" else PREFIX
    pattern = re.compile(re.escape(prefix) + r"(\d{4})_(\d{1,2})\.zip")
    found = {}
    for year_text, month_text in pattern.findall(html):
        year, month = int(year_text), int(month_text)
        if 1987 <= year <= today.year and 1 <= month <= 12 and (year, month) < (today.year, today.month):
            found[(year, month)] = f"{prefix}{year}_{month}.zip"
    return found


def select_months(available, months):
    if not 1 <= months <= 24 or not available:
        raise DataError("Choose 1–24 months and ensure BTS monthly archives are available.")
    year, month = max(available)
    latest = year * 12 + month - 1
    selected = []
    for offset in range(months):
        value = latest - offset
        period = (value // 12, value % 12 + 1)
        if period not in available:
            raise DataError(f"BTS archive directory is missing {month_label(*period)}. No complete window can be declared.")
        selected.append((month_label(*period), available[period]))
    return selected


class HistorySync:
    def __init__(self, root: Path, transport, history=None, *, dataset="reporting"):
        if dataset not in ("reporting", "marketing"):
            raise DataError("Choose reporting or marketing BTS data.")
        self.dataset = dataset
        self.root, self.transport = root, transport
        self.history = history or FlightHistory(root / "data" / "flight_history.sqlite")
        self.folder = root / "data" / ("bts_marketing" if dataset == "marketing" else "bts")
        self.state_path = root / "data" / ("marketing_history_sync.json" if dataset == "marketing" else "history_sync.json")

    def save_state(self, state):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({**state, "updated_at": utc_now()}, indent=2) + "\n")
        temporary.replace(self.state_path)

    def download(self, filename, on_bytes):
        path = self.folder / filename
        if path.exists() and zipfile.is_zipfile(path):
            return path
        temporary = path.with_suffix(".part")
        request = Request(ARCHIVE_ROOT + filename, headers={"User-Agent": self.transport.user_agent})
        try:
            with urlopen(request, timeout=120, context=Transport.tls_context()) as response, temporary.open("wb") as output:
                size = 0
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > 512 * 1024 * 1024:
                        raise DataError("BTS monthly archive exceeds the supported download size.")
                    output.write(block)
                    on_bytes(size)
        except HTTPError as error:
            raise DataError(f"BTS archive download returned HTTP {error.code}.") from None
        except (URLError, TimeoutError, OSError):
            raise DataError("BTS archive download was interrupted. Rerun sync-history to resume completed months.") from None
        if not zipfile.is_zipfile(temporary):
            raise DataError("BTS returned a response that is not a valid ZIP archive.")
        temporary.replace(path)
        return path

    def run(self, *, months=24, today=None, progress=print):
        self.folder.mkdir(parents=True, exist_ok=True)
        lock_folder = self.root / "data" / "bts"
        lock_folder.mkdir(parents=True, exist_ok=True)
        with (lock_folder / ".sync.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DataError("A history sync is already running. Check history-status for progress.") from None
            state = {"state": "discovering", "dataset": self.dataset, "started_at": utc_now(), "target_months": months,
                     "completed_months": [], "current_month": None, "download_bytes": 0}
            self.save_state(state)
            try:
                html = self.transport.get_bytes(ARCHIVE_ROOT).decode("utf-8")
                available = available_archives(html, today or date.today(), self.dataset)
                # Anchor enrichment to the reporting window already imported.
                if self.dataset == "marketing":
                    with closing(sqlite3.connect(self.history.path)) as db:
                        latest_reporting = db.execute("SELECT MAX(month) FROM synced_months").fetchone()[0]
                    if latest_reporting:
                        available = {period: name for period, name in available.items()
                                     if month_label(*period) <= latest_reporting}
                selected = select_months(available, months)
                state.update({"window_start": selected[-1][0], "window_end": selected[0][0]})
                with closing(sqlite3.connect(self.history.path)) as db:
                    ledger = "marketing_synced_months" if self.dataset == "marketing" else "synced_months"
                    completed = {row[0] for row in db.execute(f"SELECT month FROM {ledger}")}
                # Fetch a few public archives concurrently; SQLite imports remain
                # ordered and transactional. Completed months are never refetched.
                with ThreadPoolExecutor(max_workers=4) as pool:
                    pending = {period: pool.submit(self.download, filename, lambda size: None)
                               for period, filename in selected if period not in completed}
                    self.import_months(selected, completed, pending, state, progress)
                state.update({"state": "ready", "current_month": None, "coverage": self.history.coverage()})
                self.save_state(state)
                return state
            except BaseException as error:
                state.update({"state": "error", "error": str(error) or "History sync interrupted. Rerun to resume."})
                self.save_state(state)
                raise

    def import_months(self, selected, completed, pending, state, progress):
        for period, filename in selected:
            if period in completed:
                state["completed_months"].append(period)
                self.save_state(state)
                continue
            state.update({"state": "downloading", "current_month": period, "download_bytes": 0})
            self.save_state(state)
            progress(json.dumps({"stage": "downloading", "month": period}), flush=True)
            archive = pending[period].result()
            state["download_bytes"] = archive.stat().st_size
            checksum = self.checksum(archive)
            state["state"] = "importing"
            self.save_state(state)
            result = self.history.import_file(archive, source_month=period, checksum=checksum, dataset=self.dataset)
            state["completed_months"].append(period)
            self.save_state(state)
            progress(json.dumps({"stage": "imported", "month": period, **result}), flush=True)

    @staticmethod
    def checksum(path):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()


def sync_status(root: Path, dataset="reporting"):
    path = root / "data" / ("marketing_history_sync.json" if dataset == "marketing" else "history_sync.json")
    if not path.exists():
        return {"state": "not_started", "completed_months": []}
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return {"state": "unknown", "completed_months": []}
