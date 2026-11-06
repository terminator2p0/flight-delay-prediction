"""Aviation observations and date-bounded NWS hourly forecasts."""

from datetime import date, datetime
from urllib.parse import urlparse

from .core import DataError, utc_now


class Weather:
    def __init__(self, transport):
        self.transport = transport

    def aviation(self, icao: str):
        icao = icao.strip().upper()
        if len(icao) != 4 or not icao.isalnum():
            raise DataError("Use a four-character ICAO weather station, such as KBOS.")
        observations = self.transport.get_json("https://aviationweather.gov/api/data/metar",
                                               {"ids": icao, "format": "json"})
        forecasts = self.transport.get_json("https://aviationweather.gov/api/data/taf",
                                            {"ids": icao, "format": "json"})
        return {"source": "NOAA Aviation Weather Center", "fetched_at": utc_now(),
                "station": icao, "observations": observations or [], "terminal_forecasts": forecasts or [],
                "notice": "Current observations and terminal forecasts apply only to their reported times, not the entire 30-day search window."}

    def for_date(self, latitude: float, longitude: float, departure: date, *, today: date):
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise DataError("Invalid airport coordinates.")
        result = {"source": "National Weather Service", "fetched_at": utc_now(),
                  "travel_date": departure.isoformat(), "available": False, "periods": []}
        if not 0 <= (departure - today).days <= 7:
            return {**result, "reason": "Travel date is outside the standard seven-day forecast horizon."}
        point = self.transport.get_json(f"https://api.weather.gov/points/{latitude:.4f},{longitude:.4f}")
        url = (point or {}).get("properties", {}).get("forecastHourly")
        if not url:
            return {**result, "reason": "No hourly forecast is available for this location."}
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "api.weather.gov":
            raise DataError("NWS returned an unexpected forecast address.")
        forecast = self.transport.get_json(url)
        properties = (forecast or {}).get("properties", {})
        for period in properties.get("periods", []) or []:
            try:
                # Forecast timestamps contain the location's UTC offset.
                start = datetime.fromisoformat(period["startTime"])
            except (KeyError, ValueError, TypeError):
                continue
            if start.date() == departure:
                result["periods"].append(period)
        result["available"] = bool(result["periods"])
        result["provider_updated_at"] = properties.get("updateTime")
        if not result["available"]:
            result["reason"] = "The returned forecast does not cover this travel date."
        return result
