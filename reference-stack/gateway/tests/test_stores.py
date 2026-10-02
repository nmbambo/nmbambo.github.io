"""Store semantics against MemoryStore (reference) and KurrentStore (against an in-process fake client).
MessageDbStore: only its connection setup is unit-tested; the SQL paths are covered by tests/live/02_gateway_stores.py."""
import json
import types
import unittest
import uuid

from envelope import ConcurrencyError, store_uuid
from stores import KurrentStore, MemoryStore


def ev(n, stream="content-a", **over):
    e = {"id": str(uuid.UUID(int=n)), "type": "ContentAdded", "stream": stream, "data": {"n": n},
         "meta": {"ts": "2026-10-02T10:00:00Z", "schema": 1, "source": "t"}}
    e.update(over)
    return e


# ---- a tiny fake of the kurrentdbclient surface the store uses ---------------------------------------
class FakeWrongVersion(Exception):
    pass


class FakeNotFound(Exception):
    pass


class FakeInvalidCommitPosition(Exception):
    pass


class Rec:
    def __init__(self, ne, stream, pos, commit):
        self.type, self.data, self.metadata, self.id = ne.type, ne.data, ne.metadata, ne.id
        self.stream_name, self.stream_position, self.commit_position = stream, pos, commit


class FakeKdbModule:
    class StreamState:
        NO_STREAM = "NO_STREAM"
        ANY = "ANY"
    exceptions = types.SimpleNamespace(WrongCurrentVersionError=FakeWrongVersion, NotFoundError=FakeNotFound,
                                       InvalidCommitPositionError=FakeInvalidCommitPosition)

    class NewEvent:
        def __init__(self, type, data, metadata=b"", id=None):
            self.type, self.data, self.metadata, self.id = type, data, metadata, id


class FakeClient:
    """Commit positions step by 100 and include a foreign (system-like) event at the start."""

    def __init__(self):
        self.log = []
        self.log.append(Rec(FakeKdbModule.NewEvent("$system", b"{}", b"{}"), "$settings", 0, 0))  # no 'id' metadata
        self.reads_all = 0

    def get_current_version(self, stream):
        n = sum(1 for r in self.log if r.stream_name == stream)
        return FakeKdbModule.StreamState.NO_STREAM if n == 0 else n - 1

    def append_to_stream(self, stream, *, events, current_version):
        cur = self.get_current_version(stream)
        want = current_version
        if want != "ANY" and want != cur:  # NO_STREAM == NO_STREAM, int == int
            raise FakeWrongVersion("wrong")
        base = -1 if cur == "NO_STREAM" else cur
        for i, ne in enumerate(events if isinstance(events, list) else [events]):
            self.log.append(Rec(ne, stream, base + 1 + i, (len(self.log) + 1) * 100))
        return self.log[-1].commit_position

    def get_stream(self, stream, *, stream_position=None, limit=10**9, **_):
        recs = [r for r in self.log if r.stream_name == stream]
        if not recs:
            raise FakeNotFound()
        start = stream_position or 0
        return tuple(r for r in recs if r.stream_position >= start)[:limit]

    def read_all(self, *, commit_position=None, backwards=False, limit=10**9, **_):
        self.reads_all += 1
        # Like a real node (seen live): a commit position must be exactly the start of a log record.
        if commit_position is not None and commit_position not in {r.commit_position for r in self.log}:
            raise FakeInvalidCommitPosition(f"InvalidPosition at {commit_position}")
        recs = [r for r in self.log if not r.stream_name.startswith("$")]
        recs = [r for r in recs if commit_position is None or r.commit_position >= commit_position]
        return list(reversed(recs))[:limit] if backwards else recs[:limit]


def stores():
    yield "memory", MemoryStore()
    yield "kurrent-fake", KurrentStore(FakeClient(), kdb=FakeKdbModule)


class StoreContract(unittest.TestCase):
    def each(self):
        for name, s in stores():
            with self.subTest(store=name):
                yield s

    def test_append_assigns_versions_and_increasing_positions(self):
        for s in self.each():
            r = s.append("content-a", [ev(1), ev(2)])
            self.assertEqual(r["appended"], 2)
            self.assertEqual([n["version"] for n in r["new"]], [0, 1])
            self.assertEqual(r["positions"], sorted(set(r["positions"])))
            r2 = s.append("content-a", [ev(3)])
            self.assertEqual(r2["new"][0]["version"], 2)
            self.assertGreater(r2["positions"][0], r["positions"][-1])

    def test_append_is_idempotent_by_id(self):
        for s in self.each():
            s.append("content-a", [ev(1), ev(2)])
            r = s.append("content-a", [ev(1), ev(2), ev(3)])
            self.assertEqual(r["appended"], 1)
            self.assertEqual(len(s.read_stream("content-a")), 3)
            self.assertEqual(s.append("content-a", [ev(1)])["appended"], 0)

    def test_idempotent_retry_with_stale_expected_version_is_a_noop_not_a_409(self):
        for s in self.each():
            s.append("content-a", [ev(1)], expected_version=-1)
            r = s.append("content-a", [ev(1)], expected_version=-1)  # lost-response retry
            self.assertEqual(r["appended"], 0)

    def test_expected_version(self):
        for s in self.each():
            s.append("content-a", [ev(1)], expected_version=-1)
            with self.assertRaises(ConcurrencyError):
                s.append("content-a", [ev(2)], expected_version=-1)
            with self.assertRaises(ConcurrencyError):
                s.append("content-a", [ev(2)], expected_version=5)
            self.assertEqual(s.append("content-a", [ev(2)], expected_version=0)["appended"], 1)
            self.assertEqual(len(s.read_stream("content-a")), 2)  # failed appends left nothing behind

    def test_read_from_position_is_inclusive_and_skips_foreign_events(self):
        for s in self.each():
            s.append("content-a", [ev(1)])
            s.append("content-b", [ev(2, stream="content-b")])
            s.append("content-a", [ev(3)])
            allv = s.read(0)
            self.assertEqual([e["data"]["n"] for e in allv], [1, 2, 3])
            from_second = s.read(allv[1]["position"])
            self.assertEqual([e["data"]["n"] for e in from_second], [2, 3])
            self.assertEqual(s.read(allv[2]["position"] + 1), [])
            self.assertEqual(len(s.read(0, limit=2)), 2)
            self.assertEqual(s.last_position(), allv[-1]["position"])

    def test_read_from_a_position_that_is_not_a_record_boundary(self):
        """Regression (found live): the browser asks for position+1, which KurrentDB rejects; positions are sparse."""
        for s in self.each():
            s.append("content-a", [ev(1)])
            s.append("content-b", [ev(2, stream="content-b")])
            s.append("content-a", [ev(3)])
            allv = s.read(0)
            self.assertEqual([e["data"]["n"] for e in s.read(allv[0]["position"] + 1)], [2, 3])
            self.assertEqual([e["data"]["n"] for e in s.read(allv[1]["position"] + 1, limit=1)], [3])
            self.assertEqual(s.read(allv[2]["position"] + 1), [])
            s.append("content-a", [ev(4)])  # log grew past the cached boundaries
            self.assertEqual([e["data"]["n"] for e in s.read(allv[2]["position"] + 1)], [4])

    def test_empty_store(self):
        for s in self.each():
            self.assertEqual(s.last_position(), -1)
            self.assertEqual(s.read(0), [])
            self.assertEqual(s.read_stream("nope-1"), [])
            self.assertIsNone(s.get(str(uuid.UUID(int=99))))

    def test_get_by_id_returns_envelope_with_original_id(self):
        for s in self.each():
            sha = "ab" * 32
            s.append("content-a", [ev(1, id=sha)])
            got = s.get(sha)
            self.assertEqual(got["id"], sha)
            self.assertEqual(got["data"], {"n": 1})


class MessageDbConnection(unittest.TestCase):
    def test_connects_with_message_store_search_path(self):
        """Regression (found live): write_message() calls acquire_lock() unqualified; without this it fails."""
        import sys
        from stores import MessageDbStore, SEARCH_PATH_OPTIONS
        seen = {}
        fake = types.SimpleNamespace(connect=lambda dsn, **kw: seen.update(dsn=dsn, **kw) or types.SimpleNamespace(closed=False))
        old = sys.modules.get("psycopg")
        sys.modules["psycopg"] = fake
        try:
            MessageDbStore("postgresql://x")._db()
        finally:
            if old is None:
                del sys.modules["psycopg"]
            else:
                sys.modules["psycopg"] = old
        self.assertIn("search_path=message_store", seen["options"])
        self.assertEqual(seen["options"], SEARCH_PATH_OPTIONS)


class MessageDbConflict(unittest.TestCase):
    def test_wrong_expected_version_becomes_concurrency_error_with_actual_version(self):
        from stores import MessageDbStore

        class Cur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=()):
                if "write_message" in sql:
                    raise RuntimeError("Wrong expected version: -1 (Stream: content-a, Stream Version: 2)\nCONTEXT: x")
            def fetchone(self): return None

        class Conn:
            closed = False
            def cursor(self): return Cur()
            def commit(self): pass
            def rollback(self): self.rolled = True

        s = MessageDbStore("x", connect=Conn)
        with self.assertRaises(ConcurrencyError) as cm:
            s.append("content-a", [ev(1)], expected_version=-1)
        self.assertEqual((cm.exception.expected, cm.exception.actual), (-1, 2))


class KurrentSpecifics(unittest.TestCase):
    def test_event_ids_are_uuids_derived_from_envelope_ids(self):
        fake = FakeClient()
        s = KurrentStore(fake, kdb=FakeKdbModule)
        sha = "cd" * 32
        s.append("content-a", [ev(1, id=sha)])
        stored = [r for r in fake.log if r.stream_name == "content-a"][0]
        self.assertEqual(stored.id, store_uuid(sha))
        self.assertEqual(json.loads(stored.metadata)["id"], sha)

    def test_id_index_survives_a_gateway_restart(self):
        fake = FakeClient()
        KurrentStore(fake, kdb=FakeKdbModule).append("content-a", [ev(1)])
        restarted = KurrentStore(fake, kdb=FakeKdbModule)  # fresh process, empty in-memory index
        self.assertEqual(restarted.append("content-a", [ev(1)])["appended"], 0)


if __name__ == "__main__":
    unittest.main()
