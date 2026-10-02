"""Offline tests for solr_sink.py against an in-memory Solr that implements the same compare-and-set rules on
_version_ (-1 = must not exist, N = must equal) and real-time get. No network, no Kafka."""
import io
import json
import unittest
import urllib.error

from solr_sink import (Delete, SolrClient, SolrConflict, SolrError, SolrSink, Upsert, row_fields, to_ops)

T = "ingqiqo.events"
D = "lakebase.public.decisions"
P = "lakebase.public.parties"


class FakeSolr:
    def __init__(self):
        self.docs, self.clock, self.adds, self.before_add = {}, 1000, 0, None

    def get(self, doc_id):
        d = self.docs.get(doc_id)
        return None if d is None else {k: d[k] for k in ("id", "_version_", "ev_pos", "ev_id", "deleted") if k in d}

    def add(self, doc):
        if self.before_add:
            hook, self.before_add = self.before_add, None
            hook(self)
        cur = self.docs.get(doc["id"])
        want = doc["_version_"]
        if (want == -1 and cur is not None) or (want > 0 and (cur is None or cur["_version_"] != want)):
            raise SolrConflict("409")
        self.clock += 1
        self.docs[doc["id"]] = dict(doc, _version_=self.clock)
        self.adds += 1

    def visible(self):
        return {k: v for k, v in self.docs.items() if not v.get("deleted")}

    def state(self):
        return {k: {f: v for f, v in d.items() if f != "_version_"} for k, d in self.docs.items()}


def env(id_, type_, data, version=None):
    e = {"id": id_, "type": type_, "stream": "content-x", "data": data, "meta": {"ts": "t", "schema": 1, "source": "t"}}
    if version is not None:
        e["version"] = version
    return json.dumps(e).encode()


def add(eid, title, v, doc="p#1"):
    return env(eid, "ContentAdded" if v == 0 else "ContentChanged",
               {"id": doc, "title": title, "section": "s", "text": f"body {title}", "tags": ["page"], "type": "page",
                "source": "p.html", "url": "/p"}, v)


def rm(eid, v, doc="p#1"):
    return env(eid, "ContentRemoved", {"id": doc}, v)


def dbz(op, row, lsn, table="decisions", ts=1):
    key = {"decision_id": row["decision_id"]} if table == "decisions" else {"party_id": row["party_id"]}
    v = {"op": op, "ts_ms": ts, "source": {"lsn": lsn, "table": table}, "before": row if op == "d" else None,
         "after": None if op == "d" else row}
    return json.dumps(key).encode(), json.dumps(v).encode()


def dec(i, title="Choose a vendor", status="open", stakes=3):
    return {"decision_id": i, "party_id": 7, "title": title, "status": status, "stakes": stakes, "payload": '{"note": "alpha"}'}


class ContentEvents(unittest.TestCase):
    def setUp(self):
        self.solr = FakeSolr()
        self.sink = SolrSink(self.solr)

    def feed(self, *msgs):
        return [self.sink.handle(T, b"content-x", m)[0] for m in msgs]

    def test_upsert_maps_every_field(self):
        self.assertEqual(self.feed(add("e1", "A", 0)), ["applied"])
        d = self.solr.docs["p#1"]
        self.assertEqual((d["title"], d["section"], d["text"], d["tags"], d["type"], d["source"], d["url"]),
                         ("A", "s", "body A", ["page"], "page", "p.html", "/p"))
        self.assertEqual((d["ev_pos"], d["ev_id"], d["deleted"]), (0, "e1", False))

    def test_same_event_twice_changes_nothing(self):
        self.feed(add("e1", "A", 0))
        before, writes = self.solr.state(), self.solr.adds
        self.assertEqual(self.feed(add("e1", "A", 0)), ["duplicate"])
        self.assertEqual((self.solr.state(), self.solr.adds), (before, writes))

    def test_replaying_the_log_twice_equals_once(self):
        log = [add("e1", "A", 0), add("e2", "B", 1), add("o1", "Other", 0, doc="q#1"), rm("e3", 2),
               add("e4", "C", 3), rm("o2", 1, doc="q#1")]
        a, b = FakeSolr(), FakeSolr()
        for m in log:
            SolrSink(a).handle(T, b"k", m)
        s = SolrSink(b)
        for m in log:
            s.handle(T, b"k", m)
        applied_once = s.counts["applied"]
        for m in log:
            s.handle(T, b"k", m)
        self.assertEqual(a.state(), b.state())
        self.assertEqual(sorted(b.visible()), ["p#1"])
        self.assertEqual(b.docs["p#1"]["title"], "C")
        self.assertEqual(s.counts["applied"], applied_once)   # the whole second pass was a no-op:
        self.assertEqual(s.counts["duplicate"] + s.counts["stale"], len(log))   # last event per doc = duplicate, older = stale

    def test_out_of_order_older_never_overwrites_newer(self):
        self.assertEqual(self.feed(add("e3", "v3", 3), add("e1", "v1", 1), add("e2", "v2", 2)), ["applied", "stale", "stale"])
        self.assertEqual(self.solr.docs["p#1"]["title"], "v3")

    def test_late_change_cannot_resurrect_a_removed_document(self):
        self.assertEqual(self.feed(add("e0", "A", 0), rm("e2", 2), add("e1", "B", 1)), ["applied", "applied", "stale"])
        self.assertTrue(self.solr.docs["p#1"]["deleted"])
        self.assertEqual(self.solr.visible(), {})

    def test_remove_arriving_before_the_add_leaves_a_tombstone(self):
        self.assertEqual(self.feed(rm("e5", 5), add("e0", "A", 0), add("e4", "Z", 4)), ["applied", "stale", "stale"])
        self.assertEqual(self.solr.visible(), {})

    def test_a_newer_add_after_a_remove_brings_the_document_back(self):
        self.feed(add("e0", "A", 0), rm("e1", 1), add("e2", "B", 2))
        self.assertFalse(self.solr.docs["p#1"]["deleted"])
        self.assertEqual(self.solr.docs["p#1"]["title"], "B")

    def test_change_replaces_the_whole_document(self):
        self.feed(add("e0", "A", 0))
        self.feed(env("e1", "ContentChanged", {"id": "p#1", "title": "B", "type": "page"}, 1))
        self.assertNotIn("text", self.solr.docs["p#1"])

    def test_events_without_a_version_always_apply(self):
        self.assertEqual(self.feed(add("e1", "A", 5), env("e2", "ContentChanged", {"id": "p#1", "title": "NoVer"})), ["applied", "applied"])
        self.assertEqual(self.solr.docs["p#1"]["title"], "NoVer")
        self.assertEqual(self.solr.docs["p#1"]["ev_pos"], 5)   # the stored position is kept

    def test_unusable_messages_are_skipped(self):
        for raw in (b"not json", b"{}", env("x", "SearchSubmitted", {"algorithm": "bm25"}), env("y", "ContentAdded", {"title": "no id"}), None):
            self.assertEqual(self.sink.handle(T, b"k", raw), ["skipped"])
        self.assertEqual(self.solr.docs, {})

    def test_conflict_with_a_concurrent_newer_write_retries_and_then_drops_the_stale_event(self):
        self.feed(add("e0", "A", 0))
        newer = {"id": "p#1", "title": "N", "ev_pos": 9, "ev_id": "eN", "deleted": False}
        self.solr.before_add = lambda s: s.add(dict(newer, _version_=s.docs["p#1"]["_version_"]))
        self.assertEqual(self.feed(add("e1", "B", 1)), ["stale"])
        self.assertEqual(self.solr.docs["p#1"]["title"], "N")
        self.assertEqual(self.sink.counts["conflict_retries"], 1)

    def test_conflict_with_an_unrelated_write_retries_and_applies(self):
        self.feed(add("e0", "A", 0))
        self.solr.before_add = lambda s: s.add({"id": "p#1", "title": "A2", "ev_pos": 0, "ev_id": "x", "deleted": False,
                                                "_version_": s.docs["p#1"]["_version_"]})
        self.assertEqual(self.feed(add("e1", "B", 1)), ["applied"])
        self.assertEqual(self.solr.docs["p#1"]["title"], "B")

    def test_gives_up_after_repeated_conflicts(self):
        class Always(FakeSolr):
            def add(self, doc):
                raise SolrConflict("409")
        with self.assertRaises(SolrError):
            SolrSink(Always(), max_attempts=3).handle(T, b"k", add("e1", "A", 0))

    def test_solr_down_raises_so_the_offset_is_not_committed(self):
        class Down(FakeSolr):
            def get(self, doc_id):
                raise SolrError("solr unreachable")
        with self.assertRaises(SolrError):
            SolrSink(Down()).handle(T, b"k", add("e1", "A", 0))


class DebeziumEvents(unittest.TestCase):
    def setUp(self):
        self.solr = FakeSolr()
        self.sink = SolrSink(self.solr)

    def feed(self, key_value, topic=D):
        k, v = key_value
        return self.sink.handle(topic, k, v)[0]

    def test_insert_update_snapshot_and_delete(self):
        self.assertEqual(self.feed(dbz("c", dec(1), 100)), "applied")
        d = self.solr.docs["decision:1"]
        self.assertEqual((d["title"], d["section"], d["type"], d["source"], d["ev_pos"]),
                         ("Choose a vendor", "open", "decision", "lakebase.public.decisions", 100))
        self.assertIn("alpha", d["text"])                     # jsonb payload flattened into text
        self.assertEqual(d["tags"], ["open", "stakes-3"])
        self.assertEqual(self.feed(dbz("u", dec(1, "Renamed"), 200)), "applied")
        self.assertEqual(self.solr.docs["decision:1"]["title"], "Renamed")
        self.assertEqual(self.feed(dbz("r", dec(2), 50)), "applied")
        self.assertEqual(self.feed(dbz("d", dec(1), 300)), "applied")
        self.assertEqual(sorted(self.solr.visible()), ["decision:2"])

    def test_tombstone_after_delete_is_a_noop_and_alone_it_deletes(self):
        self.feed(dbz("c", dec(1), 100))
        self.feed(dbz("d", dec(1), 300))
        self.assertEqual(self.sink.handle(D, b'{"decision_id": 1}', None), ["duplicate"])
        self.feed(dbz("c", dec(2), 100))
        self.assertEqual(self.sink.handle(D, b'{"decision_id": 2}', None), ["applied"])   # delete event never arrived
        self.assertEqual(self.solr.visible(), {})
        self.assertEqual(self.solr.docs["decision:2"]["ev_pos"], 100)
        self.assertEqual(self.sink.handle(D, b'{"decision_id": 99}', None), ["noop"])
        self.assertNotIn("decision:99", self.solr.docs)

    def test_out_of_order_changes_by_lsn(self):
        self.assertEqual([self.feed(dbz("u", dec(1, "new"), 500)), self.feed(dbz("u", dec(1, "old"), 400)),
                          self.feed(dbz("c", dec(1, "older"), 300))], ["applied", "stale", "stale"])
        self.assertEqual(self.solr.docs["decision:1"]["title"], "new")
        self.assertEqual(self.feed(dbz("d", dec(1), 450)), "stale")          # a delete older than the last update loses too
        self.assertFalse(self.solr.docs["decision:1"]["deleted"])

    def test_replay_twice_is_identical(self):
        msgs = [dbz("r", dec(1), 10), dbz("u", dec(1, "x"), 20), dbz("c", dec(2), 30), dbz("d", dec(2), 40)]
        for m in msgs:
            self.feed(m)
        once = self.solr.state()
        for m in msgs * 2:
            self.feed(m)
        self.assertEqual(self.solr.state(), once)

    def test_parties_and_ignored_tables(self):
        p = {"party_id": 4, "label": "Synthetic Party 000004", "kind": "organisation", "region": "Gauteng"}
        self.assertEqual(self.feed(dbz("c", p, 5, table="parties"), topic=P), "applied")
        self.assertEqual(self.solr.docs["party:4"]["title"], "Synthetic Party 000004")
        self.assertEqual(self.sink.handle("lakebase.public.decision_events", b"{}", dbz("c", dec(1), 1)[1]), ["skipped"])

    def test_schema_wrapped_payload_and_junk(self):
        k, v = dbz("c", dec(3), 7)
        wrapped = json.dumps({"schema": {}, "payload": json.loads(v)}).encode()
        self.assertEqual(self.sink.handle(D, k, wrapped), ["applied"])
        for junk in (b"nope", b'{"op": "t"}', b'{"op": "c", "after": null}'):
            self.assertEqual(self.sink.handle(D, k, junk), ["skipped"])

    def test_row_fields_unknown_table(self):
        self.assertEqual(row_fields("decision_events", {}), (None, None))
        self.assertEqual(to_ops("lakebase.public.decision_events", b"{}", b"{}"), [])


class SolrClientHttp(unittest.TestCase):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def test_get_and_add_urls_and_errors(self):
        seen = []

        def opener(req, timeout=None):
            seen.append((req.get_method(), req.full_url, req.data))
            if "/get?" in req.full_url:
                return self.Resp(json.dumps({"doc": {"id": "a b", "_version_": 7}}).encode())
            return self.Resp(b"{}")
        c = SolrClient("http://solr:8983/solr/ingqiqo/", commit_within_ms=250, opener=opener)
        self.assertEqual(c.get("a b")["_version_"], 7)
        self.assertEqual(seen[0][1], "http://solr:8983/solr/ingqiqo/get?id=a+b&fl=id%2C_version_%2Cev_pos%2Cev_id%2Cdeleted")
        c.add({"id": "x", "_version_": -1})
        self.assertEqual((seen[1][0], seen[1][1]), ("POST", "http://solr:8983/solr/ingqiqo/update?commitWithin=250&overwrite=true"))
        self.assertEqual(json.loads(seen[1][2]), [{"id": "x", "_version_": -1}])

        def conflict(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, io.BytesIO(b"{}"))

        def broken(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 500, "x", {}, io.BytesIO(b"{}"))

        def down(req, timeout=None):
            raise urllib.error.URLError("refused")
        with self.assertRaises(SolrConflict):
            SolrClient("http://s", opener=conflict).add({"id": "x"})
        with self.assertRaises(SolrError) as cm:
            SolrClient("http://s", opener=broken).get("x")
        self.assertNotIsInstance(cm.exception, SolrConflict)
        with self.assertRaises(SolrError):
            SolrClient("http://s", opener=down).get("x")

    def test_missing_document_is_none(self):
        c = SolrClient("http://s", opener=lambda req, timeout=None: self.Resp(b'{"doc": null}'))
        self.assertIsNone(c.get("nope"))


if __name__ == "__main__":
    unittest.main()
