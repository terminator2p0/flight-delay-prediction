"""Application services shared by the local browser and the assistant's tools."""

from datetime import date, datetime, timedelta
import json
import re
import secrets
import threading
import time
from zoneinfo import ZoneInfo

from .agent import MODELS, Ollama, answer
from .aeroapi import AeroAPI
from .assessment import combine
from .airports import Airports
from .core import DataError, LocalStore, Transport
from .history import FlightHistory
from .history_sync import sync_status
from .itineraries import journey_key, legs_for
from .search import FlightSearch
from .tickets import TicketCheck
from .weather import Weather


class Application:
    def __init__(self, settings):
        self.settings = settings
        self.airports = Airports(settings.root / "data" / "airports.csv")
        self.store = LocalStore(settings.root / "data" / "provider_cache.sqlite")
        self.transport = Transport(settings.user_agent)
        self.searcher = FlightSearch(settings, self.transport, self.store, self.airports)
        self.tickets = TicketCheck(settings, self.transport, self.store, self.airports)
        self.aeroapi = AeroAPI(settings, self.transport, self.store)
        self.history = FlightHistory(settings.root / "data" / "flight_history.sqlite")
        self.weather = Weather(self.transport)
        self.ollama = Ollama(settings.ollama_url)
        self.sessions = {}
        self.sessions_lock = threading.Lock()

    def today(self):
        return datetime.now(ZoneInfo(self.settings.app_timezone)).date()

    def status(self):
        try:
            models, model_error = self.ollama.models(), None
        except DataError as error:
            models, model_error = [], str(error)
        coverage = self.history.coverage()
        return {"today": self.today().isoformat(), "last_departure_date": (self.today() + timedelta(days=30)).isoformat(),
                "search_ready": bool(self.settings.serpapi_key), "airports_ready": self.airports.path.exists(),
                "aeroapi_ready": bool(self.settings.aeroapi_key), "aeroapi_request_limit": self.settings.aeroapi_limit,
                "history_ready": coverage["has_records"], "history_months": self.settings.history_months,
                "history_coverage": coverage, "history_sync": sync_status(self.settings.root),
                "marketing_sync": sync_status(self.settings.root, "marketing"),
                "models": models, "model_error": model_error,
                "default_model": self.settings.ollama_model if self.settings.ollama_model in MODELS else MODELS[0]}

    def search(self, payload):
        try:
            departure = date.fromisoformat(payload["date"])
            origin, destination = payload["origin"], payload["destination"]
            if not isinstance(origin, str) or not isinstance(destination, str):
                raise ValueError()
            adults = payload.get("adults", 1)
            if type(adults) is not int:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise DataError("Choose departure and arrival airports and a valid date.") from None
        result = self.searcher.search(origin, destination, departure, today=self.today(),
                                      adults=adults, max_price=payload.get("max_price"),
                                      itinerary_type=payload.get("itinerary_type", "all"))
        search_id = secrets.token_urlsafe(24)
        with self.sessions_lock:
            now = time.monotonic()
            self.sessions = {key: value for key, value in self.sessions.items() if now - value["created"] < 7200}
            if len(self.sessions) >= 100:
                del self.sessions[next(iter(self.sessions))]
            self.sessions[search_id] = {"result": result, "created": now, "turns": {},
                                        "alternatives": {}, "alternative_sets": {}, "lock": threading.Lock()}
        return {**result, "search_id": search_id}

    def selected(self, payload):
        with self.sessions_lock:
            session = self.sessions.get(payload.get("search_id"))
        if not session or time.monotonic() - session["created"] >= 7200:
            raise DataError("This search has expired. Search again to continue.")
        index = payload.get("flight_index")
        flights = session["result"]["flights"]
        if "alternative_id" in payload:
            alternative_id = payload["alternative_id"]
            if not isinstance(alternative_id, str):
                raise DataError("Select an alternative from this search.")
            flights = session.get("alternative_sets", {}).get(alternative_id)
            if flights is None:
                raise DataError("This alternative list has expired. Compare alternatives again.")
        if type(index) is not int or not 0 <= index < len(flights):
            raise DataError("Select a flight from your search results first.")
        return session, flights[index]

    def history_for(self, flight):
        if len(legs_for(flight)) == 2:
            legs = [{"flight": leg, "history": self.history_for(leg)} for leg in legs_for(flight)]
            return {"source": "BTS", "journey": True, "available": any(l["history"].get("available") for l in legs),
                    "all_legs_available": all(l["history"].get("available") for l in legs), "legs": legs,
                    "notice": "Each leg has separate history. No combined itinerary delay rate or missed-connection probability is established."}
        match = re.fullmatch(r"([A-Z0-9]{2})\s*(\d+)", flight["flight_number"].strip().upper())
        if not match:
            return {"source": "BTS", "available": False, "reason": "This flight number cannot yet be matched to reporting records."}
        result = self.history.report(match[1], match[2], flight["origin"], flight["destination"],
                                     as_of=date.fromisoformat(flight["departure_local"][:10]),
                                     months=self.settings.history_months)
        if result.get("historical_identity_verified"):
            identity_note = ("BTS Marketing Carrier records explicitly identify the operating flights for this marketed flight and route. "
                             "Reporting Carrier outcomes are joined by date, operating airline and number, route, and scheduled departure time. "
                             "These are verified historical relationships; the operator of the selected future departure is not verified.")
        elif result.get("reason_code") == "marketing_records_conflicted":
            identity_note = result["reason"] + " The operator of the selected future departure remains unverified."
        elif result["available"]:
            identity_note = f"Records match numeric flight number and route under BTS reporting airline {result['matched_reporting_carrier']}. The requested airline prefix was not used as a filter. Marketed-to-operating airline identity is not yet verified; do not use this as a verified flight prediction."
        else:
            identity_note = "No matching reporting record has been established. Operating-airline identity is unverified."
        return {**result, "identity_verified": False, "identity_note": identity_note}

    def report(self, payload):
        session, flight = self.selected(payload)
        if not session["lock"].acquire(blocking=False):
            raise DataError("This journey is already being checked. Please wait a moment.")
        try:
            evidence = self.selected_analysis(session, flight)
            return {"flight": flight, **evidence,
                    "history_coverage": self.history.coverage(), "history_sync": sync_status(self.settings.root),
                    "marketing_sync": sync_status(self.settings.root, "marketing")}
        finally:
            session["lock"].release()

    def selected_analysis(self, session, flight):
        query = {**flight.get("booking_query", session["result"]["query"]),
                 "max_price": session["result"]["query"].get("max_price")}
        ticket = self.tickets.check(flight, query)
        session.setdefault("ticket_checks", {})[journey_key(flight)] = ticket
        flight["ticket_check"] = ticket
        for option in session["result"].get("flights", []):
            if journey_key(option) == journey_key(flight):
                option["ticket_check"] = ticket
        return {**self.analysis_for(flight), "ticket": ticket}

    def analysis_for(self, flight):
        if len(legs_for(flight)) == 2:
            legs = [{"flight": leg, **self.analysis_for(leg)} for leg in legs_for(flight)]
            layover = flight["layovers"][0]
            return {"source": "BTS + FlightAware AeroAPI", "journey": True, "legs": legs,
                    "connection": {**layover, "notice": (
                        "The scheduled layover is not all usable transfer time: allow for leaving the first plane, "
                        "reaching the next gate, and the airline's boarding deadline. First-leg delays can reduce the buffer. "
                        "Minimum connection rules and gate-transfer times have not been independently verified. "
                        "Both legs' history is shown separately; no missed-connection probability is established.")}}
        history = self.history_for(flight)
        live = self.aeroapi.lookup(flight)
        analysis = combine(flight, history, live, self.history, self.settings.history_months)
        if live.get("selected_flight_verified"):
            operating = live["selected"]["operating_ident"]
            services = history.get("operating_services", [])
            aligned = (history.get("historical_identity_verified", False) and len(services) == 1
                       and services[0]["operating_carrier"] + services[0]["operating_flight_number"] == operating)
            history = {**history, "identity_verified": bool(aligned), "selected_departure_identity_verified": True,
                       "identity_note": (f"FlightAware currently identifies the exact selected departure as operating flight {operating}. "
                                         + ("The BTS historical operating identity agrees with that match. " if aligned else
                                            "The marketed-flight BTS history is separate; use the selected-operator baseline in the combined assessment when available. ")
                                         + "This is current provider evidence, not a guarantee that the assignment cannot change or a validated delay prediction.")}
        return {"source": "BTS + FlightAware AeroAPI", "history": history, "aeroapi": live, "analysis": analysis}

    def weather_for(self, flight):
        reports = []
        points = list(dict.fromkeys((leg[key], leg[timestamp][:10]) for leg in legs_for(flight)
                                   for key, timestamp in (("origin", "departure_local"), ("destination", "arrival_local"))))
        for code, local_date in points:
            airport = self.airports.lookup(code)
            try:
                report = self.weather.for_date(airport["latitude"], airport["longitude"],
                                               date.fromisoformat(local_date), today=self.today())
                # Keep the evidence concise enough for the local model's context window.
                report["periods"] = [{key: period.get(key) for key in
                                      ("startTime", "shortForecast", "temperature", "temperatureUnit", "windSpeed", "windDirection")}
                                     for period in report.get("periods", [])]
                reports.append({"airport": code, "local_date": local_date, **report})
            except DataError as error:
                reports.append({"airport": code, "local_date": local_date, "available": False, "reason": str(error)})
        return {"source": "National Weather Service", "airports": reports,
                "notice": "Forecast conditions are not a calibrated flight delay probability."}

    def nearby_for(self, flight):
        return {"source": "OurAirports", "origin": self.airports.nearby(flight["origin"]),
                "destination": self.airports.nearby(flight["destination"])}

    def alternatives_for(self, session, flight):
        identity = journey_key(flight)
        saved = session["alternatives"].get(identity)
        if saved and time.monotonic() - saved["saved_at"] < 900:
            result = saved["result"]
            options = []
            for option in result["flights"]:
                ticket = session.get("ticket_checks", {}).get(journey_key(option))
                if ticket and ticket.get("eligible") is False:
                    continue
                options.append({**option, "ticket_check": ticket} if ticket else option)
            if options != result["flights"]:
                result = {**result, "flights": options, "options_returned": len(options),
                          "alternative_id": secrets.token_urlsafe(16)}
                saved["result"] = result
            sets = session.setdefault("alternative_sets", {})
            if result["alternative_id"] not in sets and len(sets) >= 20:
                del sets[next(iter(sets))]
            sets[result["alternative_id"]] = result["flights"]
            return result
        nearby = self.nearby_for(flight)
        origins = [flight["origin"]] + [a["iata"] for a in nearby["origin"]["nearby_airports"]]
        destinations = [flight["destination"]] + [a["iata"] for a in nearby["destination"]["nearby_airports"]]
        query = session["result"]["query"]
        selected_date = date.fromisoformat(flight["departure_local"][:10])
        options, checked, errors = [], [], []
        seen = {identity}
        today = self.today()
        for offset in (-1, 0, 1):
            departure = selected_date + timedelta(days=offset)
            if not 0 <= (departure - today).days <= 30:
                continue
            try:
                result = self.searcher.search_airports(origins, destinations, departure, today=today,
                                                       adults=query["adults"], max_price=query.get("max_price"), itinerary_type="all")
                checked.append({"date": departure.isoformat(), "cached": result["cached"], "fetched_at": result["fetched_at"]})
                for option in result["flights"]:
                    key = journey_key(option)
                    ticket = session.get("ticket_checks", {}).get(key)
                    if ticket and ticket.get("eligible") is False:
                        continue
                    if ticket:
                        option = {**option, "ticket_check": ticket}
                    if key in seen:
                        continue
                    seen.add(key)
                    difference = option["price"] - flight["price"] if option["price"] is not None and flight["price"] is not None else None
                    options.append({**option, "date_difference_days": offset, "price_difference": difference,
                                    "booking_query": result.get("query", {**query, "departure_id": ",".join(origins),
                                        "arrival_id": ",".join(destinations), "outbound_date": departure.isoformat()}),
                                    "changed_origin": option["origin"] != flight["origin"],
                                    "changed_destination": option["destination"] != flight["destination"]})
            except DataError as error:
                errors.append({"date": departure.isoformat(), "reason": str(error)})
        options.sort(key=lambda option: (option["price"] is None, option["price"] or 0, option["departure_local"]))
        alternative_id = secrets.token_urlsafe(16)
        sets = session.setdefault("alternative_sets", {})
        if len(sets) >= 20:
            del sets[next(iter(sets))]
        sets[alternative_id] = options
        result = {"source": "SerpApi Google Flights results", "flights": options, "options_returned": len(options),
                  "alternative_id": alternative_id,
                  "dates_checked": checked, "errors": errors, "nearby_radius_miles": 50,
                  "reliability_ranking_available": False,
                  "notice": "All returned eligible alternatives are included, both nonstop and one connection, regardless of your search filter. Sorted by quoted fare; no option is established as more reliable. Connecting ticket types are checked on selection. Provider coverage may be incomplete."}
        if checked:
            session["alternatives"][identity] = {"saved_at": time.monotonic(), "result": result}
        return result

    def alternatives(self, payload):
        session, flight = self.selected(payload)
        if not session["lock"].acquire(blocking=False):
            raise DataError("This flight is already being checked. Please wait a moment.")
        try:
            return self.alternatives_for(session, flight)
        finally:
            session["lock"].release()

    def chat(self, payload):
        session, flight = self.selected(payload)
        question, model = payload.get("message", ""), payload.get("model", self.settings.ollama_model)
        if not isinstance(question, str) or not question.strip() or len(question) > 3000:
            raise DataError("Enter a question of up to 3,000 characters.")
        if model not in MODELS:
            raise DataError("Choose one of the two configured local models.")
        if not session["lock"].acquire(blocking=False):
            raise DataError("The assistant is already checking this flight. Please wait a moment.")
        try:
            tools = {"get_flight_history": lambda: self.history_for(flight),
                     "get_flight_analysis": lambda: self.selected_analysis(session, flight),
                     "get_weather": lambda: self.weather_for(flight),
                     "get_nearby_airports": lambda: self.nearby_for(flight),
                     "find_alternatives": lambda: self.alternatives_for(session, flight)}
            key = (payload.get("alternative_id"), payload["flight_index"], model)
            turns = session["turns"].get(key, [])
            previous = [message for turn in turns[-1:] for message in turn]
            if len(json.dumps(previous)) > 14000:
                previous = []
            def traveler_flight(option):
                return {key: value for key, value in option.items() if key not in ("booking_token", "booking_query")}
            context = {"today": self.today().isoformat(), "flight": traveler_flight(flight),
                       "ticket": session.get("ticket_checks", {}).get(journey_key(flight)),
                       "search": session["result"]["query"], "search_fetched_at": session["result"]["fetched_at"]}
            def execute(name):
                result = tools[name]()
                if name == "get_flight_analysis" and result.get("journey"):
                    return {**result, "legs": [{"flight": leg["flight"], "analysis": {
                        "traveler": leg["analysis"]["traveler"], "cautions": leg["analysis"]["cautions"],
                        "selected_flight_verified": leg["analysis"]["selected_flight_verified"]}}
                        for leg in result["legs"]]}
                if name == "find_alternatives":
                    return {**result, "flights": [traveler_flight(option) for option in result["flights"][:8]],
                            "assistant_sample_count": min(8, len(result["flights"])),
                            "notice": result["notice"] + " The assistant receives up to eight options; the full returned list is available in the alternatives panel."}
                return result
            response, turn = answer(self.ollama, model, context, previous, question.strip(), execute)
            session["turns"][key] = (turns + [turn])[-3:]
            return response
        finally:
            session["lock"].release()
