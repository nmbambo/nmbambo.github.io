import hashlib
import unittest
import uuid

from envelope import (ValidationError, category, from_stored, store_uuid, to_stored, valid_id,
                      validate_append, validate_event)


def ev(i="0b9f3a52-86a4-4f0a-9a43-7c0e4d4f0001", **over):
    e = {"id": i, "type": "ContentAdded", "stream": "content-a", "data": {"x": 1},
         "meta": {"ts": "2026-10-02T10:00:00Z", "schema": 1, "source": "git-cdc"}}
    e.update(over)
    return e


class EnvelopeTests(unittest.TestCase):
    def test_ids_accept_uuid_and_sha256_only(self):
        self.assertTrue(valid_id(str(uuid.uuid4())))
        self.assertTrue(valid_id(hashlib.sha256(b"x").hexdigest()))
        for bad in ("", "abc", "G" * 64, hashlib.sha256(b"x").hexdigest().upper(), None, 5):
            self.assertFalse(valid_id(bad), bad)

    def test_store_uuid_is_deterministic_and_passes_uuids_through(self):
        h = hashlib.sha256(b"ContentAdded|doc|h").hexdigest()
        self.assertEqual(store_uuid(h), store_uuid(h))
        self.assertNotEqual(store_uuid(h), store_uuid(hashlib.sha256(b"other").hexdigest()))
        u = str(uuid.uuid4())
        self.assertEqual(str(store_uuid(u)), u)

    def test_category(self):
        self.assertEqual(category("content-booklet/seeing-clearly/p13"), "content")
        self.assertEqual(category("agent"), "agent")

    def test_validate_event_strips_position_and_version(self):
        clean = validate_event(ev(position=9, version=3), "content-a")
        self.assertNotIn("position", clean)
        self.assertNotIn("version", clean)

    def test_validate_event_rejections(self):
        for over in ({"id": "nope"}, {"type": ""}, {"data": []}, {"meta": None},
                     {"meta": {"ts": "t", "schema": 2, "source": "s"}},
                     {"meta": {"ts": "", "schema": 1, "source": "s"}},
                     {"stream": "other"}):
            with self.assertRaises(ValidationError, msg=str(over)):
                validate_event(ev(**over), "content-a")

    def test_validate_append(self):
        events, exp = validate_append("content-a", {"events": [ev()], "expectedVersion": -1})
        self.assertEqual((len(events), exp), (1, -1))
        for stream, body in (("$all", {"events": [ev()]}), ("content-a", {"events": []}),
                             ("content-a", {"events": [ev()], "expectedVersion": -5}),
                             ("content-a", {"events": [ev()], "expectedVersion": True}),
                             ("content-a", [ev()]), ("", {"events": [ev()]})):
            with self.assertRaises(ValidationError, msg=str((stream, body))):
                validate_append(stream, body)

    def test_duplicate_ids_in_one_request_collapse(self):
        events, _ = validate_append("content-a", {"events": [ev(), ev()]})
        self.assertEqual(len(events), 1)

    def test_stored_roundtrip_keeps_original_id_and_foreign_events_are_skipped(self):
        h = hashlib.sha256(b"k").hexdigest()
        data, md = to_stored(ev(i=h))
        env = from_stored(type_="ContentAdded", stream="content-a", data=data, metadata=md, position=4, version=0)
        self.assertEqual(env["id"], h)
        self.assertEqual((env["position"], env["version"]), (4, 0))
        self.assertIsNone(from_stored(type_="x", stream="s", data={}, metadata={}, position=0, version=0))


if __name__ == "__main__":
    unittest.main()
