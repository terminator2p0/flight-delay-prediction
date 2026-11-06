"""Bounded FlightAware lookups with exact departure matching and gate-time evidence."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import re
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import DataError, ProviderError
from .timing import arrival_timing

SOURCE = "FlightAware AeroAPI"
URL = "https://aeroapi.flightaware.com/aeroapi/flights/"


def timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None and result.utcoffset() is not None else None
    except ValueError:
        return None


def designator(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"([A-Z0-9]{2})\s*(\d{1,5})", value.strip().upper())
    return match[1] + str(int(match[2])) if match else None


def normalize(raw):
    if not isinstance(raw, dict) or raw.get("blocked") is True or raw.get("position_only") is True:
        return None
    origin, destination = raw.get("origin"), raw.get("destination")
    if not isinstance(origin, dict) or not isinstance(destination, dict):
        return None
    dep = timestamp(raw.get("scheduled_out"))
    flight_id = raw.get("fa_flight_id")
    if not dep or not isinstance(flight_id, str) or not flight_id or len(flight_id) > 200:
        return None
    if any(not isinstance(airport.get("code_iata"), str) or not re.fullmatch(r"[A-Z]{3}", airport["code_iata"]) for airport in (origin, destination)):
        return None
    try:
        origin_zone = ZoneInfo(origin["timezone"])
        destination_zone = ZoneInfo(destination["timezone"])
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        return None
    ident = designator(raw.get("ident_iata"))
    carrier, number = raw.get("operator_iata"), str(raw.get("flight_number", ""))
    operating = designator(carrier + number) if isinstance(carrier, str) and number.isdigit() else None
    # An IATA ident is documented as the operating designator. Do not reconcile
    # contradictory identity fields by guessing which one is authoritative.
    identity_conflict = bool(operating and ident and operating != ident)
    operating = operating or ident
    codeshares = raw.get("codeshares_iata")
    codeshares = codeshares[:100] if isinstance(codeshares, list) else []
    aliases = {item for item in (designator(v) for v in codeshares if isinstance(v, str)) if item}
    if ident:
        aliases.add(ident)
    if operating:
        aliases.add(operating)
    result = {"flight_id": flight_id, "ident_iata": ident, "operating_ident": operating,
              "identity_conflict": identity_conflict, "aliases": sorted(aliases),
              "origin": origin["code_iata"], "destination": destination["code_iata"],
              "origin_timezone": str(origin_zone), "destination_timezone": str(destination_zone),
              "scheduled_departure_local": dep.astimezone(origin_zone).isoformat(),
              "status": raw.get("status")[:160] if isinstance(raw.get("status"), str) else None,
              "cancelled_or_untracked": raw.get("cancelled") if type(raw.get("cancelled")) is bool else None,
              "diverted": raw.get("diverted") if type(raw.get("diverted")) is bool else None}
    for key in ("scheduled_out", "estimated_out", "actual_out", "scheduled_in", "estimated_in", "actual_in"):
        value = timestamp(raw.get(key))
        result[key] = value.astimezone(timezone.utc).isoformat() if value else None
    for event, zone in (("out", origin_zone), ("in", destination_zone)):
        for kind in ("scheduled", "estimated", "actual"):
            value = timestamp(result[f"{kind}_{event}"])
            result[f"{kind}_{event}_local"] = value.astimezone(zone).isoformat() if value else None
    return result


def gate_delay(record, event, kind):
    actual, scheduled = timestamp(record.get(f"{kind}_{event}")), timestamp(record.get(f"scheduled_{event}"))
    return (actual - scheduled).total_seconds() / 60 if actual and scheduled else None


def recent_summary(records, now):
    # A recently scheduled flight is not an observed outcome. Only elapsed,
    # non-conflicting operations with explicit gate times can supply delay rates.
    past = [r for r in records if now - timedelta(days=9) <= timestamp(r["scheduled_out"]) < now]
    grouped = defaultdict(list)
    for record in past:
        grouped[record["operating_ident"] or "Unknown operator"].append(record)
    groups = []
    for ident, rows in sorted(grouped.items()):
        eligible = [r for r in rows if r["cancelled_or_untracked"] is False and r["diverted"] is False
                    and timestamp(r.get("actual_in")) and timestamp(r["actual_in"]) <= now
                    and timestamp(r.get("scheduled_in"))]
        delays = [gate_delay(r, "in", "actual") for r in eligible]
        groups.append({"operating_ident": ident, "observed_operations": len(rows),
                       "arrival_timing": arrival_timing(delays),
                       "first_measured_departure": min((r["scheduled_departure_local"] for r in eligible), default=None),
                       "last_measured_departure": max((r["scheduled_departure_local"] for r in eligible), default=None),
                       "latest_arrivals": [{"departure_date": r["scheduled_departure_local"][:10],
                                            "scheduled_arrival_local": r["scheduled_in_local"],
                                            "actual_arrival_local": r["actual_in_local"],
                                            "minutes": gate_delay(r, "in", "actual")}
                                           for r in sorted(eligible, key=lambda r: timestamp(r["actual_in"]), reverse=True)[:5]],
                       "measured_gate_arrivals": len(delays), "arrived_15_minutes_or_more_late": sum(d >= 15 for d in delays),
                       "observed_delay_rate": sum(d >= 15 for d in delays) / len(delays) if delays else None,
                       "mean_lateness_minutes": sum(max(d, 0) for d in delays) / len(delays) if delays else None,
                       "cancelled_or_untracked": sum(r["cancelled_or_untracked"] is True for r in rows),
                       "diverted": sum(r["diverted"] is True for r in rows),
                       "first_departure": min(r["scheduled_out"] for r in rows),
                       "last_departure": max(r["scheduled_out"] for r in rows)})
    return {"available": bool(past), "groups": groups,
            "notice": "Recent returned operations only, not complete long-term history. Delay rates use actual gate arrivals, not estimates or runway times. FlightAware's cancelled flag can also mean no longer tracked, so it is not a confirmed airline cancellation count."}


class AeroAPI:
    def __init__(self, settings, transport, store, *, clock=time.time):
        self.settings, self.transport, self.store, self.clock = settings, transport, store, clock
        self.cache, self.failures, self.lock = {}, {}, threading.Lock()

    def unavailable(self, ident, result):
        with self.lock:
            self.failures = {key: item for key, item in self.failures.items() if 0 <= self.clock() - item[0] < 60}
            if len(self.failures) >= 100:
                del self.failures[next(iter(self.failures))]
            self.failures[ident] = (self.clock(), result)
        return result

    def lookup(self, flight, *, now=None):
        now = now or datetime.now(timezone.utc)
        base = {"source": SOURCE, "available": False, "selected_flight_verified": False,
                "fetched_at": None, "cached": False, "recent": {"available": False, "groups": []}}
        if not self.settings.aeroapi_key:
            return {**base, "reason_code": "not_configured", "reason": "Add AEROAPI_API_KEY to the local .env file to enable FlightAware. BTS history remains available."}
        ident = designator(flight.get("flight_number"))
        try:
            selected = datetime.fromisoformat(flight["departure_local"])
        except (KeyError, TypeError, ValueError):
            selected = None
        if not ident or not selected:
            return {**base, "reason_code": "invalid_selection", "reason": "The selected flight identifier or departure time cannot be checked."}
        try:
            with self.lock:
                failure = self.failures.get(ident)
                if failure and 0 <= self.clock() - failure[0] < 60:
                    return {**failure[1], "cached": True}
                saved = self.cache.get(ident)
                if saved and 0 <= self.clock() - saved[0] < 900:
                    data, cached = saved[1], True
                else:
                    self.store.reserve_aeroapi(self.settings.aeroapi_limit)
                    raw = self.transport.get_json(URL + ident,
                        {"ident_type": "designator", "start": (now - timedelta(days=9)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "end": (now + timedelta(hours=47)).strftime("%Y-%m-%dT%H:%M:%SZ"), "max_pages": 1},
                        headers={"x-apikey": self.settings.aeroapi_key}, timeout=15)
                    if not isinstance(raw, dict) or not isinstance(raw.get("flights"), list):
                        raise DataError("FlightAware returned an unexpected response. No flight data was substituted.")
                    if len(raw["flights"]) > 100:
                        raise DataError("FlightAware returned more records than the bounded lookup supports.")
                    records = [record for item in raw["flights"] if (record := normalize(item))]
                    links = raw.get("links")
                    data = {"records": records, "fetched_at": now.isoformat(),
                            "partial": bool(links.get("next")) if isinstance(links, dict) else False}
                    # Flight payloads stay in memory only; the persistent ledger
                    # stores request timestamps, never keys or provider payloads.
                    self.cache = {key: value for key, value in self.cache.items() if 0 <= self.clock() - value[0] < 900}
                    if len(self.cache) >= 100:
                        del self.cache[next(iter(self.cache))]
                    self.cache[ident] = (self.clock(), data)
                    cached = False
        except ProviderError as error:
            reason = {401: "FlightAware did not accept the API key. Check the local key.",
                      400: "FlightAware could not process this flight lookup.",
                      402: "FlightAware requires account access or API credit for this lookup. Check its account dashboard.",
                      403: "This lookup is not allowed by the FlightAware account. Check its API access and plan.",
                      404: "FlightAware did not return this flight identifier.",
                      429: "FlightAware's request limit was reached. Try again later."}.get(error.status, "FlightAware is temporarily unavailable.")
            return self.unavailable(ident, {**base, "reason_code": "provider_error", "provider_status": error.status,
                                           "reason": reason + " BTS history remains available."})
        except DataError as error:
            return self.unavailable(ident, {**base, "reason_code": "lookup_unavailable", "reason": str(error)})

        route_records = [r for r in data["records"] if ident in r["aliases"]
                         and r["origin"] == flight["origin"] and r["destination"] == flight["destination"]]
        unique, conflicts = {}, set()
        for record in route_records:
            # The ID identifies a physical flight instance. A second version
            # with a changed schedule is a conflict, not an additional flight.
            # Diversion legs on other routes have already been filtered out.
            key = record["flight_id"]
            if record["identity_conflict"] or (key in unique and unique[key] != record):
                conflicts.add(key)
            unique[key] = record
        records = [r for key, r in unique.items() if key not in conflicts]
        matches = []
        for record in records:
            departure = datetime.fromisoformat(record["scheduled_departure_local"])
            same_time = (departure.replace(tzinfo=None, second=0, microsecond=0) == selected.replace(second=0, microsecond=0)
                         if selected.tzinfo is None else abs((departure - selected).total_seconds()) < 60)
            if same_time and record["operating_ident"]:
                matches.append(record)
        recent = recent_summary(records, now)
        result = {**base, "fetched_at": data["fetched_at"], "cached": cached,
                  "recent": recent, "partial_results": data["partial"], "conflicting_records_excluded": len(conflicts),
                  "available": bool(matches or recent["available"])}
        if len(matches) == 1:
            chosen = matches[0]
            return {**result, "selected_flight_verified": True, "reason_code": "selected_departure_matched",
                    "selected": {**chosen, "actual_arrival_delay_minutes": gate_delay(chosen, "in", "actual"),
                                 "estimated_arrival_delay_minutes": gate_delay(chosen, "in", "estimated"),
                                 "actual_departure_delay_minutes": gate_delay(chosen, "out", "actual"),
                                 "estimated_departure_delay_minutes": gate_delay(chosen, "out", "estimated")},
                    "reason": "FlightAware matched the selected flight or an explicit codeshare, route, and scheduled local departure minute."}
        if len(matches) > 1:
            return {**result, "reason_code": "ambiguous_departure", "reason": "Several FlightAware flights match this departure. The selected operating flight remains unverified."}
        return {**result, "reason_code": "selected_departure_not_found",
                "reason": "The selected departure was not returned by FlightAware. Recent same-flight/route observations are separate historical evidence. This lookup covers only roughly two days ahead; a missing match does not mean the flight is cancelled."}
