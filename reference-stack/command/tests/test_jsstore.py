import unittest

from envelope import ConcurrencyError
from tests.fakes import FakeLog, Loop, ev
from jsstore import JetStreamStore, subject_for, stream_from_subject, H_VERSION


class SubjectTests(unittest.TestCase):
    def test_plain_names_pass_through(self):
        self.assertEqual(subject_for("content-engage#e3"), "es.content-engage#e3")
        self.assertEqual(subject_for("agent"), "es.agent")

    def test_slashes_kept_dots_and_wildcards_encoded(self):
        s = subject_for("content-booklet/seeing-clearly/p13")
        self.assertEqual(s, "es.content-booklet/seeing-clearly/p13")
        for bad in ("a.b", "a*b", "a>b", "a b", "a\tb", "a\nb", "é", "a%b"):
            subj = subject_for(bad)
            token = subj[len("es."):]
            self.assertNotRegex(token, r"[.*>\s]", bad)        # exactly one subject token, no wildcard
            self.assertEqual(stream_from_subject(subj), bad)   # and reversible

    def test_injection_cannot_widen_the_subject(self):
        self.assertEqual(subject_for("x.>").count("."), 1)
        self.assertEqual(subject_for("*").count("*"), 0)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.lp = Loop()
        self.log = FakeLog()
        self.store = JetStreamStore(self.log, loop=self.lp.loop)
        self.lp.run(self.store.load_index())

    def tearDown(self):
        self.lp.stop()

    def test_append_assigns_dense_positions_and_versions(self):
        r = self.store.append("s1", [ev("s1", 0), ev("s1", 1)], -1)
        self.assertEqual((r["appended"], r["positions"]), (2, [0, 1]))
        r = self.store.append("s2", [ev("s2", 2)], -1)
        self.assertEqual(r["positions"], [2])
        self.assertEqual([e["version"] for e in self.store.read_stream("s1")], [0, 1])
        self.assertEqual(self.store.last_position(), 2)

    def test_idempotent_repost_appends_nothing_and_ignores_stale_expected_version(self):
        evs = [ev("s1", 0), ev("s1", 1)]
        self.store.append("s1", evs, -1)
        r = self.store.append("s1", evs, -1)          # a retry after a lost response
        self.assertEqual((r["appended"], r["skipped"]), (0, 2))
        self.assertEqual(self.store.append("s1", evs)["appended"], 0)
        self.assertEqual(len(self.log.msgs), 2)

    def test_expected_version_conflicts(self):
        self.store.append("s1", [ev("s1", 0), ev("s1", 1), ev("s1", 2)], -1)
        for expected, actual in ((-1, 2), (1, 2), (5, 2)):
            with self.assertRaises(ConcurrencyError) as cm:
                self.store.append("s1", [ev("s1", 9)], expected)
            self.assertEqual((cm.exception.expected, cm.exception.actual), (expected, actual))
        self.assertIsNone(self.store.get(ev("s1", 9)["id"]), "a 409 stores nothing")
        self.assertEqual(self.store.append("s1", [ev("s1", 9)], 2)["appended"], 1)

    def test_new_stream_requires_minus_one(self):
        with self.assertRaises(ConcurrencyError):
            self.store.append("fresh", [ev("fresh", 0)], 0)
        self.assertEqual(self.store.append("fresh", [ev("fresh", 0)], -1)["appended"], 1)

    def test_partially_existing_request_appends_only_the_new_ones_with_continuing_versions(self):
        self.store.append("s1", [ev("s1", 0)], -1)
        r = self.store.append("s1", [ev("s1", 0), ev("s1", 1)], 0)
        self.assertEqual((r["appended"], r["skipped"]), (1, 1))
        self.assertEqual([e["version"] for e in self.store.read_stream("s1")], [0, 1])

    def test_broker_cas_catches_a_concurrent_writer(self):
        """Another process appends to the subject between our read and our publish: JetStream refuses ours."""
        self.store.append("s1", [ev("s1", 0)], -1)
        subject = subject_for("s1")

        def rival(_subject):
            self.log.msgs.append(type(self.log.msgs[0])(len(self.log.msgs) + 1, subject, b"{}", {H_VERSION: "1"}))
        self.log.before_publish = rival
        with self.assertRaises(ConcurrencyError) as cm:
            self.store.append("s1", [ev("s1", 5)], 0)
        self.assertEqual(cm.exception.actual, 1)
        self.assertIsNone(self.store.get(ev("s1", 5)["id"]))

    def test_reads_are_ordered_inclusive_and_limited(self):
        for i in range(6):
            self.store.append(f"s{i % 2}", [ev(f"s{i % 2}", i)])
        self.assertEqual([e["position"] for e in self.store.read(0, 100)], [0, 1, 2, 3, 4, 5])
        self.assertEqual([e["position"] for e in self.store.read(2, 2)], [2, 3])
        self.assertEqual(self.store.read(6, 10), [])
        self.assertEqual(self.store.read(-5, 1)[0]["position"], 0)

    def test_get_by_id_and_envelope_roundtrip(self):
        e = ev("s1", 0, id_="a" * 64)
        self.store.append("s1", [e], -1)
        got = self.store.get("a" * 64)
        self.assertEqual({k: got[k] for k in ("id", "type", "stream", "data", "meta")}, e)
        self.assertEqual((got["position"], got["version"]), (0, 0))
        self.assertIsNone(self.store.get("b" * 64))

    def test_index_is_rebuilt_from_the_log_after_a_restart(self):
        self.store.append("s1", [ev("s1", 0), ev("s1", 1)], -1)
        again = JetStreamStore(self.log, loop=self.lp.loop)
        self.assertEqual(self.lp.run(again.load_index()), 2)
        self.assertEqual(again.append("s1", [ev("s1", 0), ev("s1", 1)], -1)["appended"], 0)
        self.assertEqual(again.append("s1", [ev("s1", 2)], 1)["appended"], 1)

    def test_odd_stream_names_roundtrip(self):
        name = "content-booklet/seeing clearly.p13*>"
        self.store.append(name, [ev(name, 0)], -1)
        self.assertEqual(len(self.store.read_stream(name)), 1)
        self.assertEqual(self.store.read_stream("content-booklet/seeing"), [])


if __name__ == "__main__":
    unittest.main()
