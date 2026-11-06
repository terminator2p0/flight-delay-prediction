"""Run data integrations locally without third-party Python dependencies."""

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .airports import Airports
from .core import DataError, LocalStore, Settings, Transport
from .history import FlightHistory
from .search import FlightSearch
from .weather import Weather

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None):
    parser = argparse.ArgumentParser(description="Delay Aware data connections")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Show setup status without exposing credentials")
    server = sub.add_parser("serve", help="Run the local search and assistant application")
    server.add_argument("--port", type=int, default=8765)
    sub.add_parser("sync-airports", help="Download public US airport lookup data")
    nearby = sub.add_parser("nearby-airports", help="Find US airport candidates within 50 straight-line miles")
    nearby.add_argument("airport")
    search = sub.add_parser("search", help="Search one-way nonstop and one-connection journeys")
    search.add_argument("origin")
    search.add_argument("destination")
    search.add_argument("date", type=date.fromisoformat)
    search.add_argument("--adults", type=int, default=1)
    search.add_argument("--max-price", type=int, help="Optional maximum displayed fare in whole USD")
    search.add_argument("--itinerary-type", choices=("all", "nonstop", "one_connection"), default="all")
    weather = sub.add_parser("weather", help="Get weather covering a travel date, if available")
    weather.add_argument("airport")
    weather.add_argument("date", type=date.fromisoformat)
    aviation = sub.add_parser("aviation-weather", help="Get current METAR and TAF reports")
    aviation.add_argument("station")
    importer = sub.add_parser("import-history", help="Import a local BTS flight-level CSV or ZIP")
    importer.add_argument("file", type=Path)
    importer.add_argument("--dataset", choices=("reporting", "marketing"), default="reporting")
    sync = sub.add_parser("sync-history", help="Download and import the latest published BTS monthly archives")
    sync.add_argument("--months", type=int, default=24)
    marketing_sync = sub.add_parser("sync-marketing-history", help="Enrich reporting history with BTS marketing-to-operating relationships")
    marketing_sync.add_argument("--months", type=int, default=24)
    audit = sub.add_parser("audit-history-join", help="Reconcile marketing and reporting operations for a month")
    audit.add_argument("month", help="Month as YYYY-MM")
    sub.add_parser("history-status", help="Show history download progress and loaded coverage")
    history = sub.add_parser("history", help="Calculate observed flight-level reliability")
    history.add_argument("carrier")
    history.add_argument("flight_number")
    history.add_argument("origin")
    history.add_argument("destination")
    history.add_argument("--as-of", type=date.fromisoformat)
    history.add_argument("--departure-hour", type=int)
    analysis = sub.add_parser("analyze-flight", help="Combine BTS history with a bounded AeroAPI lookup")
    analysis.add_argument("flight_number", help="Marketed flight designator, for example 'DL 2351'")
    analysis.add_argument("origin")
    analysis.add_argument("destination")
    analysis.add_argument("departure", help="Scheduled local departure, for example 2026-10-24T07:05:00")
    args = parser.parse_args(argv)
    try:
        settings = Settings.load(ROOT)
        if args.command == "serve":
            from .server import serve
            serve(settings, args.port)
            return 0
        today = datetime.now(ZoneInfo(settings.app_timezone)).date()
        transport = Transport(settings.user_agent)
        airports = Airports(ROOT / "data" / "airports.csv")
        if args.command == "status":
            result = {"serpapi_key_configured": bool(settings.serpapi_key),
                      "search_limit_per_rolling_31_days": settings.search_limit,
                      "airport_reference_ready": airports.path.exists(),
                      "history_database_ready": (ROOT / "data" / "flight_history.sqlite").exists(),
                      "history_months": settings.history_months, "app_timezone": settings.app_timezone,
                      "ollama_model_configured": settings.ollama_model,
                      "ollama_connection_tested": False,
                      "aeroapi_key_configured": bool(settings.aeroapi_key),
                      "aeroapi_limit_per_rolling_31_days": settings.aeroapi_limit,
                      "scope": "one-way US domestic; nonstop or one same-airport connection; next 30 days; ticket details checked on selection"}
        elif args.command == "sync-airports":
            airports.sync(transport)
            result = {"source": "OurAirports", "saved": str(airports.path)}
        elif args.command == "nearby-airports":
            result = airports.nearby(args.airport)
        elif args.command == "search":
            store = LocalStore(ROOT / "data" / "provider_cache.sqlite")
            result = FlightSearch(settings, transport, store, airports).search(
                args.origin, args.destination, args.date, today=today, adults=args.adults,
                max_price=args.max_price, itinerary_type=args.itinerary_type)
        elif args.command == "aviation-weather":
            result = Weather(transport).aviation(args.station)
        elif args.command == "weather":
            airport = airports.lookup(args.airport)
            result = Weather(transport).for_date(airport["latitude"], airport["longitude"], args.date, today=today)
        elif args.command == "import-history":
            result = FlightHistory(ROOT / "data" / "flight_history.sqlite").import_file(args.file, dataset=args.dataset)
        elif args.command == "sync-history":
            from .history_sync import HistorySync
            result = HistorySync(ROOT, transport).run(months=args.months, today=today)
        elif args.command == "sync-marketing-history":
            from .history_sync import HistorySync
            result = HistorySync(ROOT, transport, dataset="marketing").run(months=args.months, today=today)
        elif args.command == "audit-history-join":
            from .marketing import audit_join
            date.fromisoformat(args.month + "-01")
            result = audit_join(ROOT / "data" / "flight_history.sqlite", args.month)
        elif args.command == "analyze-flight":
            from .application import Application
            departure = datetime.fromisoformat(args.departure)
            result = Application(settings).analysis_for({"flight_number": args.flight_number,
                "origin": args.origin.strip().upper(), "destination": args.destination.strip().upper(),
                "departure_local": departure.isoformat()})
        elif args.command == "history-status":
            from .history_sync import sync_status
            result = {"sync": sync_status(ROOT), "marketing_sync": sync_status(ROOT, "marketing"),
                      "coverage": FlightHistory(ROOT / "data" / "flight_history.sqlite").coverage()}
        else:
            result = FlightHistory(ROOT / "data" / "flight_history.sqlite").report(
                args.carrier, args.flight_number, args.origin, args.destination,
                as_of=args.as_of or today, months=settings.history_months, departure_hour=args.departure_hour)
        print(json.dumps(result, indent=2))
        return 0
    except (DataError, OSError, ValueError, ZoneInfoNotFoundError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
