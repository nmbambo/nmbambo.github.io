"""HTTP layer (standard library only). Routes follow assets/es/adapters.mjs GatewayStore:

  POST /streams/{stream}   {events:[envelope], expectedVersion?}  -> 200 {appended, positions} | 409 | 400
  GET  /streams/{stream}                                           -> [envelope]
  GET  /events?from=N&limit=M                                      -> [envelope]   (position >= N)
  GET  /events/last                                                -> {position}
  GET  /events/{id}                                                -> envelope | 404
  GET  /health                                                     -> status
  POST /TOPIC/{topic...}   optional Solace REST pass-through (adds CORS) when SOLACE_REST_URL is set
No request bodies or event data are ever logged (no personal data may enter logs).
"""
import json
import logging
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from envelope import ConcurrencyError, ValidationError, valid_id, validate_append

log = logging.getLogger("gateway")
MAX_BODY = 1_000_000
DEFAULT_LIMIT, MAX_LIMIT = 1000, 5000
CORS_HEADERS = "Content-Type, Accept, Authorization, Solace-Message-ID, Solace-Message-VPN, Solace-Delivery-Mode"


def make_handler(store, publisher, *, store_name, allowed_origins, solace_url=None, opener=None):
    open_url = opener or urllib.request.urlopen
    origins = set(allowed_origins)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ingqiqo-gateway/1"

        def log_message(self, fmt, *args):  # method + path only; never bodies
            log.info("%s %s", self.command, urlsplit(self.path).path)

        # -- helpers --
        def _cors(self):
            origin = self.headers.get("Origin")
            if origin and ("*" in origins or origin in origins):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", CORS_HEADERS)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def _send(self, status, payload=None, raw=None, ctype="application/json"):
            body = raw if raw is not None else (b"" if payload is None else json.dumps(payload).encode())
            self.send_response(status)
            self._cors()
            if body:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValidationError("request body too large")
            return self.rfile.read(n) if n else b""

        # -- verbs --
        def do_OPTIONS(self):
            self._send(204)

        def do_GET(self):
            try:
                u = urlsplit(self.path)
                parts = [unquote(p) for p in u.path.split("/") if p]
                q = parse_qs(u.query)
                if parts == ["health"]:
                    return self._send(200, {"status": "ok", "store": store_name,
                                            "kafkaClusterId": getattr(publisher, "cluster_id", None),
                                            "kafkaPending": publisher.pending})
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
                raw_parts = u.path.split("/")
                parts = [unquote(p) for p in raw_parts if p]
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
                    publish_failed = publisher.publish(result["new"]) if result["new"] else 0
                    return self._send(200, {"appended": result["appended"], "positions": result["positions"],
                                            "kafkaQueued": publish_failed})
                if parts and parts[0] == "TOPIC":
                    return self._solace(u)
                self._send(404, {"error": "not found"})
            except ValidationError as exc:
                self._send(400, {"error": str(exc)})
            except Exception:  # noqa: BLE001
                log.exception("POST failed")
                self._send(500, {"error": "internal error"})

        def _solace(self, u):
            if not solace_url:
                return self._send(404, {"error": "solace pass-through not configured"})
            body = self._body()
            # Solace-Message-VPN is sent by the site's SolaceRestBroker but is not a documented Solace REST
            # header (the VPN is chosen by client username / port), so it is dropped here.
            headers = {k: v for k, v in self.headers.items()
                       if (k.lower() in ("content-type", "authorization") or k.lower().startswith("solace-"))
                       and k.lower() != "solace-message-vpn"}
            req = urllib.request.Request(solace_url.rstrip("/") + u.path, data=body, method="POST", headers=headers)
            try:
                with open_url(req, timeout=5) as resp:
                    return self._send(resp.status, raw=resp.read() or None)
            except urllib.error.HTTPError as exc:
                return self._send(exc.code, raw=exc.read() or None, ctype="text/plain")
            except Exception:  # noqa: BLE001
                return self._send(502, {"error": "solace unreachable"})

    return Handler


def serve(store, publisher, host, port, **kw):
    return ThreadingHTTPServer((host, port), make_handler(store, publisher, **kw))
