"""Configuration, bounded HTTP reads, caching, and free-tier protection."""

from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler, HTTPSHandler


class DataError(Exception):
    """An actionable data/configuration failure safe to show to the caller."""


class ProviderError(DataError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Data provider returned HTTP {status}.")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, *args, **kwargs):
        return None  # Never forward an authenticated provider request elsewhere.


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_env(path: Path) -> dict[str, str]:
    """Read simple KEY=value settings without executing shell expressions."""
    if not path.exists():
        return {}
    values = {}
    for line_no, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, raw = line.partition("=")
        if not separator:
            raise DataError(f"Invalid .env assignment at line {line_no}.")
        try:
            parts = shlex.split(raw, comments=True)
        except ValueError:
            raise DataError(f"Invalid .env quoting at line {line_no}.") from None
        if len(parts) > 1:
            raise DataError(f"Quote values containing spaces in .env line {line_no}.")
        values[key.strip()] = parts[0] if parts else ""
    return values


@dataclass(frozen=True)
class Settings:
    root: Path
    serpapi_key: str = field(default="", repr=False)
    search_limit: int = 250
    history_months: int = 24
    app_timezone: str = "America/New_York"
    user_agent: str = "DelayAware/0.1 (local development)"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "gpt-oss:20b"
    aeroapi_key: str = field(default="", repr=False)
    aeroapi_limit: int = 100

    @classmethod
    def load(cls, root: Path):
        values = {**read_env(root / ".env"), **os.environ}
        try:
            limit = int(values.get("SERPAPI_SEARCH_LIMIT", "250"))
        except ValueError:
            raise DataError("SERPAPI_SEARCH_LIMIT must be an integer.") from None
        if not 1 <= limit <= 250:
            raise DataError("SERPAPI_SEARCH_LIMIT must be between 1 and 250.")
        try:
            months = int(values.get("HISTORY_MONTHS", "24"))
        except ValueError:
            raise DataError("HISTORY_MONTHS must be an integer.") from None
        if not 1 <= months <= 24:
            raise DataError("HISTORY_MONTHS must be between 1 and 24.")
        try:
            aeroapi_limit = int(values.get("AEROAPI_REQUEST_LIMIT", "100"))
        except ValueError:
            raise DataError("AEROAPI_REQUEST_LIMIT must be an integer.") from None
        if not 1 <= aeroapi_limit <= 100:
            raise DataError("AEROAPI_REQUEST_LIMIT must be between 1 and 100 for initial testing.")
        aeroapi_key = values.get("AEROAPI_API_KEY", "")
        if any(character.isspace() for character in aeroapi_key):
            raise DataError("AEROAPI_API_KEY must not contain whitespace.")
        return cls(
            root=root,
            serpapi_key=values.get("SERPAPI_API_KEY", ""),
            search_limit=limit,
            history_months=months,
            app_timezone=values.get("APP_TIMEZONE", "America/New_York"),
            user_agent=values.get("NWS_USER_AGENT", "DelayAware/0.1 (local development)"),
            ollama_url=values.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
            ollama_model=values.get("OLLAMA_MODEL", "gpt-oss:20b"),
            aeroapi_key=aeroapi_key, aeroapi_limit=aeroapi_limit,
        )


class Transport:
    def __init__(self, user_agent: str):
        self.user_agent = user_agent

    @staticmethod
    def tls_context():
        context = ssl.create_default_context()
        defaults = ssl.get_default_verify_paths()
        # python.org macOS installs may lack their own CA link. Use the system
        # certificate bundle in that case, preserving full TLS verification.
        system_bundle = Path("/etc/ssl/cert.pem")
        if (not defaults.cafile and not defaults.capath and system_bundle.is_file()
                and not os.environ.get("SSL_CERT_FILE") and not os.environ.get("SSL_CERT_DIR")):
            context.load_verify_locations(cafile=str(system_bundle))
        return context

    def get_bytes(self, url: str, params=None, *, headers=None, timeout=60) -> bytes:
        if urlparse(url).scheme != "https":
            raise DataError("Public data requests must use HTTPS.")
        if params:
            url += "?" + urlencode(params)
        request = Request(url, headers={"User-Agent": self.user_agent,
                                        "Accept": "application/json, text/csv;q=0.9", **(headers or {})})
        try:
            if headers:
                opener = build_opener(HTTPSHandler(context=self.tls_context()), NoRedirect())
                response_context = opener.open(request, timeout=timeout)
            else:
                response_context = urlopen(request, timeout=timeout, context=self.tls_context())
            with response_context as response:
                body = response.read(25 * 1024 * 1024 + 1)
        except HTTPError as error:
            # URLs and response bodies can contain credentials. Do not echo them.
            raise ProviderError(error.code) from None
        except URLError as error:
            if isinstance(error.reason, ssl.SSLCertVerificationError):
                raise DataError("Python could not verify the provider's HTTPS certificate. Check the local certificate configuration.") from None
            raise DataError("Could not reach the data provider; check connectivity and retry.") from None
        except (TimeoutError, OSError):
            raise DataError("Could not reach the data provider; check connectivity and retry.") from None
        if len(body) > 25 * 1024 * 1024:
            raise DataError("Provider response exceeds the supported size.")
        return body

    def get_json(self, url: str, params=None, *, headers=None, timeout=60):
        body = self.get_bytes(url, params, headers=headers, timeout=timeout)
        if not body:
            return None  # AWC returns HTTP 204 when no matching observations exist.
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise DataError("Data provider returned an unreadable JSON response.") from None


class LocalStore:
    """Cache normalized results and conservatively count SerpApi attempts."""

    def __init__(self, path: Path, clock=time.time):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.clock = clock
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, saved REAL, body TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS requests (at REAL NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS requests_at ON requests(at)")
            db.execute("CREATE TABLE IF NOT EXISTS aeroapi_requests (at REAL NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS aeroapi_requests_at ON aeroapi_requests(at)")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def key(params: dict) -> str:
        return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()

    def get(self, key: str, ttl: int):
        with self.connect() as db:
            row = db.execute("SELECT saved, body FROM cache WHERE key = ?", (key,)).fetchone()
        if row and 0 <= self.clock() - row[0] < ttl:
            return json.loads(row[1])
        return None

    def put(self, key: str, body):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                       (key, self.clock(), json.dumps(body)))

    def reserve_search(self, limit: int):
        if not 1 <= limit <= 250:
            raise DataError("Search allowance must be between 1 and 250.")
        now = self.clock()
        # A rolling 31-day window is conservative across provider billing dates.
        # Count attempts, including failures, rather than risk undercounting usage.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            month = db.execute("SELECT COUNT(*) FROM requests WHERE at > ?", (now - 31 * 86400,)).fetchone()[0]
            hour = db.execute("SELECT COUNT(*) FROM requests WHERE at > ?", (now - 3600,)).fetchone()[0]
            if month >= limit:
                raise DataError("Local search allowance reached. Cached results remain available.")
            if hour >= 50:
                raise DataError("Local hourly search allowance reached. Retry later.")
            db.execute("INSERT INTO requests VALUES (?)", (now,))

    def reserve_aeroapi(self, limit: int):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DataError("AeroAPI request allowance must be between 1 and 100.")
        now = self.clock()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            month = db.execute("SELECT COUNT(*) FROM aeroapi_requests WHERE at>?", (now - 31 * 86400,)).fetchone()[0]
            minute = db.execute("SELECT COUNT(*) FROM aeroapi_requests WHERE at>?", (now - 60,)).fetchone()[0]
            if month >= limit:
                raise DataError("Local AeroAPI allowance reached. BTS history remains available.")
            if minute >= 6:
                raise DataError("Local AeroAPI rate limit reached. Wait a minute before checking another flight.")
            db.execute("INSERT INTO aeroapi_requests VALUES (?)", (now,))
