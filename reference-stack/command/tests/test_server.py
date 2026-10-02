import json
import threading
import unittest
import urllib.error
import urllib.request

from tests.fakes import FakeLog, Loop, ev
from jsstore import JetStreamStore
from server import serve


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lp = Loop()
        cls.store = JetStreamStore(FakeLog(), loop=cls.lp.loop)
        cls.lp.run(cls.store.load_index())
        cls.httpd = serve(cls.store, "127.0.0.1", 0, allowed_origins=["http://localhost:8000"],
                          health=lambda: {"ready": True, "store": "jetstream"})
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.lp.stop()

    def call(self, method, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None), dict(r.headers)
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (json.loads(raw) if raw else None), dict(e.headers)

    def test_gateway_compatible_routes(self):
        s, b, _ = self.call("POST", "/streams/content-a", {"events": [ev("content-a", 1)], "expectedVersion": -1})
        self.assertEqual((s, b["appended"], b["positions"]), (200, 1, [0]))
        s, b, _ = self.call("POST", "/streams/content-a", {"events": [ev("content-a", 1)], "expectedVersion": -1})
        self.assertEqual((s, b["appended"]), (200, 0))
        s, b, _ = self.call("POST", "/streams/content-a", {"events": [ev("content-a", 2)], "expectedVersion": -1})
        self.assertEqual((s, b["actual"], b["expected"]), (409, 0, -1))
        s, b, _ = self.call("GET", "/events?from=0&limit=10")
        self.assertEqual([e["id"] for e in b], [ev("content-a", 1)["id"]])
        self.assertEqual(self.call("GET", "/events/last")[1], {"position": 0})
        self.assertEqual(self.call("GET", f"/events/{ev('content-a', 1)['id']}")[0], 200)
        self.assertEqual(self.call("GET", f"/events/{ev('content-a', 7)['id']}")[0], 404)
        self.assertEqual(len(self.call("GET", "/streams/content-a")[1]), 1)
        self.assertEqual(self.call("GET", "/streams/nothing")[1], [])

    def test_validation_and_cors(self):
        s, b, _ = self.call("POST", "/streams/content-a", {"events": [{"id": "nope"}]})
        self.assertEqual(s, 400)
        s, _, h = self.call("OPTIONS", "/streams/x", headers={"Origin": "http://localhost:8000"})
        self.assertEqual((s, h["Access-Control-Allow-Origin"]), (204, "http://localhost:8000"))
        _, _, h = self.call("OPTIONS", "/streams/x", headers={"Origin": "http://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", h)
        s, b, _ = self.call("GET", "/health")
        self.assertEqual((s, b["status"]), (200, "ok"))
        self.assertEqual(self.call("GET", "/search?q=x")[0], 404, "search is the query service's job")


if __name__ == "__main__":
    unittest.main()
