"""Smoke test: a card renders at the right size, long text shrinks to fit, bad input is refused."""
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "social"))
import quote_card  # noqa: E402


def png_size(p):
    with open(p, "rb") as f:
        head = f.read(24)
    return struct.unpack(">II", head[16:24])


class QuoteCard(unittest.TestCase):
    def test_render_sizes_and_url(self):
        with tempfile.TemporaryDirectory() as d:
            urls = quote_card.render([
                {"slug": "t-short", "text": "An open decision is not a free decision.", "pillar": "Decisions"},
                {"slug": "t-long", "text": "x " * 140 + "end.", "pillar": "Fit", "size": "square"},
            ], Path(d))
            self.assertEqual(png_size(Path(d) / "t-short.png"), (1080, 1350))
            self.assertEqual(png_size(Path(d) / "t-long.png"), (1080, 1080))
            self.assertTrue(all(u.startswith("https://nmbambo.github.io/") for u in urls))

    def test_refuses_bad_input(self):
        with tempfile.TemporaryDirectory() as d:
            for bad in ({"slug": "Bad Slug", "text": "x"}, {"slug": "ok-slug", "text": ""},
                        {"slug": "ok-slug", "text": "x" * 400}, {"slug": "ok-slug", "text": "x", "size": "wide"}):
                with self.assertRaises(SystemExit):
                    quote_card.render([bad], Path(d))

    def test_text_is_escaped(self):
        self.assertNotIn("<script>alert", quote_card.card_html("<script>alert(1)</script>", "p"))


if __name__ == "__main__":
    unittest.main()
