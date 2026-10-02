import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import build_corpus  # noqa: E402
import cdc  # noqa: E402

PAGE = """<!doctype html><html><head><title>Home — Nkosinathi Mbambo</title></head><body>
<header class="top"><nav>Menu Chrome</nav></header>
<main><section id="a" aria-labelledby="ah"><h2 id="ah"><span class="num">I</span> Alpha</h2>
<p>Alpha   text
here.</p><script>var x=1;</script><svg><title>svgtitle</title><text>svgtext</text></svg></section>
<section aria-labelledby="b"><h2 id="b">Beta</h2><p>Beta text.</p></section></main>
<footer>Footer Chrome</footer></body></html>"""

WORK = """<html><head><title>Thing — Nkosinathi Mbambo</title></head><body><main>
<section class="hero"><h1>Thing</h1><dl class="facts">
<div><dt>Lead demand</dt><dd>Architect <span class="soon-inline">· posture</span></dd></div>
<div><dt>Spine plane</dt><dd>L5 Judgement · L7 Execution &amp; Feedback</dd></div></dl></section>
</main></body></html>"""


def booklet(pages):
    body = '<section class="page cover"><div class="meta">Second edition · 2026</div><h1>Cover</h1></section>\n'
    for n, (h, t) in enumerate(pages, 2):
        body += (f'<section class="page"><div class="head"><img src="x.svg"></div><div class="body">'
                 f'<p class="kicker">K</p><h2>{h}</h2><p>{t}</p></div>'
                 f'<div class="foot"><span>{h}</span><span>{n:02d}</span></div></section>\n')
    body += '<section class="page back"><p>Back page</p></section>'
    return f"<html><head><title>Seeing Clearly — Ingqiqo</title></head><body>{body}</body></html>"


def write(p, s):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.bk = Path(self.tmp.name) / "booklets"
        write(self.repo / "index.html", PAGE)
        write(self.repo / "work" / "thing" / "index.html", WORK)
        write(self.repo / "reference-stack" / "index.html", PAGE)
        write(self.bk / "seeing-clearly.html", booklet([
            ("Good one", "Clean public text."),
            ("Leaky", "Our rate card says R 500 per day, call +27 81 321 3766."),
            ("Good two", "Mail me at a@b.co, call 011 555 1234 anytime."),
        ]))
        write(self.bk / "business-plan.html", booklet([("Secret", "Plan text.")]))
        self.out = self.repo / "data" / "corpus.json"

    def build(self, bk=None):
        return build_corpus.build(self.repo, bk if bk is not None else self.bk, self.out)

    def cdc(self):
        return cdc.run(self.repo / "data", self.repo)

    def log(self):
        p = self.repo / "data" / "events.ndjson"
        return p.read_text().splitlines() if p.exists() else []


class TestCorpus(Env):
    def test_site_sections_and_chrome(self):
        docs, _ = self.build()
        by = {d["id"]: d for d in docs}
        self.assertIn("home#a", by)
        self.assertIn("home#b", by)
        a = by["home#a"]
        self.assertEqual(a["section"], "Alpha")
        self.assertEqual(a["title"], "Home")
        self.assertEqual(a["text"], "Alpha Alpha text here.")
        self.assertEqual(a["tags"], ["page"])
        self.assertNotIn("Chrome", json.dumps(docs))
        self.assertNotIn("svgtext", json.dumps(docs))
        self.assertFalse(any("reference-stack" in d["id"] for d in docs))

    def test_work_tags(self):
        docs, _ = self.build()
        w = next(d for d in docs if d["id"] == "work/thing#hero")
        self.assertEqual(w["type"], "work")
        self.assertEqual(w["tags"], ["work", "Architect", "L5 Judgement", "L7 Execution & Feedback"])

    def test_booklet_filtering(self):
        docs, stats = self.build()
        b = {d["id"]: d for d in docs if d["type"] == "booklet"}
        self.assertEqual(sorted(b), ["booklet/seeing-clearly/p2", "booklet/seeing-clearly/p4"])
        self.assertEqual(stats["seeing-clearly"]["dropped_pages"], [3])
        self.assertIsNone(b["booklet/seeing-clearly/p2"]["url"])
        self.assertEqual(b["booklet/seeing-clearly/p2"]["source"], "Seeing Clearly, 2nd, p2")
        t = b["booklet/seeing-clearly/p4"]["text"]
        self.assertNotIn("@", t)
        self.assertNotIn("555", t)
        self.assertFalse(any("secret" in json.dumps(d).lower() for d in docs))

    def test_carry_forward_when_booklets_absent(self):
        first, _ = self.build()
        want = [d for d in first if d["type"] == "booklet"]
        self.assertTrue(want)
        docs, stats = self.build(bk=Path(self.tmp.name) / "nope")
        self.assertEqual([d for d in docs if d["type"] == "booklet"], want)
        self.assertTrue(stats["seeing-clearly"]["carried"])
        self.assertEqual(json.loads(self.out.read_text()), docs)


class TestCDC(Env):
    def test_initial_then_idempotent(self):
        self.build()
        n = len(json.loads(self.out.read_text()))
        ev = self.cdc()
        self.assertEqual(len(ev), n)
        self.assertEqual({e["type"] for e in ev}, {"ContentAdded"})
        before = self.log()
        self.assertEqual(self.cdc(), [])
        self.build()
        self.assertEqual(self.cdc(), [])
        self.assertEqual(self.log(), before)

    def test_envelope_and_ids(self):
        self.build()
        self.cdc()
        e = json.loads(self.log()[0])
        self.assertEqual(e["stream"], "content-" + e["data"]["id"])
        self.assertEqual(e["meta"]["source"], "git-cdc")
        self.assertEqual(e["meta"]["schema"], 1)
        want = hashlib.sha256(f"ContentAdded|{e['data']['id']}|{e['data']['hash']}".encode()).hexdigest()
        self.assertEqual(e["id"], want)
        self.assertEqual(len(set(json.loads(x)["id"] for x in self.log())), len(self.log()))

    def test_single_change_and_remove_append_only(self):
        self.build()
        self.cdc()
        before = self.log()
        write(self.repo / "index.html", PAGE.replace("Beta text.", "Beta text changed."))
        self.build()
        ev = self.cdc()
        self.assertEqual([(e["type"], e["data"]["id"]) for e in ev], [("ContentChanged", "home#b")])
        self.assertEqual(self.log()[:len(before)], before)
        self.assertEqual(len(self.log()), len(before) + 1)
        self.assertEqual(self.cdc(), [])
        write(self.repo / "index.html", PAGE.replace('<section aria-labelledby="b"><h2 id="b">Beta</h2><p>Beta text.</p></section>', ""))
        self.build()
        ev = self.cdc()
        self.assertEqual([(e["type"], e["data"]) for e in ev], [("ContentRemoved", {"id": "home#b"})])
        self.assertEqual(self.cdc(), [])
        state = json.loads((self.repo / "data" / "cdc-state.json").read_text())
        self.assertNotIn("home#b", state)

    def test_lost_state_does_not_duplicate_events(self):
        self.build()
        self.cdc()
        n = len(self.log())
        (self.repo / "data" / "cdc-state.json").unlink()
        self.assertEqual(self.cdc(), [])
        self.assertEqual(len(self.log()), n)


if __name__ == "__main__":
    unittest.main()
