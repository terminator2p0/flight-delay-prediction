"""Public airport reference data used to enforce the domestic search scope."""

import csv
from io import StringIO
import math
from pathlib import Path

from .core import DataError

AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
US_COUNTRIES = {"US", "PR", "VI", "GU", "MP", "AS"}
NEARBY_RADIUS_MILES = 50
EARTH_RADIUS_MILES = 3958.7613


class Airports:
    def __init__(self, path: Path):
        self.path = path

    def sync(self, transport):
        body = transport.get_bytes(AIRPORTS_URL)
        self.parse(body.decode("utf-8-sig"))  # Validate before replacing local data.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(body)
        temporary.replace(self.path)

    @staticmethod
    def parse(text: str) -> dict:
        reader = csv.DictReader(StringIO(text))
        required = {"iata_code", "iso_country", "latitude_deg", "longitude_deg",
                    "name", "ident", "scheduled_service", "type"}
        if not required.issubset(reader.fieldnames or []):
            raise DataError("Airport reference file has an unexpected format.")
        airports = {}
        for row in reader:
            code = row["iata_code"]
            if (row["iso_country"] not in US_COUNTRIES or not code
                    or row["scheduled_service"] != "yes" or row["type"] == "closed"):
                continue
            try:
                lat, lon = float(row["latitude_deg"]), float(row["longitude_deg"])
            except ValueError:
                continue
            if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            airports[code] = {"iata": code, "icao": row["ident"], "name": row["name"],
                              "latitude": lat, "longitude": lon, "country": row["iso_country"]}
        if not airports:
            raise DataError("No supported US airports found in the reference file.")
        return airports

    def load(self) -> dict:
        if not self.path.exists():
            raise DataError("Airport reference data is missing. Run: python3 -m delay_aware sync-airports")
        return self.parse(self.path.read_text(encoding="utf-8-sig"))

    def lookup(self, code: str) -> dict:
        airport = self.load().get(code.strip().upper())
        if not airport:
            raise DataError("Choose a supported US airport with scheduled passenger service.")
        return airport

    @staticmethod
    def distance_miles(first: dict, second: dict) -> float:
        lat1, lat2 = math.radians(first["latitude"]), math.radians(second["latitude"])
        delta_lat = lat2 - lat1
        delta_lon = math.radians(second["longitude"] - first["longitude"])
        haversine = (math.sin(delta_lat / 2) ** 2
                     + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2)
        return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(min(1.0, max(0.0, haversine))))

    def nearby(self, code: str) -> dict:
        airports = self.load()
        origin = airports.get(code.strip().upper())
        if not origin:
            raise DataError("Choose a supported US airport with scheduled passenger service.")
        candidates = []
        for airport in airports.values():
            if airport["iata"] == origin["iata"]:
                continue
            distance = self.distance_miles(origin, airport)
            # Apply the radius before rounding distances for display.
            if distance <= NEARBY_RADIUS_MILES:
                candidates.append({**airport, "distance_miles": distance})
        candidates.sort(key=lambda airport: (airport["distance_miles"], airport["iata"]))
        for airport in candidates:
            airport["distance_miles"] = round(airport["distance_miles"], 1)
        return {"source": "OurAirports", "reference_airport": origin,
                "radius_miles": NEARBY_RADIUS_MILES, "distance_kind": "straight_line",
                "nearby_airports": candidates,
                "notice": "Airport candidates require a flight search to confirm service on the requested route and date. Distances are not driving distances or travel times."}
