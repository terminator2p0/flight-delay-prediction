"""Loopback-only local browser application. Never serves project files."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .application import Application
from .core import DataError

WEB = Path(__file__).parent / "web"
STATIC = {"/": ("index.html", "text/html"), "/style.css": ("style.css", "text/css"),
          "/app.js": ("app.js", "text/javascript")}


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Requests can contain travel details; no request logging by default.

        def reply(self, status, body, mime="application/json"):
            data = json.dumps(body, allow_nan=False).encode() if mime == "application/json" else body
            self.send_response(status)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def allowed_host(self):
            port = self.server.server_address[1]
            return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def do_GET(self):
            if not self.allowed_host():
                return self.reply(403, {"error": "Use the local application address."})
            path = urlparse(self.path).path
            try:
                if path in STATIC:
                    filename, mime = STATIC[path]
                    return self.reply(200, (WEB / filename).read_bytes(), mime)
                if path == "/api/status":
                    return self.reply(200, app.status())
                if path == "/api/airports":
                    query = parse_qs(urlparse(self.path).query).get("q", [""])[0].strip().lower()
                    airports = app.airports.load().values()
                    matches = [airport for airport in airports if not query or query in airport["iata"].lower() or query in airport["name"].lower()]
                    matches.sort(key=lambda airport: (airport["iata"].lower() != query, airport["name"]))
                    return self.reply(200, {"airports": matches[:1000]})
                return self.reply(404, {"error": "Page not found."})
            except DataError as error:
                return self.reply(400, {"error": str(error)})

        def do_POST(self):
            port = self.server.server_address[1]
            allowed_origins = (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
            if (not self.allowed_host() or self.headers.get("Origin") not in (*allowed_origins, None)
                    or self.headers.get("X-Delay-Aware") != "local"
                    or self.headers.get_content_type() != "application/json"):
                return self.reply(403, {"error": "Use the local application to make requests."})
            routes = {"/api/search": app.search, "/api/report": app.report,
                      "/api/chat": app.chat, "/api/alternatives": app.alternatives}
            operation = routes.get(urlparse(self.path).path)
            if not operation:
                return self.reply(404, {"error": "Action not found."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise DataError("Request is empty or too large.")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise DataError("Expected a structured request.")
                return self.reply(200, operation(payload))
            except DataError as error:
                return self.reply(400, {"error": str(error)})
            except (ValueError, TypeError, KeyError):
                return self.reply(400, {"error": "The request is invalid. Please check your inputs."})
            except Exception:
                # Never send tracebacks, paths or credential-bearing requests to the browser.
                return self.reply(500, {"error": "This action could not be completed. Please try again."})
    return Handler


def serve(settings, port=8765):
    if not 1024 <= port <= 65535:
        raise DataError("Choose a local port from 1024 to 65535.")
    app = Application(settings)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(app))
    server.daemon_threads = True
    print(f"Delay Aware is running at http://127.0.0.1:{port}", flush=True)
    print("Open that address in your browser. Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
