"""Shared journey identity and conservative search-result normalization."""

from datetime import datetime
import math


ITINERARY_TYPES = ("all", "nonstop", "one_connection")


def amount(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def legs_for(flight):
    return flight.get("legs") or [flight]


def journey_key(flight):
    return tuple((leg["flight_number"], leg["origin"], leg["destination"],
                  leg["departure_local"], leg.get("arrival_local")) for leg in legs_for(flight))


def split_ticket_evidence(value):
    if not isinstance(value, dict):
        return False
    if any(value.get(key) is True for key in ("separate_tickets", "self_transfer", "airport_change")):
        return True
    extensions = value.get("extensions", [])
    return isinstance(extensions, list) and any(
        isinstance(text, str) and any(term in text.lower() for term in
        ("separate ticket", "self-transfer", "self transfer", "airport change")) for text in extensions)


def normalize_offer(offer, origins, destinations, departure, supported, itinerary_type, max_price):
    if not isinstance(offer, dict) or split_ticket_evidence(offer):
        return None
    raw_legs = offer.get("flights")
    if not isinstance(raw_legs, list) or len(raw_legs) not in (1, 2):
        return None
    if (itinerary_type == "nonstop" and len(raw_legs) != 1
            or itinerary_type == "one_connection" and len(raw_legs) != 2):
        return None
    legs = []
    for raw in raw_legs:
        if not isinstance(raw, dict) or split_ticket_evidence(raw):
            return None
        dep, arr = raw.get("departure_airport"), raw.get("arrival_airport")
        if not isinstance(dep, dict) or not isinstance(arr, dict):
            return None
        if (not isinstance(dep.get("id"), str) or not isinstance(arr.get("id"), str)
                or dep["id"] not in supported or arr["id"] not in supported or dep["id"] == arr["id"]):
            return None
        number = raw.get("flight_number")
        if not isinstance(number, str) or not number.strip() or len(number) > 40:
            return None
        try:
            out, arrival = datetime.fromisoformat(dep["time"]), datetime.fromisoformat(arr["time"])
        except (ValueError, KeyError, TypeError):
            return None
        legs.append({"flight_number": number.strip(), "airline": raw.get("airline") if isinstance(raw.get("airline"), str) else None,
                     "origin": dep["id"], "destination": arr["id"],
                     "departure_local": out.isoformat(), "arrival_local": arrival.isoformat(),
                     "duration_minutes": amount(raw.get("duration")), "history_match": "unverified"})
    first, last = legs[0], legs[-1]
    if (first["origin"] not in origins or last["destination"] not in destinations
            or first["origin"] == last["destination"]
            or datetime.fromisoformat(first["departure_local"]).date() != departure):
        return None
    layovers = []
    if len(legs) == 2:
        if first["destination"] != last["origin"]:
            return None
        raw_layovers = offer.get("layovers")
        if not isinstance(raw_layovers, list) or len(raw_layovers) != 1 or not isinstance(raw_layovers[0], dict):
            return None
        layover = raw_layovers[0]
        minutes = amount(layover.get("duration"))
        if layover.get("id") != first["destination"] or minutes is None or minutes <= 0:
            return None
        # Keep provider elapsed time: local subtraction can differ across DST.
        arrival, out = datetime.fromisoformat(first["arrival_local"]), datetime.fromisoformat(last["departure_local"])
        try:
            wall_minutes = (out - arrival).total_seconds() / 60
        except TypeError:
            return None
        if not any(math.isclose(wall_minutes - minutes, shift, abs_tol=0.001) for shift in (-60, 0, 60)):
            return None
        layovers.append({"airport": first["destination"], "duration_minutes": minutes,
                         "arrival_local": first["arrival_local"], "departure_local": last["departure_local"],
                         "overnight": out.date() > arrival.date(), "duration_basis": "provider_reported",
                         "minimum_connection_time_verified": False})
    price = amount(offer.get("price"))
    if max_price is not None and (price is None or price > max_price):
        return None
    total = amount(offer.get("total_duration"))
    if total is None and all(leg["duration_minutes"] is not None for leg in legs):
        total = sum(leg["duration_minutes"] for leg in legs) + sum(l["duration_minutes"] for l in layovers)
    result = {"flight_number": " + ".join(leg["flight_number"] for leg in legs),
              "airline": " / ".join(dict.fromkeys(leg["airline"] for leg in legs if leg["airline"])) or None,
              "origin": first["origin"], "destination": last["destination"],
              "departure_local": first["departure_local"], "arrival_local": last["arrival_local"],
              "duration_minutes": total, "price": price, "currency": "USD", "history_match": "unverified",
              "legs": legs, "layovers": layovers, "stops": len(legs) - 1,
              "ticket_status": "not_checked" if layovers else "not_applicable"}
    token = offer.get("booking_token")
    if layovers and isinstance(token, str) and 0 < len(token) <= 16000:
        result["booking_token"] = token
    return result
