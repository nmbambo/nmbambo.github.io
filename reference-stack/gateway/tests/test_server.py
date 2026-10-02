"""End-to-end HTTP tests: real ThreadingHTTPServer on an ephemeral port, MemoryStore, fake publisher."""
import json
import threading
import unittest
import urllib.error
import urllib.request
import uuid

from publisher import KafkaRestPublisher, build_record, record_failed
from server import serve
from stores import MemoryStore


def ev(n, stream="content-a"):
    return {"id": str(uuid.UUID(int=n)), "type": "ContentAdded", "stream": stream, "data": {"n": n},
            "meta": {"ts": "2026-10-02T10:00:00Z", "schema": 1, "source": "t"}}


class FakePublisher:
    pending, cluster_id = 0, "cluster-1"

    def __init__(self):
        self.sent = []

    def publish(self, events):
        self.sent.extend(events)
        return 0


def call(base, method, path, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None), r.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None), e.headers


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.pub = FakePublisher()
        self.httpd = serve(MemoryStore(), self.pub, "127.0.0.1", 0, store_name="memory",
                           allowed_origins=["http://localhost:8000"])
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_append_then_read_matches_adapter_expectations(self):
        s, body, _ = call(self.base, "POST", "/streams/content-a", {"events": [ev(1), ev(2)], "expectedVersion": -1})
        self.assertEqual(s, 200)
        self.assertEqual(body["appended"], 2)
        self.assertEqual(body["positions"], [0, 1])
        s, evs, _ = call(self.base, "GET", "/events?from=0")
        self.assertEqual([e["data"]["n"] for e in evs], [1, 2])
        s, evs, _ = call(self.base, "GET", "/events?from=1&limit=5")
        self.assertEqual([e["data"]["n"] for e in evs], [2])
        self.assertEqual(call(self.base, "GET", "/events/last")[1], {"position": 1})
        self.assertEqual(call(self.base, "GET", "/streams/content-a")[1][0]["version"], 0)
        self.assertEqual(call(self.base, "GET", "/events/" + ev(1)["id"])[0], 200)
        self.assertEqual(call(self.base, "GET", "/events/" + ev(9)["id"])[0], 404)
        self.assertEqual(call(self.base, "GET", "/events/not-an-id")[0], 404)

    def test_idempotent_and_publishes_only_new_events(self):
        call(self.base, "POST", "/streams/content-a", {"events": [ev(1)]})
        s, body, _ = call(self.base, "POST", "/streams/content-a", {"events": [ev(1), ev(2)]})
        self.assertEqual(body["appended"], 1)
        self.assertEqual([e["data"]["n"] for e in self.pub.sent], [1, 2])
        self.assertTrue(all("position" in e for e in self.pub.sent))

    def test_409_on_wrong_expected_version(self):
        call(self.base, "POST", "/streams/content-a", {"events": [ev(1)]})
        s, body, _ = call(self.base, "POST", "/streams/content-a", {"events": [ev(2)], "expectedVersion": -1})
        self.assertEqual(s, 409)
        self.assertEqual(body["actual"], 0)
        self.assertEqual(len(self.pub.sent), 1)  # nothing published for the rejected append

    def test_400_on_bad_input(self):
        self.assertEqual(call(self.base, "POST", "/streams/content-a", {"events": [{"id": "x"}]})[0], 400)
        self.assertEqual(call(self.base, "POST", "/streams/content-a", {"nope": 1})[0], 400)
        self.assertEqual(call(self.base, "GET", "/events?from=abc")[0], 400)

    def test_stream_name_is_url_decoded(self):
        name = "content-booklet/seeing-clearly/p13"
        enc = urllib.request.quote(name, safe="")
        s, _, _ = call(self.base, "POST", "/streams/" + enc, {"events": [ev(1, stream=name)]})
        self.assertEqual(s, 200)
        self.assertEqual(call(self.base, "GET", "/streams/" + enc)[1][0]["stream"], name)

    def test_cors_allows_configured_origin_only(self):
        _, _, h = call(self.base, "OPTIONS", "/streams/x", headers={"Origin": "http://localhost:8000"})
        self.assertEqual(h["Access-Control-Allow-Origin"], "http://localhost:8000")
        _, _, h = call(self.base, "OPTIONS", "/streams/x", headers={"Origin": "http://evil.example"})
        self.assertIsNone(h["Access-Control-Allow-Origin"])

    def test_health_and_unconfigured_solace(self):
        s, body, _ = call(self.base, "GET", "/health")
        self.assertEqual((s, body["store"], body["kafkaClusterId"]), (200, "memory", "cluster-1"))
        self.assertEqual(call(self.base, "POST", "/TOPIC/ingqiqo/events/x", {"a": 1})[0], 404)


class PublisherTests(unittest.TestCase):
    def test_record_shape_matches_the_browser_broker(self):
        e = ev(1)
        self.assertEqual(build_record(e), {"key": {"type": "STRING", "data": "content-a"},
                                           "value": {"type": "JSON", "data": e}})

    def test_v3_http_200_with_error_code_counts_as_failure(self):
        self.assertFalse(record_failed(200, {"error_code": 200, "offset": 3}))
        self.assertTrue(record_failed(200, {"error_code": 400, "message": "bad"}))
        self.assertTrue(record_failed(503, {}))
        self.assertFalse(record_failed(200, {}))

    def test_publish_resolves_cluster_then_posts_and_queues_on_failure(self):
        calls = []

        class Resp:
            def __init__(self, status, body):
                self.status, self._b = status, json.dumps(body).encode()

            def read(self):
                return self._b

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        mode = {"fail": False}

        def opener(req, timeout=None):
            calls.append((req.get_method(), req.full_url))
            if req.full_url.endswith("/v3/clusters"):
                return Resp(200, {"data": [{"cluster_id": "abc"}]})
            return Resp(200, {"error_code": 500 if mode["fail"] else 200})

        p = KafkaRestPublisher("http://rest:8082/", retries=0, opener=opener)
        self.assertEqual(p.publish([ev(1)]), 0)
        self.assertEqual(calls[-1], ("POST", "http://rest:8082/v3/clusters/abc/topics/ingqiqo.events/records"))
        mode["fail"] = True
        self.assertEqual(p.publish([ev(2)]), 1)
        self.assertEqual(p.pending, 1)



class FakeSolr:
    """opener= replacement: records the request URL and answers with a canned Solr /select response."""

    def __init__(self, status=200, body=None, boom=None):
        self.requests, self.status, self.boom = [], status, boom
        self.body = body if body is not None else {
            "responseHeader": {"status": 0, "QTime": 3},
            "response": {"numFound": 2, "start": 0, "docs": [
                {"id": "a#1", "title": "A", "score": 2.5}, {"id": "b#2", "title": "B", "score": 1.5}]}}

    def __call__(self, req, timeout=None):
        self.requests.append(req.full_url)
        if self.boom:
            raise self.boom
        if self.status != 200:
            raise urllib.error.HTTPError(req.full_url, self.status, "x", {}, None)

        class R:
            status = 200

            def __init__(s, b):
                s.b = b

            def read(s):
                return json.dumps(s.b).encode()

            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False
        return R(self.body)


class SearchEndpointTests(unittest.TestCase):
    def start(self, solr_url="http://solr:8983/solr/ingqiqo", **fake):
        self.solr = FakeSolr(**fake)
        httpd = serve(MemoryStore(), FakePublisher(), "127.0.0.1", 0, store_name="memory",
                      allowed_origins=["http://localhost:8000"], solr_url=solr_url, opener=self.solr)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % httpd.server_address[1]

    def get(self, path, **kw):
        return call(self.base, "GET", path, **kw)

    def test_not_configured_is_404(self):
        self.start(solr_url=None)
        self.assertEqual(self.get("/search?q=x")[0], 404)

    def test_proxies_translated_params_only(self):
        self.start()
        status, body, headers = self.get("/search?q=meaning+coherence&algorithm=bm25&rows=5",
                                         headers={"Origin": "http://localhost:8000"})
        self.assertEqual(status, 200)
        self.assertEqual([r["id"] for r in body["results"]], ["a#1", "b#2"])
        self.assertEqual((body["total"], body["algorithm"], body["qtimeMs"]), (2, "bm25", 3))
        self.assertEqual(headers["Access-Control-Allow-Origin"], "http://localhost:8000")
        from urllib.parse import parse_qs, urlsplit
        sent = urlsplit(self.solr.requests[0])
        self.assertEqual(sent.path, "/solr/ingqiqo/select")
        params = {k: v[0] for k, v in parse_qs(sent.query).items()}
        self.assertEqual(params["q"], "meaning coherence")
        self.assertEqual((params["defType"], params["rows"], params["qf"]), ("edismax", "5", "title^3 section^2 tags^2 text"))
        self.assertEqual(set(params), {"q", "qf", "mm", "defType", "fl", "rows", "start", "sort", "lowercaseOperators", "tie", "wt"})

    def test_default_algorithm_and_each_algorithm_accepted(self):
        self.start()
        self.assertEqual(self.get("/search?q=spine")[1]["algorithm"], "bm25")
        for a in ("boolean", "bm25", "fuzzy", "prefix", "phrase"):
            self.assertEqual(self.get(f"/search?q=spine&algorithm={a}")[0], 200, a)

    def test_allowlist_rejects_everything_else_without_calling_solr(self):
        self.start()
        for bad in ("/search?q=x&fl=*", "/search?q=x&defType=lucene", "/search?q=x&stream.url=http://x", "/search?q=x&wt=xml",
                    "/search?q=x&shards=evil", "/search?q=x&q=y", "/search?q=x&rows=0", "/search?q=x&rows=101",
                    "/search?q=x&rows=abc", "/search?q=x&start=-1", "/search?q=x&start=1001", "/search?q=x&algorithm=semantic",
                    "/search?q=x&json.facet=%7B%7D", "/search?q=x&fq=deleted:true"):
            status, body, _ = self.get(bad)
            self.assertEqual(status, 400, bad)
            self.assertIn("error", body)
        self.assertEqual(self.solr.requests, [])

    def test_local_params_and_malformed_boolean_are_400_with_position(self):
        self.start()
        s, b, _ = self.get("/search?q=%7B!dismax+qf%3Dtext%7Dx&algorithm=bm25")
        self.assertEqual(s, 400)
        self.assertIn("local parameters", b["error"])
        s, b, _ = self.get("/search?q=a+AND&algorithm=boolean")
        self.assertEqual((s, b["position"]), (400, 2))
        self.assertEqual(self.solr.requests, [])

    def test_empty_query_answers_without_solr(self):
        self.start()
        s, b, _ = self.get("/search?q=&algorithm=bm25")
        self.assertEqual((s, b["total"], b["results"]), (200, 0, []))
        self.assertEqual(self.solr.requests, [])

    def test_backend_failures_are_generic(self):
        self.start(boom=OSError("connection refused: solr:8983"))
        s, b, _ = self.get("/search?q=x")
        self.assertEqual(s, 503)
        self.assertNotIn("solr:8983", json.dumps(b))
        self.start(status=400)
        self.assertEqual(self.get("/search?q=x")[0], 502)


if __name__ == "__main__":
    unittest.main()
