import json
import unittest

from logic import PROJECTOR, process, project, parse_message
from repo import MemoryRepo

T = "ingqiqo.events"


def msg(id_, type_, data, version=None, stream="content-a"):
    e = {"id": id_, "type": type_, "stream": stream, "data": data,
         "meta": {"ts": "2026-10-02T10:00:00Z", "schema": 1, "source": "t"}}
    if version is not None:
        e["version"] = version
    return json.dumps(e).encode()


def doc(i, title, v):
    return msg(f"e{i}", "ContentAdded" if v == 0 else "ContentChanged",
               {"id": "page#1", "title": title, "section": "s", "type": "page", "url": "/x", "hash": f"h{v}"}, v)


def snapshot(r):
    return (dict(r.docs), {k: dict(v) for k, v in r.stats.items()}, dict(r.checkpoints))


class IdempotencyTests(unittest.TestCase):
    def test_same_event_twice_changes_nothing(self):
        r = MemoryRepo()
        self.assertEqual(process(r, T, 0, 0, doc(1, "A", 0)), "applied")
        before = snapshot(r)
        self.assertEqual(process(r, T, 0, 1, doc(1, "A", 0)), "duplicate")  # redelivered at a later offset
        after = snapshot(r)
        self.assertEqual(before[0], after[0])
        self.assertEqual(before[1], after[1])
        self.assertEqual(after[2][(PROJECTOR, T, 0)], 2)  # checkpoint still advances

    def test_counters_are_not_double_counted(self):
        r = MemoryRepo()
        m = msg("s1", "SearchSubmitted", {"algorithm": "bm25", "query": "q"}, stream="search-x")
        for off in range(5):
            process(r, T, 0, off, m)
        self.assertEqual(r.stats["bm25"]["submitted"], 1)

    def test_replaying_the_whole_log_twice_equals_once(self):
        log = [doc(1, "A", 0), doc(2, "B", 1),
               msg("s1", "SearchSubmitted", {"algorithm": "fuzzy"}),
               msg("s2", "ResultOpened", {"algorithm": "fuzzy"}),
               msg("s3", "AlgorithmOverridden", {"proposed": "fuzzy", "chosen": "bm25"}),
               msg("e9", "ContentRemoved", {"id": "page#1"}, 2)]
        once, twice = MemoryRepo(), MemoryRepo()
        for i, m in enumerate(log):
            process(once, T, 0, i, m)
        for rnd in range(2):
            for i, m in enumerate(log):
                process(twice, T, 0, i + rnd * len(log), m)
        self.assertEqual(snapshot(once)[:2], snapshot(twice)[:2])
        self.assertTrue(once.docs["page#1"]["removed"])
        self.assertEqual(once.stats["fuzzy"], {"submitted": 1, "opened": 1, "overridden_from": 1, "overridden_to": 0})
        self.assertEqual(once.stats["bm25"]["overridden_to"], 1)

    def test_out_of_order_older_version_does_not_overwrite_newer(self):
        r = MemoryRepo()
        process(r, T, 0, 0, doc(2, "NEW", 1))
        process(r, T, 0, 1, doc(1, "OLD", 0))
        self.assertEqual(r.docs["page#1"]["title"], "NEW")

    def test_checkpoint_never_moves_backwards(self):
        r = MemoryRepo()
        process(r, T, 0, 10, doc(1, "A", 0))
        process(r, T, 0, 3, msg("zz", "Unknown", {}))
        self.assertEqual(r.checkpoints[(PROJECTOR, T, 0)], 11)

    def test_checkpoints_are_per_partition(self):
        r = MemoryRepo()
        process(r, T, 0, 4, doc(1, "A", 0))
        process(r, T, 1, 9, doc(2, "B", 1))
        self.assertEqual(r.load_checkpoint(PROJECTOR, T, 0), 5)
        self.assertEqual(r.load_checkpoint(PROJECTOR, T, 1), 10)
        self.assertIsNone(r.load_checkpoint(PROJECTOR, T, 2))

    def test_unknown_type_and_poison_messages_advance_checkpoint_only(self):
        r = MemoryRepo()
        self.assertEqual(process(r, T, 0, 0, msg("u1", "SomethingNew", {})), "applied")
        self.assertEqual(process(r, T, 0, 1, b"not json"), "skipped")
        self.assertEqual(process(r, T, 0, 2, b'{"id": 5}'), "skipped")
        self.assertEqual((r.docs, r.stats), ({}, {}))
        self.assertEqual(r.checkpoints[(PROJECTOR, T, 0)], 3)

    def test_failure_mid_event_rolls_back_event_id_checkpoint_and_model(self):
        class Boom(MemoryRepo):
            def apply(self, op):
                raise RuntimeError("db down")
        r = Boom()
        with self.assertRaises(RuntimeError):
            process(r, T, 0, 0, doc(1, "A", 0))
        self.assertEqual((r.processed, r.checkpoints), (set(), {}))  # so the retry will apply it

    def test_remove_before_add_leaves_a_tombstone_that_a_stale_add_cannot_revive(self):
        r = MemoryRepo()
        process(r, T, 0, 0, msg("e2", "ContentRemoved", {"id": "page#1"}, 1))
        process(r, T, 0, 1, doc(1, "A", 0))
        self.assertTrue(r.docs["page#1"]["removed"])


class PureProjectionTests(unittest.TestCase):
    def test_project_is_deterministic(self):
        ev = parse_message(doc(1, "A", 0))
        self.assertEqual(project(ev), project(ev))

    def test_parse_message_rejects_non_envelopes(self):
        for raw in (b"", b"[]", b'{"id":"x"}', b'{"id":"x","type":"T","data":[]}', None):
            self.assertIsNone(parse_message(raw))

    def test_content_event_without_doc_id_yields_no_ops(self):
        self.assertEqual(project({"id": "x", "type": "ContentAdded", "data": {}}), [])


if __name__ == "__main__":
    unittest.main()
