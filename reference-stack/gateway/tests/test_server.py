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


if __name__ == "__main__":
    unittest.main()
