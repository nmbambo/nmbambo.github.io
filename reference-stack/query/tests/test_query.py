"""Query service: read model + search + HTTP, against the site's own event log (no NATS needed)."""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from consumer import drain  # noqa: E402
from readmodel import ReadModel  # noqa: E402
from search import ALGORITHMS, search  # noqa: E402
from server import serve  # noqa: E402

LOG = os.path.join(HERE, "..", "..", "..", "data", "events.ndjson")


def site_messages():
    with open(LOG, encoding="utf-8") as fh:
        return [(i, "es." + json.loads(l)["stream"], l.encode(), {"Ingqiqo-Version": "0"}) for i, l in enumerate(fh, 1)]


def cdc(seq, op, lsn, row, before=None):
    return (seq, "lakebase.public.decisions", json.dumps({"op": op, "before": before, "after": row, "source": {"lsn": lsn}}).encode(), {})


class ReadModelTest(unittest.TestCase):
    def setUp(self):
        self.rm = ReadModel()
        self.msgs = site_messages()
        drain(self.rm, "es", self.msgs)

    def test_redelivery_is_a_no_op(self):
        before = self.rm.dump()
        self.assertEqual(drain(self.rm, "es", self.msgs), {})
        self.assertEqual(self.rm.dump(), before)

    def test_replay_from_zero_is_identical(self):
        other = ReadModel()
        for i in range(0, len(self.msgs), 7):              # different batch boundaries, same result
            drain(other, "es", self.msgs[i:i + 7])
        self.assertEqual(other.dump(), self.rm.dump())

    def test_every_method_answers(self):
        c = self.rm.reader()
        for alg in ALGORITHMS:
            r = search(c, "meaning coherence", algorithm=alg, rows=5, version=self.rm.version)
            self.assertGreater(r["total"], 0, alg)
            self.assertLessEqual(len(r["results"]), 5)

    def test_boolean_semantics(self):
        c = self.rm.reader()
        allc = search(c, "coherence", algorithm="boolean", rows=100)["total"]
        neg = search(c, "coherence -quantum", algorithm="boolean", rows=100)["total"]
        self.assertLessEqual(neg, allc)
        n = search(c, "NOT coherence", algorithm="boolean", rows=100)["total"]
        self.assertEqual(n + allc, self.rm.stats()["docs"])

    def test_cdc_lsn_order_and_delete(self):
        rm = self.rm
        drain(rm, "cdc", [cdc(1, "c", 10, {"decision_id": 1, "title": "Zebra decision"})])
        c = rm.reader()
        self.assertEqual(search(c, "zebra", algorithm="bm25")["total"], 1)
        drain(rm, "cdc", [cdc(2, "d", 30, None, {"decision_id": 1}), cdc(3, "u", 20, {"decision_id": 1, "title": "Zebra again"})])  # late older update
        self.assertEqual(search(rm.reader(), "zebra", algorithm="bm25")["total"], 0)


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rm = ReadModel()
        drain(cls.rm, "es", site_messages())
        cls.httpd = serve(cls.rm, "127.0.0.1", 0, allowed_origins=["http://localhost:8000"])
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def get(self, path, origin=None):
        req = urllib.request.Request(self.base + path, headers={"Origin": origin} if origin else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read()), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), e.headers

    def test_search_contract(self):
        s, body, h = self.get("/search?q=title%3Aspine&algorithm=boolean&rows=2&start=0", "http://localhost:8000")
        self.assertEqual(s, 200)
        self.assertEqual(set(body), {"algorithm", "total", "qtimeMs", "ftsQuery", "results"})
        self.assertEqual(h["Access-Control-Allow-Origin"], "http://localhost:8000")

    def test_bad_syntax_is_400_with_position(self):
        s, body, _ = self.get("/search?q=%28a%20AND&algorithm=boolean")
        self.assertEqual(s, 400)
        self.assertIn("position", body)

    def test_unknown_origin_gets_no_cors_and_writes_are_refused(self):
        _, _, h = self.get("/stats", "http://evil.example")
        self.assertIsNone(h.get("Access-Control-Allow-Origin"))
        req = urllib.request.Request(self.base + "/search", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 405)


if __name__ == "__main__":
    unittest.main()
