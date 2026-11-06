"""Import BTS flight-level CSVs and calculate observed arrival reliability."""

from contextlib import closing, contextmanager
import csv
from datetime import date, datetime
import io
import math
from pathlib import Path
import re
import sqlite3
import zipfile

from .core import DataError, utc_now
from .timing import arrival_timing

ALIASES = {
    "date": ("FlightDate", "FL_DATE"),
    "carrier": ("Reporting_Airline", "OP_UNIQUE_CARRIER"),
    "number": ("Flight_Number_Reporting_Airline", "OP_CARRIER_FL_NUM"),
    "origin": ("Origin",), "destination": ("Dest",),
    "scheduled": ("CRSDepTime", "CRS_DEP_TIME"),
    "delay": ("ArrDelay", "ARR_DELAY", "ArrDelayMinutes", "ARR_DELAY_NEW"),
    "cancelled": ("Cancelled", "CANCELLED"), "diverted": ("Diverted", "DIVERTED"),
}


def normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def number(value: str) -> str:
    result = float(value)
    if not math.isfinite(result) or result < 0 or not result.is_integer():
        raise ValueError("Invalid flight number")
    return str(int(result))


@contextmanager
def csv_file(path: Path):
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if len(members) != 1:
                raise DataError("Use a BTS ZIP containing exactly one flight-level CSV.")
            # Read the member directly; do not extract archive paths to disk.
            with archive.open(members[0]) as binary:
                with io.TextIOWrapper(binary, encoding="utf-8-sig", newline="") as stream:
                    yield stream
    else:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            yield stream


class FlightHistory:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(sqlite3.connect(path, timeout=30)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS flights (
                date TEXT, carrier TEXT, number TEXT, origin TEXT, destination TEXT,
                scheduled TEXT, hour INTEGER, delay REAL, cancelled INTEGER, diverted INTEGER,
                PRIMARY KEY (date, carrier, number, origin, destination, scheduled))""")
            db.execute("CREATE INDEX IF NOT EXISTS flight_lookup ON flights(carrier, number, origin, destination, date)")
            db.execute("CREATE INDEX IF NOT EXISTS flight_number_route ON flights(number, origin, destination, date)")
            db.execute("CREATE INDEX IF NOT EXISTS flight_dates ON flights(date)")
            db.execute("CREATE TABLE IF NOT EXISTS imports (file TEXT, imported_at TEXT, inserted INTEGER, rejected INTEGER)")
            db.execute("""CREATE TABLE IF NOT EXISTS synced_months (
                month TEXT PRIMARY KEY, file TEXT, sha256 TEXT, imported_at TEXT,
                rows INTEGER, rejected INTEGER)""")
            db.execute("""CREATE TABLE IF NOT EXISTS marketing_flights (
                date TEXT, carrier TEXT, number TEXT, origin TEXT, destination TEXT,
                scheduled TEXT, hour INTEGER, delay REAL, cancelled INTEGER, diverted INTEGER,
                marketing_carrier TEXT, marketing_number TEXT,
                PRIMARY KEY (date, carrier, number, origin, destination, scheduled, marketing_carrier, marketing_number)
                ) WITHOUT ROWID""")
            db.execute("""CREATE INDEX IF NOT EXISTS marketing_lookup ON marketing_flights
                (marketing_carrier, marketing_number, origin, destination, date)""")
            db.execute("CREATE TABLE IF NOT EXISTS marketing_imports (file TEXT, imported_at TEXT, inserted INTEGER, rejected INTEGER)")
            db.execute("""CREATE TABLE IF NOT EXISTS marketing_synced_months (
                month TEXT PRIMARY KEY, file TEXT, sha256 TEXT, imported_at TEXT,
                rows INTEGER, rejected INTEGER, excluded_duplicates INTEGER)""")

    def import_file(self, path: Path, *, source_month: str | None = None, checksum: str | None = None,
                    dataset: str = "reporting"):
        if dataset not in ("reporting", "marketing"):
            raise DataError("Choose reporting or marketing BTS data.")
        marketing = dataset == "marketing"
        if not path.is_file():
            raise DataError("BTS input file does not exist.")
        inserted, rejected, duplicates, excluded_duplicates = 0, 0, 0, 0
        with csv_file(path) as stream:
            reader = csv.reader(stream)
            headers = {normalized(name): index for index, name in enumerate(next(reader, []))}
            fields = dict(ALIASES)
            if marketing:
                fields.update({"carrier": ("Operating_Airline",), "number": ("Flight_Number_Operating_Airline",),
                               "marketing_carrier": ("IATA_Code_Marketing_Airline", "Marketing_Airline_Network"),
                               "marketing_number": ("Flight_Number_Marketing_Airline",)})
            columns = {}
            for field, aliases in fields.items():
                columns[field] = next((headers[normalized(alias)] for alias in aliases if normalized(alias) in headers), None)
            if any(column is None for column in columns.values()):
                raise DataError(f"Use BTS {dataset.title()} Carrier flight-level records. Monthly Airline_Delay_Cause summaries cannot describe individual flights.")
            duplicate_column = headers.get("duplicate") if marketing else None
            table = "marketing_flights" if marketing else "flights"
            with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
                batch = []
                def flush():
                    nonlocal inserted, duplicates
                    before = db.total_changes
                    placeholders = ",".join("?" for _ in range(12 if marketing else 10))
                    db.executemany(f"INSERT OR IGNORE INTO {table} VALUES ({placeholders})", batch)
                    added = db.total_changes - before
                    inserted += added
                    duplicates += len(batch) - added
                    batch.clear()
                for raw in reader:
                    if not raw:
                        continue
                    row = {key: raw[column].strip() if column < len(raw) else "" for key, column in columns.items()}
                    try:
                        raw_date = row["date"][:10]
                        flight_date = date.fromisoformat(raw_date) if "-" in raw_date else datetime.strptime(raw_date, "%Y%m%d").date()
                        if source_month and flight_date.isoformat()[:7] != source_month:
                            raise DataError("The downloaded BTS file contains dates outside its advertised month.")
                        if duplicate_column is not None:
                            duplicate_flag = raw[duplicate_column].strip().upper() if duplicate_column < len(raw) else ""
                            if duplicate_flag not in ("Y", "N"):
                                raise ValueError("Invalid duplicate flag")
                            if duplicate_flag == "Y":
                                excluded_duplicates += 1
                                continue
                        flight_number = number(row["number"])
                        time_value = float(row["scheduled"])
                        if not math.isfinite(time_value) or not time_value.is_integer():
                            raise ValueError("Invalid scheduled time")
                        scheduled = int(time_value)
                        if not 0 <= scheduled <= 2400 or scheduled % 100 >= 60 or (scheduled // 100 == 24 and scheduled != 2400):
                            raise ValueError("Invalid scheduled time")
                        cancelled, diverted = float(row["cancelled"]), float(row["diverted"])
                        if cancelled not in (0, 1) or diverted not in (0, 1):
                            raise ValueError("Missing cancellation/diversion flags")
                        delay = float(row["delay"]) if row["delay"] else None
                        if delay is not None and not math.isfinite(delay):
                            raise ValueError("Non-finite delay")
                        carrier, origin, destination = row["carrier"].upper(), row["origin"].upper(), row["destination"].upper()
                        if not carrier or not re.fullmatch(r"[A-Z]{3}", origin) or not re.fullmatch(r"[A-Z]{3}", destination):
                            raise ValueError("Missing flight identity")
                        if marketing:
                            marketing_number = number(row["marketing_number"])
                            marketing_carrier = row["marketing_carrier"].upper()
                            if not re.fullmatch(r"[A-Z0-9]{2}", marketing_carrier):
                                raise ValueError("Missing marketing airline")
                    except (ValueError, OverflowError):
                        rejected += 1
                        continue
                    operation = (flight_date.isoformat(), carrier, flight_number, origin, destination,
                                 f"{scheduled:04d}", (scheduled // 100) % 24, delay, int(cancelled), int(diverted))
                    batch.append(operation + ((marketing_carrier, marketing_number) if marketing else ()))
                    if len(batch) >= 5000:
                        flush()
                if batch:
                    flush()
                if source_month and inserted + duplicates == 0:
                    raise DataError("The BTS month contains no usable flight records.")
                imports = "marketing_imports" if marketing else "imports"
                db.execute(f"INSERT INTO {imports} VALUES (?, ?, ?, ?)", (path.name, utc_now(), inserted, rejected))
                if source_month:
                    ledger = "marketing_synced_months" if marketing else "synced_months"
                    stored_rows = inserted + duplicates
                    if marketing:
                        from .marketing import next_month
                        stored_rows = db.execute("SELECT COUNT(*) FROM marketing_flights WHERE date>=? AND date<?",
                                                 (source_month + "-01", next_month(source_month))).fetchone()[0]
                    entry = (source_month, path.name, checksum, utc_now(), stored_rows, rejected)
                    if marketing:
                        entry += (excluded_duplicates,)
                    db.execute(f"INSERT OR REPLACE INTO {ledger} VALUES ({','.join('?' for _ in entry)})", entry)
        return {"source": f"BTS {dataset.title()} Carrier On-Time Performance", "file": path.name,
                "inserted": inserted, "duplicates": duplicates, "rejected": rejected,
                "excluded_source_duplicates": excluded_duplicates}

    def coverage(self):
        with closing(sqlite3.connect(self.path, timeout=30)) as db:
            months = db.execute("SELECT month, rows, rejected FROM synced_months ORDER BY month").fetchall()
            dates = db.execute("SELECT (SELECT MIN(date) FROM flights), (SELECT MAX(date) FROM flights)").fetchone()
            has_records = bool(dates[0])
            marketing = db.execute("SELECT month, rows, rejected, excluded_duplicates, imported_at FROM marketing_synced_months ORDER BY month").fetchall()
        return {"has_records": has_records, "first_date": dates[0], "last_date": dates[1],
                "loaded_months": [row[0] for row in months],
                "synced_records": sum(row[1] for row in months), "rejected_records": sum(row[2] for row in months),
                "marketing": {"loaded_months": [row[0] for row in marketing],
                              "synced_records": sum(row[1] for row in marketing),
                              "rejected_records": sum(row[2] for row in marketing),
                              "excluded_source_duplicates": sum(row[3] for row in marketing),
                              "last_imported_at": max((row[4] for row in marketing), default=None)}}

    @staticmethod
    def summarize(rows):
        eligible = [row for row in rows if not row[3] and not row[4] and row[2] is not None]
        delays = [row[2] for row in eligible]
        delayed = sum(value >= 15 for value in delays)
        return {"scheduled_flights": len(rows), "completed_with_arrival_delay": len(eligible),
                "arrival_timing": arrival_timing(delays),
                "delayed_arrivals_15_minutes_or_more": delayed,
                "historical_delay_rate": delayed / len(eligible) if eligible else None,
                "mean_arrival_lateness_minutes": sum(max(value, 0) for value in delays) / len(delays) if delays else None,
                "cancelled_flights": sum(row[3] for row in rows),
                "diverted_flights": sum(row[4] for row in rows),
                "completed_without_delay_measurement": sum(not r[3] and not r[4] and r[2] is None for r in rows)}

    def report(self, carrier: str, flight_number: str, origin: str, destination: str,
               *, as_of: date, months: int = 24, departure_hour: int | None = None,
               match_carrier: bool = False):
        if not 1 <= months <= 24 or (departure_hour is not None and not 0 <= departure_hour <= 23):
            raise DataError("Use 1–24 history months and a departure hour from 0–23.")
        try:
            flight_number = number(flight_number)
        except (ValueError, OverflowError):
            raise DataError("Use the numeric reporting-airline flight number.") from None
        identity = (carrier.strip().upper(), flight_number, origin.strip().upper(), destination.strip().upper())
        result = {"source": "BTS Reporting Carrier On-Time Performance", "generated_at": utc_now(),
                  "available": False, "requested_months": months, "as_of": as_of.isoformat(),
                  "identity": dict(zip(("requested_airline", "flight_number", "origin", "destination"), identity)),
                  "match_method": "reporting_carrier_number_route" if match_carrier else "flight_number_route",
                  "notice": "Observed arrival delay rates are not a weather-adjusted forecast. Matching a numeric flight number and route does not verify the operating airline or a codeshare relationship."}
        with closing(sqlite3.connect(self.path)) as db:
            latest = db.execute("""SELECT MAX(date) FROM (
                SELECT MAX(date) AS date FROM flights WHERE date < ? UNION ALL
                SELECT MAX(date) AS date FROM marketing_flights WHERE date < ?)""",
                (as_of.isoformat(), as_of.isoformat())).fetchone()[0]
            if not latest:
                return {**result, "reason_code": "history_not_loaded", "reason": "No flight-level BTS history has been imported for dates before this trip."}
            latest_date = date.fromisoformat(latest)
            month_index = latest_date.year * 12 + latest_date.month - 1 - (months - 1)
            start = date(month_index // 12, month_index % 12 + 1, 1)
            if not match_carrier:
                from .marketing import marketing_report
                matched = marketing_report(db, result, start, as_of, departure_hour)
                if matched is not None:
                    return matched
            conditions = "number = ? AND origin = ? AND destination = ? AND date >= ? AND date < ?"
            parameters = (*identity[1:], start.isoformat(), as_of.isoformat())
            if match_carrier:
                conditions += " AND carrier = ?"
                parameters += (identity[0],)
            rows = db.execute(f"SELECT date, hour, delay, cancelled, diverted, carrier FROM flights WHERE {conditions} ORDER BY date",
                              parameters).fetchall()
        if departure_hour is not None:
            rows = [row for row in rows if row[1] == departure_hour]
        if not rows:
            return {**result, "reason_code": "flight_not_matched", "reason": "History is loaded, but no records match this numeric flight number, route, and optional departure hour."}
        carriers = sorted({row[5] for row in rows})
        if len(carriers) > 1:
            candidates = []
            for code in carriers:
                group = [row for row in rows if row[5] == code]
                candidates.append({"reporting_carrier": code, "observed_first_date": group[0][0],
                                   "observed_last_date": group[-1][0], "overall": self.summarize(group)})
            return {**result, "reason_code": "ambiguous_flight_number",
                    "reason": "Several reporting airlines used this flight number on this route. Their histories are shown separately; the selected flight's operating airline has not been verified.",
                    "carrier_candidates": candidates, "window_start": start.isoformat(), "dataset_latest_date": latest}
        coverage = sorted({row[0][:7] for row in rows})
        return {**result, "available": True, "reason_code": "history_matched", "window_start": start.isoformat(), "dataset_latest_date": latest,
                "matched_reporting_carrier": carriers[0],
                "observed_first_date": rows[0][0], "observed_last_date": rows[-1][0],
                "months_with_observations": coverage, "departure_hour_filter": departure_hour,
                "overall": self.summarize(rows),
                "same_calendar_month": self.summarize([row for row in rows if date.fromisoformat(row[0]).month == as_of.month])}
