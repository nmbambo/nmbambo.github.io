"""Check 6: CORS, in a real browser (Playwright / Chromium).

Serves a tiny page (cors_page.html, which imports the site's own assets/es adapter modules) from two local origins:
an allowed one (http://localhost:8000, in ALLOWED_ORIGINS) and a disallowed one (http://localhost:9999).
From the allowed origin the page calls the gateway (POST append, GET read/last, a 409) and the Confluent REST proxy;
Chromium must accept all of it (preflights included). From the disallowed origin Chromium must block both.
Read-only static server over the repo root; nothing is written. Needs `pip install playwright` + Chromium
(PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers in the sandbox).
"""
import functools
import http.server
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

from _common import GATEWAY, REPO, REST, Checks, compose, http as jget, run_id

PAGE = "/reference-stack/tests/live/cors_page.html"


def serve(port):
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    handler = functools.partial(Quiet, directory=str(REPO))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main():
    from playwright.sync_api import sync_playwright
    c = Checks("06_cors")
    rid = run_id()
    cluster = jget("GET", f"{GATEWAY}/health")[1]["kafkaClusterId"]
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 2))
    servers = [serve(8000), serve(9999)]
    wire = []
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(args=["--no-sandbox"])
            res = {}
            for origin in ("http://localhost:8000", "http://localhost:9999"):
                page = b.new_page()
                wire.clear()
                page.on("response", lambda r, w=wire: w.append((r.request.method, r.url, r.status, r.headers.get("access-control-allow-origin"))) if ("8088" in r.url or "8082" in r.url) else None)
                page.goto(origin + PAGE)
                page.wait_for_function("window.corsReady === true", timeout=15000)
                out = page.evaluate("(a) => window.runCors(a)", {"gateway": GATEWAY, "rest": REST, "clusterId": cluster, "rid": rid})
                res[origin] = (out, list(wire))
                page.close()
            b.close()
    finally:
        for s in servers:
            s.shutdown()
    ok_out, ok_wire = res["http://localhost:8000"]
    bad_out, bad_wire = res["http://localhost:9999"]
    for name, label in [("gateway_post_append", "POST /streams/{s} (JSON body, preflighted) via GatewayStore.append"),
                        ("gateway_get_read", "GET /events?from= via GatewayStore.read"),
                        ("gateway_get_last", "GET /events/last via GatewayStore.lastPosition"),
                        ("kafka_rest_publish", "POST REST proxy /v3/.../records via KafkaRestBroker.publish")]:
        r = ok_out.get(name, {})
        c.check(f"allowed origin localhost:8000: {label} accepted by Chromium", r.get("ok"), r)
    c.check("allowed origin: a 409 is readable by the page and becomes ConcurrencyError (CORS headers on error responses)",
            ok_out.get("gateway_409_readable", {}).get("value") == "ConcurrencyError", ok_out.get("gateway_409_readable"))
    # Playwright does not surface Chromium's preflight requests, so prove they happened from the servers' own logs,
    # and probe the preflight answer directly with the headers a browser sends.
    gw_log = compose("logs", "--no-log-prefix", "--since", since, "gateway").stdout
    rest_log = compose("logs", "--no-log-prefix", "--since", since, "kafka-rest").stdout
    gw_opts = [l for l in gw_log.splitlines() if "OPTIONS" in l]
    rest_opts = [l for l in rest_log.splitlines() if '"OPTIONS ' in l]
    c.check("the browser sent preflights: OPTIONS seen in the gateway log", len(gw_opts) >= 1, f"{len(gw_opts)} lines, e.g. {gw_opts[:1]}")
    c.check("the browser sent preflights: OPTIONS seen in the REST proxy access log", len(rest_opts) >= 1, f"{len(rest_opts)} lines, e.g. {[l[-120:] for l in rest_opts[:1]]}")
    hdrs = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}
    pre = {}
    for label, base, path in (("gateway", GATEWAY, "/streams/content-x"), ("rest", REST, "/v3/clusters/x/topics/t/records")):
        for origin in ("http://localhost:8000", "http://localhost:9999"):
            req = urllib.request.Request(base + path, method="OPTIONS", headers=dict(hdrs, Origin=origin))
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    pre[(label, origin)] = (r.status, dict(r.headers))
            except urllib.error.HTTPError as e:
                pre[(label, origin)] = (e.code, dict(e.headers))
    for label in ("gateway", "rest"):
        st, h = pre[(label, "http://localhost:8000")]
        c.check(f"{label} preflight for the allowed origin: 2xx, Allow-Origin echoes it, Content-Type allowed, POST allowed",
                200 <= st < 300 and h.get("Access-Control-Allow-Origin") == "http://localhost:8000"
                and "content-type" in h.get("Access-Control-Allow-Headers", "").lower() and "POST" in h.get("Access-Control-Allow-Methods", ""),
                {"status": st, "allow-origin": h.get("Access-Control-Allow-Origin"), "allow-headers": h.get("Access-Control-Allow-Headers"), "allow-methods": h.get("Access-Control-Allow-Methods")})
        st, h = pre[(label, "http://localhost:9999")]
        c.check(f"{label} preflight for a disallowed origin carries no Access-Control-Allow-Origin",
                h.get("Access-Control-Allow-Origin") is None, {"status": st})
    for name in ("gateway_post_append", "gateway_get_read", "kafka_rest_publish"):
        r = bad_out.get(name, {})
        c.check(f"disallowed origin localhost:9999: {name} is blocked by the browser", r.get("ok") is False and r.get("name") == "Error" or r.get("ok") is False, r)
    return c.finish({"blocked_origin_wire": bad_wire[:6]})


if __name__ == "__main__":
    sys.exit(main())
