"""Read ticket evidence for a selected journey; never follow booking actions."""

from datetime import date

from .aeroapi import designator
from .core import DataError, utc_now
from .itineraries import amount, journey_key, legs_for, normalize_offer, split_ticket_evidence


class TicketCheck:
    def __init__(self, settings, transport, store, airports):
        self.settings, self.transport, self.store, self.airports = settings, transport, store, airports

    def check(self, flight, query):
        if len(legs_for(flight)) == 1:
            return {"status": "not_applicable", "eligible": True, "notice": "Nonstop journey."}
        base = {"source": "SerpApi Google Flights booking options", "status": "unavailable", "eligible": None,
                "fetched_at": utc_now(), "notice": "Single-ticket eligibility could not be established. Do not assume these flights are sold on one ticket."}
        token = flight.get("booking_token")
        if not isinstance(token, str) or not token or not self.settings.serpapi_key:
            return base
        params = {key: query[key] for key in ("engine", "departure_id", "arrival_id", "outbound_date", "type", "adults", "travel_class", "currency", "gl", "hl") if key in query}
        params.setdefault("outbound_date", flight["departure_local"][:10])
        params["booking_token"] = token
        key = self.store.key({"schema": "ticket-check-v1", "max_price": query.get("max_price"), "journey": journey_key(flight), **params})
        cached = self.store.get(key, ttl=900)
        if cached is not None:
            return {**cached, "cached": True}
        failure = self.store.get(key + ":failure", ttl=60)
        if failure is not None:
            return {**failure, "cached": True}
        try:
            self.store.reserve_search(self.settings.search_limit)
            raw = self.transport.get_json("https://serpapi.com/search.json", {**params, "api_key": self.settings.serpapi_key})
            if not isinstance(raw, dict) or raw.get("error") or raw.get("search_metadata", {}).get("status") != "Success":
                raise DataError("Ticket details could not be retrieved.")
            result = self.normalize(raw, flight, query, base)
        except DataError as error:
            result = {**base, "reason": str(error)}
        self.store.put(key + ":failure" if result["status"] == "unavailable" else key, result)
        return result

    def normalize(self, raw, flight, query, base):
        selected = raw.get("selected_flights")
        if not isinstance(selected, list) or len(selected) != 1 or not isinstance(selected[0], dict):
            return base
        # Bind the booking evidence to both exact legs and their scheduled times.
        selected_offer = {**selected[0], "layovers": selected[0].get("layovers", [
            {"id": l["airport"], "duration": l["duration_minutes"]} for l in flight.get("layovers", [])])}
        normalized = normalize_offer(selected_offer, [flight["origin"]], [flight["destination"]],
                                     date.fromisoformat(flight["departure_local"][:10]),
                                     self.airports.load(), "one_connection", None)
        if normalized is None or journey_key(normalized) != journey_key(flight):
            return {**base, "reason": "Returned ticket details did not match both selected flights and times."}
        required = [designator(leg["flight_number"]) for leg in legs_for(flight)]
        options = raw.get("booking_options")
        if not isinstance(options, list):
            return base
        together_offers, split_count = [], 0
        for option in options:
            if not isinstance(option, dict):
                continue
            if "separate_tickets" in option and type(option["separate_tickets"]) is not bool:
                continue
            if (split_ticket_evidence(option) or "departing" in option or "returning" in option
                    or split_ticket_evidence(option.get("together"))):
                split_count += 1
                continue
            together = option.get("together")
            if not isinstance(together, dict) or split_ticket_evidence(together):
                continue
            marketed = together.get("marketed_as")
            if (not all(required) or not isinstance(marketed, list)
                    or [designator(n) for n in marketed] != required
                    or not isinstance(together.get("book_with"), str) or not together["book_with"]):
                continue
            price = amount(together.get("price"))
            if price is None:
                continue
            together_offers.append({"seller": together["book_with"], "price": price, "currency": "USD",
                                    "fare_name": together.get("option_title") if isinstance(together.get("option_title"), str) else None})
        if together_offers:
            cap = query.get("max_price")
            affordable = [o for o in together_offers if cap is None or o["price"] <= cap]
            if not affordable:
                return {**base, "status": "over_budget", "eligible": False,
                        "lowest_together_price": min(o["price"] for o in together_offers),
                        "notice": "The returned sold-together fares exceed your maximum fare. This journey is excluded from eligible options."}
            best = min(affordable, key=lambda o: o["price"])
            return {**base, "status": "sold_together", "eligible": True, **best,
                    "notice": "The provider returned a sold-together offer covering both selected flights. Confirm ticket terms and the final fare with the seller; no ticket has been issued."}
        if options and split_count == len(options):
            return {**base, "status": "separate_tickets", "eligible": False,
                    "notice": "Only separate-ticket options were returned. This journey is outside the supported single-ticket scope."}
        return base
