"""Import-level smoke tests for the I/O modules (no Docker, Kafka, or Postgres needed)."""
import unittest

import run
from tools.base import Ctx
from tools.airbyte import Airbyte
from tools.base import ToolNotConfigured


class SmokeTests(unittest.TestCase):
    def test_image_tags_resolve_from_compose_file(self):
        ctx = Ctx({}, "t1")
        self.assertRegex(ctx.image_tag("debezium-connect"), r"^quay\.io/debezium/connect:\d")
        self.assertRegex(ctx.image_tag("olake"), r"^olakego/source-postgres:v\d")
        self.assertIsNone(ctx.image_tag("nope"))

    def test_airbyte_requires_manual_setup_and_is_reported_as_skipped(self):
        with self.assertRaises(ToolNotConfigured):
            Airbyte(Ctx({}, "t2"))

    def test_state_keeps_global_sequence_and_keys_unique_across_phases(self):
        st = run.State(rows=100, seed=1)
        a, b = st.make(300), st.make(300)
        self.assertEqual(b[0].seq, a[-1].seq + 1)
        inserted = [m.key for m in a + b if m.op == "insert"]
        self.assertEqual(len(inserted), len(set(inserted)))

    def test_sampler_can_be_started_and_stopped_without_docker(self):
        s = run.Sampler(["x"])
        s.start()
        self.assertEqual(s.finish(), [])


if __name__ == "__main__":
    unittest.main()
