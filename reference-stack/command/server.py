"""Command service HTTP layer (standard library only). Same routes as the heavy gateway, so the browser's GatewayStore
(assets/es/adapters.mjs) works against it unchanged:

  POST /streams/{stream}   {events:[envelope], expectedVersion?}  -> 200 {appended, positions, skipped} | 409 | 400
  GET  /streams/{stream}                                           -> [envelope]
  GET  /events?from=N&limit=M                                      -> [envelope]   (position >= N)
  GET  /events/last                                                -> {position}
  GET  /events/{id}                                                -> envelope | 404
  GET  /health                                                     -> 200 once the log is reachable and indexed, else 503
Writes go to JetStream and nowhere else; search lives in the query service. No request bodies or event data are logged.
"""
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from envelope import ConcurrencyError, ValidationError, valid_id, validate_append

log = logging.getLogger("command")
MAX_BODY = 1_000_000
DEFAULT_LIMIT, MAX_LIMIT = 1000, 5000
CORS_HEADERS = "Content-Type, Accept, Authorization"


def make_handler(store, *, allowed_origins, health=None):
    origins = set(allowed_origins)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ingqiqo-command/1"

        def log_message(self, fmt, *args):  # method + path only; never bodies
            log.info("%s %s", self.command, urlsplit(self.path).path)

        def _cors(self):
            origin = self.headers.get("Origin")
            if origin and ("*" in origins or origin in origins):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", CORS_HEADERS)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def _send(self, status, payload=None):
            body = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            self._cors()
            if body:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValidationError("request body too large")
            return self.rfile.read(n) if n else b""

        def do_OPTIONS(self):
            self._send(204)

        def do_GET(self):
            try:
                u = urlsplit(self.path)
                parts = [unquote(p) for p in u.path.split("/") if p]
                q = parse_qs(u.query)
                if parts == ["health"]:
                    h = health() if health else {"ready": True}
                    return self._send(200 if h.get("ready") else 503, dict(h, status="ok" if h.get("ready") else "starting"))
                if parts == ["events"]:
                    frm = int(q.get("from", ["0"])[0])
                    lim = min(int(q.get("limit", [str(DEFAULT_LIMIT)])[0]), MAX_LIMIT)
                    return self._send(200, store.read(max(frm, 0), max(lim, 1)))
                if parts == ["events", "last"]:
                    return self._send(200, {"position": store.last_position()})
                if len(parts) == 2 and parts[0] == "events":
                    ev = store.get(parts[1]) if valid_id(parts[1]) else None
                    return self._send(200, ev) if ev else self._send(404, {"error": "not found"})
                if len(parts) == 2 and parts[0] == "streams":
                    return self._send(200, store.read_stream(parts[1]))
                self._send(404, {"error": "not found"})
            except ValueError:
                self._send(400, {"error": "bad query parameter"})
            except Exception:  # noqa: BLE001
                log.exception("GET failed")
                self._send(500, {"error": "internal error"})

        def do_POST(self):
            try:
                u = urlsplit(self.path)
                parts = [unquote(p) for p in u.path.split("/") if p]
                if len(parts) == 2 and parts[0] == "streams":
                    try:
                        body = json.loads(self._body() or b"null")
                    except ValueError:
                        raise ValidationError("body is not valid JSON")
                    events, expected = validate_append(parts[1], body)
                    try:
                        result = store.append(parts[1], events, expected)
                    except ConcurrencyError as exc:
                        return self._send(409, {"error": str(exc), "stream": parts[1],
                                                "expected": exc.expected, "actual": exc.actual})
                    return self._send(200, {"appended": result["appended"], "positions": result["positions"],
                                            "skipped": result.get("skipped", 0)})
                self._send(404, {"error": "not found"})
            except ValidationError as exc:
                self._send(400, {"error": str(exc)})
            except Exception:  # noqa: BLE001
                log.exception("POST failed")
                self._send(500, {"error": "internal error"})

    return Handler


def serve(store, host, port, **kw):
    return ThreadingHTTPServer((host, port), make_handler(store, **kw))
