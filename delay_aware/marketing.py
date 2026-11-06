"""Resolve historical marketed flights using explicit BTS operating identities."""

from collections import defaultdict
from datetime import date
import sqlite3

from .history import FlightHistory

# This is an operation-level join, never a join on flight number alone.
JOIN = """r.date=m.date AND r.carrier=m.carrier AND r.number=m.number
          AND r.origin=m.origin AND r.destination=m.destination AND r.scheduled=m.scheduled"""
CONFLICT = """(r.cancelled!=m.cancelled OR r.diverted!=m.diverted OR
              (r.delay IS NOT NULL AND m.delay IS NOT NULL AND ABS(r.delay-m.delay)>0.000001))"""


def marketing_report(db, base, start, as_of, departure_hour):
    identity = base["identity"]
    cursor = db.cursor()
    cursor.row_factory = sqlite3.Row
    records = cursor.execute(f"""SELECT m.*, r.date AS reporting_date, r.delay AS reporting_delay,
        r.cancelled AS reporting_cancelled, r.diverted AS reporting_diverted
        FROM marketing_flights m LEFT JOIN flights r ON {JOIN}
        WHERE m.marketing_carrier=? AND m.marketing_number=? AND m.origin=? AND m.destination=?
        AND m.date>=? AND m.date<? ORDER BY m.date,m.scheduled,m.carrier,m.number""",
        (identity["requested_airline"], identity["flight_number"], identity["origin"],
         identity["destination"], start.isoformat(), as_of.isoformat())).fetchall()
    if departure_hour is not None:
        records = [row for row in records if row["hour"] == departure_hour]
    if not records:
        return None

    occurrences = defaultdict(list)
    for row in records:
        occurrences[(row["date"], row["scheduled"])].append(row)
    observations, services = [], defaultdict(list)
    matched = marketing_only = conflicts = ambiguous = 0
    for group in occurrences.values():
        if len(group) != 1:
            # Do not silently blend conflicting operators for one marketed departure.
            ambiguous += 1
            continue
        row = group[0]
        reporting = row["reporting_date"] is not None
        if reporting and (row["cancelled"] != row["reporting_cancelled"]
                          or row["diverted"] != row["reporting_diverted"]
                          or (row["delay"] is not None and row["reporting_delay"] is not None
                              and abs(row["delay"] - row["reporting_delay"]) > 0.000001)):
            conflicts += 1
            continue
        if reporting:
            matched += 1
        else:
            marketing_only += 1
        delay = row["reporting_delay"] if reporting and row["reporting_delay"] is not None else row["delay"]
        observation = (row["date"], row["hour"], delay, row["cancelled"], row["diverted"], row["carrier"])
        observations.append(observation)
        services[(row["carrier"], row["number"])].append(observation)

    result = {**base, "source": ("BTS Marketing Carrier + Reporting Carrier On-Time Performance"
                                if matched else "BTS Marketing Carrier On-Time Performance"),
              "match_method": "bts_marketing_operating_key", "window_start": start.isoformat(),
              "historical_identity_verified": bool(observations), "identity_verified": False,
              "join_key": ["flight_date", "operating_airline", "operating_flight_number",
                           "origin", "destination", "scheduled_departure_local"],
              "join_coverage": {"marketing_records_considered": len(records),
                                "matched_reporting_operations": matched,
                                "marketing_only_operations": marketing_only,
                                "outcome_conflicts_excluded": conflicts,
                                "ambiguous_departures_excluded": ambiguous},
              "operating_services": [
                  {"operating_carrier": carrier, "operating_flight_number": number,
                   "observed_first_date": rows[0][0], "observed_last_date": rows[-1][0],
                   "overall": FlightHistory.summarize(rows)}
                  for (carrier, number), rows in sorted(services.items())],
              "departure_hour_filter": departure_hour,
              "notice": "BTS explicitly links the marketing and operating identities of these historical operations. This does not verify the operator of a future departure or predict its delay. Marketing-only observations are identified separately; conflicting records are excluded."}
    if not observations:
        return {**result, "reason_code": "marketing_records_conflicted",
                "reason": "Marketing records exist, but their outcomes or operating identities conflict. No combined delay rate is produced."}
    return {**result, "available": True, "reason_code": "marketing_history_matched",
            "dataset_latest_date": max(row[0] for row in observations),
            "observed_first_date": observations[0][0], "observed_last_date": observations[-1][0],
            "months_with_observations": sorted({row[0][:7] for row in observations}),
            "matched_reporting_carrier": next(iter(services))[0] if matched and len({key[0] for key in services}) == 1 else None,
            "overall": FlightHistory.summarize(observations),
            "same_calendar_month": FlightHistory.summarize([
                row for row in observations if date.fromisoformat(row[0]).month == as_of.month])}


def audit_join(path, month):
    """Reconcile physical operations for one month, without multiplying codeshares."""
    with sqlite3.connect(path, timeout=30) as db:
        rows = db.execute(f"""WITH m AS (
            SELECT DISTINCT date,carrier,number,origin,destination,scheduled,delay,cancelled,diverted
            FROM marketing_flights WHERE date>=? AND date<?)
            SELECT COUNT(*), COALESCE(SUM(r.date IS NOT NULL),0),
                COALESCE(SUM(r.date IS NULL),0),
                COALESCE(SUM(r.date IS NOT NULL AND {CONFLICT}),0)
            FROM m LEFT JOIN flights r ON {JOIN}""", (month + "-01", next_month(month))).fetchone()
        stored = db.execute("SELECT COUNT(*) FROM marketing_flights WHERE date>=? AND date<?",
                            (month + "-01", next_month(month))).fetchone()[0]
        reporting_only = db.execute(f"""SELECT COUNT(*) FROM flights r
            WHERE r.date>=? AND r.date<? AND NOT EXISTS
            (SELECT 1 FROM marketing_flights m WHERE {JOIN})""",
            (month + "-01", next_month(month))).fetchone()[0]
    return {"month": month, "marketing_relationships": stored, "distinct_operating_records": rows[0],
            "matched_reporting_operations": rows[1], "marketing_only_operations": rows[2],
            "outcome_conflicts": rows[3], "reporting_only_operations": reporting_only}


def next_month(month):
    year, value = map(int, month.split("-"))
    return f"{year + (value == 12):04d}-{value % 12 + 1:02d}-01"
