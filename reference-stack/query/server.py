"""Query side HTTP (read only). Standard library. Same CORS rules as the command service: an Origin in ALLOWED_ORIGINS gets
Access-Control-Allow-Origin, anything else gets none; GET and OPTIONS only; no cookies, no credentials.

  GET /health                       {"ready", "docs", "lag": {"es", "cdc"}}           503 until the first catch-up
  GET /search?q=&algorithm=&rows=&start=                                               the contract of assets/search/remote.mjs
  GET /events?from=0&limit=1000     the projected log (positions, ids, types)
  GET /stats                        counts, checkpoints, per-status apply counters
"""
import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fts_query import MAX_QUERY_LENGTH, QueryError
from search import search


def make_handler(rm, *, allowed_origins, health=None):
    allowed = set(allowed_origins or [])

    class H(BaseHTTPRequestHandler):
        server_version = "ingqiqo-query/0.1"
        sys_version = ""

        def log_message(self, *a):  # quiet
            pass

        def _cors(self):
            o = self.headers.get("Origin")
            if o and o in allowed:
                self.send_header("Access-Control-Allow-Origin", o)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Accept")
                self.send_header("Access-Control-Max-Age", "600")

        def _json(self, status, body):
            raw = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self._cors()
            self.end_headers()
            self.wfile.write(raw)

        def do_OPTIONS(self):  # noqa: N802
            self.send_response(204)
            self._cors()
            self.end_headers()

        def _deny(self):
            self._json(405, {"error": "Read only. Commands go to the command service."})

        do_POST = do_PUT = do_DELETE = do_PATCH = _deny  # noqa: N815

        def do_GET(self):  # noqa: N802
            u = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(u.query)
            one = lambda k, d=None: (qs.get(k) or [d])[0]  # noqa: E731
            try:
                if u.path == "/health":
                    h = health() if health else {"ready": True}
                    return self._json(200 if h.get("ready") else 503, h)
                if u.path == "/search":
                    q = one("q", "")
                    if len(q) > MAX_QUERY_LENGTH:
                        return self._json(400, {"error": f"Search strings are limited to {MAX_QUERY_LENGTH} characters.", "position": MAX_QUERY_LENGTH})
                    try:
                        rows, start = int(one("rows", "10")), int(one("start", "0"))
                    except ValueError:
                        return self._json(400, {"error": "rows and start must be whole numbers."})
                    try:
                        return self._json(200, search(rm.reader(), q, algorithm=one("algorithm", "bm25"), rows=rows, start=start,
                                                      version=rm.version))
                    except QueryError as e:
                        return self._json(400, {"error": str(e), "position": getattr(e, "position", 0)})
                if u.path == "/events":
                    try:
                        frm, lim = int(one("from", "0")), min(1000, max(1, int(one("limit", "1000"))))
                    except ValueError:
                        return self._json(400, {"error": "from and limit must be whole numbers."})
                    return self._json(200, {"events": rm.events(frm, lim)})
                if u.path == "/stats":
                    return self._json(200, rm.stats())
                return self._json(404, {"error": "Not found."})
            except Exception:  # noqa: BLE001  never leak internals
                return self._json(500, {"error": "Internal error."})

    return H


def serve(rm, host, port, **kw):
    return ThreadingHTTPServer((host, port), make_handler(rm, **kw))
