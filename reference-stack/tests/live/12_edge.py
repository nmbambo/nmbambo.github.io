"""Live check of the KrakenD edge gateway in front of the LIGHT stack (127.0.0.1:8092). Standard library only.
Run after `docker compose up -d` (krakend is part of the default stack). Everything goes through the edge, never straight to the services:
  1 POST /streams/{stream} appends an event (200/201); re-appending the same id is de-duplicated
  2 a stale expectedVersion comes back as 409 (status and body pass through unchanged)
  3 the event becomes searchable through GET /search
  4 a malformed boolean query comes back as 400 from the query service
  5 GET /stats, /events and the two health routes work
  6 CORS: an allowed origin gets Access-Control-Allow-Origin, a disallowed one gets none; the preflight is answered
Uses uniquely named synthetic streams and documents, so it can be re-run without cleaning up.
"""
import json, sys, time, urllib.error, urllib.parse, urllib.request, uuid
EDGE = "http://127.0.0.1:8092"
ALLOWED, DENIED = "http://localhost:8000", "http://evil.example"
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m) or c  # noqa: E731
results = []

def call(method, path, body=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(EDGE + path, data=None if body is None else json.dumps(body).encode(), method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw, status, hd = r.read(), r.status, r.headers
    except urllib.error.HTTPError as e:
        raw, status, hd = e.read(), e.code, e.headers
    try:
        return status, json.loads(raw or b"null"), hd
    except ValueError:
        return status, raw, hd

def wait(pred, t=60):
    end = time.time() + t
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False

tag = uuid.uuid4().hex[:8]
word = "edgetest" + "".join("abcdefghij"[int(c, 16) % 10] for c in tag)   # letters only, unique per run
stream = f"content-edge-{tag}"
eid = str(uuid.uuid4())
event = {"id": eid, "type": "ContentAdded", "stream": stream,
         "data": {"id": f"edge#{tag}", "title": f"Synthetic {word} page", "section": "Edge", "source": "edge.html",
                  "text": f"Synthetic document {word} added through the KrakenD edge gateway.", "tags": ["page"],
                  "type": "page", "url": f"/edge.html#{tag}", "hash": uuid.uuid4().hex + uuid.uuid4().hex},
         "meta": {"ts": "2026-10-04T00:00:00Z", "schema": 1, "source": "edge-test"}}
path = f"/streams/{urllib.parse.quote(stream, safe='')}"

# 1 append and de-duplicate
code, body, _ = call("POST", path, {"events": [event], "expectedVersion": -1})
results.append(ok(code in (200, 201) and body.get("appended") == 1, f"1 append through the edge: HTTP {code} {body}"))
code, body, _ = call("POST", path, {"events": [event]})
results.append(ok(code in (200, 201) and body.get("appended") == 0 and body.get("skipped") == 1, f"1 re-appending the same id is de-duplicated: HTTP {code} {body}"))

# 2 optimistic concurrency
other = {**event, "id": str(uuid.uuid4())}
code, body, hd = call("POST", path, {"events": [other], "expectedVersion": -1})
results.append(ok(code == 409 and isinstance(body, dict) and "actual" in body and hd.get("Content-Type", "").startswith("application/json"),
                  f"2 stale expectedVersion is refused: HTTP {code} {body}"))
code, body, _ = call("POST", path, {"events": [other], "expectedVersion": 0})
results.append(ok(code in (200, 201) and body.get("appended") == 1, f"2 correct expectedVersion 0 is accepted: HTTP {code}"))
code, body, _ = call("POST", path, {"events": "nope"})
results.append(ok(code == 400, f"2 bad envelope passes the command service's 400 through: HTTP {code}"))

# 3 search sees it (JetStream -> SQLite FTS5)
q = "/search?" + urllib.parse.urlencode({"q": word, "algorithm": "bm25", "rows": 5})
found = wait(lambda: call("GET", q)[1]["total"] >= 1, 60)
code, body, _ = call("GET", q)
results.append(ok(found and code == 200 and any(r["id"] == f"edge#{tag}" for r in body["results"]),
                  f"3 search through the edge finds the new document ({body.get('total') if isinstance(body, dict) else '?'} hit)"))
code, body, _ = call("GET", "/search?" + urllib.parse.urlencode({"q": word, "algorithm": "bm25", "rows": 1, "start": 0, "ignored": "x"}))
results.append(ok(code == 200 and len(body["results"]) <= 1, "3 rows/start pass through (and an unlisted parameter is dropped, not an error)"))

# 4 malformed query -> 400 from the query service
code, body, _ = call("GET", "/search?" + urllib.parse.urlencode({"q": "alpha AND (beta", "algorithm": "boolean"}))
results.append(ok(code == 400 and isinstance(body, dict) and "error" in body, f"4 malformed boolean query: HTTP {code} {body}"))
code, body, _ = call("GET", "/search?q=x&rows=abc")
results.append(ok(code == 400, f"4 non-numeric rows: HTTP {code}"))

# 5 the read and health routes
code, body, _ = call("GET", "/stats")
results.append(ok(code == 200 and body.get("docs", 0) > 0 and "checkpoints" in body, f"5 /stats: HTTP {code}, {body.get('docs')} docs, {body.get('events')} events"))
code, body, _ = call("GET", "/events?from=0&limit=2")
results.append(ok(code == 200 and 1 <= len(body["events"]) <= 2, f"5 /events honours limit: {len(body['events'])} events"))
for svc in ("command", "query"):
    code, body, _ = call("GET", f"/health/{svc}")
    results.append(ok(code == 200 and (body.get("ready") is True or body.get("status") == "ok"), f"5 /health/{svc}: HTTP {code}"))
code, _, _ = call("GET", "/nope")
results.append(ok(code == 404, f"5 an unrouted path is 404 at the edge: HTTP {code}"))

# 6 CORS
_, _, hd = call("GET", "/stats", headers={"Origin": ALLOWED})
results.append(ok(hd.get("Access-Control-Allow-Origin") == ALLOWED and hd.get("Access-Control-Allow-Credentials") is None,
                  f"6 allowed origin gets ACAO={hd.get('Access-Control-Allow-Origin')!r}, no credentials header"))
_, _, hd = call("GET", "/stats", headers={"Origin": DENIED})
results.append(ok(hd.get("Access-Control-Allow-Origin") is None, f"6 disallowed origin gets no Access-Control-Allow-Origin (got {hd.get('Access-Control-Allow-Origin')!r})"))
code, _, hd = call("OPTIONS", path, headers={"Origin": ALLOWED, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
results.append(ok(code in (200, 204) and hd.get("Access-Control-Allow-Origin") == ALLOWED and "POST" in (hd.get("Access-Control-Allow-Methods") or ""),
                  f"6 preflight for POST from the allowed origin: HTTP {code}, methods {hd.get('Access-Control-Allow-Methods')!r}"))
code, _, hd = call("OPTIONS", path, headers={"Origin": DENIED, "Access-Control-Request-Method": "POST"})
results.append(ok(hd.get("Access-Control-Allow-Origin") is None, f"6 preflight from the disallowed origin gets no ACAO (HTTP {code})"))

print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
