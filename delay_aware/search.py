"""One-way nonstop and one-connection discovery through SerpApi."""

from datetime import date

from .core import DataError, utc_now
from .itineraries import ITINERARY_TYPES, journey_key, normalize_offer


class FlightSearch:
    def __init__(self, settings, transport, store, airports):
        self.settings, self.transport, self.store, self.airports = settings, transport, store, airports

    def search(self, origin: str, destination: str, departure: date, *, today: date,
               adults: int = 1, max_price: int | None = None, itinerary_type: str = "all"):
        return self.search_airports([origin], [destination], departure, today=today,
                                    adults=adults, max_price=max_price, itinerary_type=itinerary_type)

    def search_airports(self, origins: list[str], destinations: list[str], departure: date,
                        *, today: date, adults: int = 1, max_price: int | None = None,
                        itinerary_type: str = "all"):
        if itinerary_type not in ITINERARY_TYPES:
            raise DataError("Choose All, Nonstop only, or One connection only.")
        origins = list(dict.fromkeys(code.strip().upper() for code in origins))
        destinations = list(dict.fromkeys(code.strip().upper() for code in destinations))
        if not origins or not destinations or len(origins) > 30 or len(destinations) > 30:
            raise DataError("Choose between 1 and 30 airports at either end.")
        if len(origins) == len(destinations) == 1 and origins == destinations:
            raise DataError("Departure and arrival airports must differ.")
        if not 0 <= (departure - today).days <= 30:
            raise DataError("Choose a departure date from today through 30 days ahead.")
        if type(adults) is not int or not 1 <= adults <= 9:
            raise DataError("Choose between 1 and 9 adult passengers.")
        if max_price is not None and (type(max_price) is not int or max_price <= 0):
            raise DataError("Maximum ticket price must be a positive whole-dollar USD amount.")
        supported = self.airports.load()
        if any(code not in supported for code in origins + destinations):
            raise DataError("Choose a supported US airport with scheduled passenger service.")
        params = {"engine": "google_flights", "departure_id": ",".join(origins), "arrival_id": ",".join(destinations),
                  "outbound_date": departure.isoformat(), "type": 2, "stops": 1 if itinerary_type == "nonstop" else 2,
                  "adults": adults, "travel_class": 1, "currency": "USD", "gl": "us", "hl": "en", "show_hidden": "true"}
        if max_price is not None:
            params["max_price"] = max_price
        cache_key = self.store.key({"schema": "journeys-v1", "itinerary_type": itinerary_type, **params})
        cached = self.store.get(cache_key, ttl=900)
        if cached is not None:
            return {**cached, "cached": True}
        if not self.settings.serpapi_key:
            raise DataError("Set SERPAPI_API_KEY in the local .env file to enable live flight search.")
        self.store.reserve_search(self.settings.search_limit)
        raw = self.transport.get_json("https://serpapi.com/search.json", {**params, "api_key": self.settings.serpapi_key})
        if not isinstance(raw, dict) or raw.get("error") or raw.get("search_metadata", {}).get("status") != "Success":
            raise DataError("Flight search did not complete successfully. No flight results were substituted.")
        options, seen = [], set()
        for group in ("best_flights", "other_flights"):
            offers = raw.get(group)
            for offer in offers if isinstance(offers, list) else []:
                option = normalize_offer(offer, origins, destinations, departure, supported, itinerary_type, max_price)
                if option is None:
                    continue
                identity = journey_key(option)
                if identity in seen:
                    continue
                seen.add(identity)
                options.append(option)
        result = {"source": "SerpApi Google Flights results", "fetched_at": utc_now(),
                  "provider_created_at": raw.get("search_metadata", {}).get("created_at"), "cached": False,
                  "query": {**params, "itinerary_type": itinerary_type}, "flights": options,
                  "notice": "Returned options may not cover every available journey. Connecting ticket types are checked on selection; separate tickets are excluded when identified. Fares can change. All times are local to each airport."}
        self.store.put(cache_key, result)
        return result
