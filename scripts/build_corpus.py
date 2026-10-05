#!/usr/bin/env python3
"""Build data/corpus.json: one document per site <section> and per public-safe booklet page.

Python 3.11 standard library only. See es-search-build CONTRACT section 6.
Usage: python3 scripts/build_corpus.py [--repo DIR] [--booklets DIR] [--out FILE]
"""
import argparse
import html
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOKLET_DIR = Path("/home/claude/ingqiqo-booklets-v2/03_drafts")
# Only these four booklets may ever enter the corpus.
BOOKLETS = {
    "seeing-clearly": "Seeing Clearly",
    "company-profile": "Company Profile",
    "engagement-pack": "Engagement Pack",
    "progress-record": "The Progress Record",
}
# Where each booklet can be read online (resources/read/), so a booklet hit links somewhere.
READER = {
    "seeing-clearly": "/resources/read/seeing-clearly.html",
    "company-profile": "/resources/read/company-profile.html",
    "engagement-pack": "/resources/read/engagement-pack.html",
    "progress-record": "/resources/read/the-progress-record.html",
}
SKIP_DIRS = {".git", "stack", "reference-stack", "node_modules", "assets", "data", "tests", "scripts"}

# Contract section 6: any booklet page matching one of these (case-insensitive) is dropped whole.
LEAK_PATTERNS = [re.compile(r"(?<![A-Za-z])R\s?\d")] + [re.compile(p, re.I) for p in (
    r"\bZAR\b", r"rate card", r"\bprice", r"\bpricing\b", r"\bfees?\b", r"per cent of", r"\brand\b", r"\d\s?(?:m|bn|million|billion)\b",
    r"%\s*of (the )?(fee|price)", r"\[confirm", r"registration number", r"\bVAT\b", r"\bCSD\b",
    r"B-BBEE", r"projection", r"revenue", r"days of principal", r"delivery ceiling",
)]
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")

SKIP_TAGS = {"script", "style", "svg", "nav", "header", "footer"}
VOID = {"img", "br", "hr", "meta", "link", "input", "col", "source", "wbr", "area", "base"}
INLINE = {"span", "a", "em", "strong", "b", "i", "code", "sub", "sup", "small", "abbr", "mark", "u", "s"}
HEADINGS = {"h1", "h2", "h3", "h4"}


def collapse(s):
    return re.sub(r"\s+", " ", s).strip()


class Collector(HTMLParser):
    """Collects <title> and one record per <section>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.sections = []
        self._cur = None
        self._skip = []  # stack of [tag, depth, kind]
        self._in_title = False
        self._in_heading = None

    @staticmethod
    def _cls(attrs):
        return (dict(attrs).get("class") or "").split()

    def _skipping(self):
        return bool(self._skip)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = self._cls(attrs)
        if self._skip:
            if tag == self._skip[-1][0] and tag not in VOID:
                self._skip[-1][1] += 1
            return
        if tag == "title" and self._cur is None:
            self._in_title = True
            return
        kind = None
        if tag in SKIP_TAGS:
            kind = "skip"
        elif tag == "div" and "foot" in cls:
            kind = "foot"
        elif tag == "div" and "head" in cls:
            kind = "skip"
        elif tag == "span" and "num" in cls:
            kind = "skip"
        if kind and tag not in VOID:
            self._skip.append([tag, 1, kind])
            if kind == "foot" and self._cur is not None:
                self._cur["foot_open"] = True
            return
        if tag == "section":
            self._cur = {"attrs": a, "cls": cls, "heading": "", "kicker": "", "text": [],
                         "foot": [], "meta": ""}
            self.sections.append(self._cur)
            return
        if self._cur is None:
            return
        if tag in HEADINGS and not self._cur["heading"] and self._in_heading is None:
            self._in_heading = tag
        if tag == "p" and "kicker" in cls:
            self._in_heading = self._in_heading or "kicker"
        if tag == "div" and "meta" in cls:
            self._in_heading = self._in_heading or "meta"
        if tag not in INLINE:
            self._cur["text"].append(" ")

    def handle_startendtag(self, tag, attrs):
        if not self._skip and self._cur is not None and tag not in INLINE:
            self._cur["text"].append(" ")

    def handle_endtag(self, tag):
        if self._skip:
            top = self._skip[-1]
            if tag == top[0]:
                top[1] -= 1
                if top[1] == 0:
                    self._skip.pop()
            return
        if tag == "title":
            self._in_title = False
            return
        if self._cur is None:
            return
        if self._in_heading and (tag == self._in_heading or (
                self._in_heading in ("kicker", "meta") and tag in ("p", "div"))):
            self._in_heading = None
        if tag == "section":
            self._cur = None
            return
        if tag not in INLINE:
            self._cur["text"].append(" ")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._cur is None:
            return
        if self._skip:
            if self._skip[-1][2] == "foot" and data.strip():
                self._cur["foot"].append(collapse(data))
            return
        self._cur["text"].append(data)
        if self._in_heading == "kicker":
            self._cur["kicker"] += data
        elif self._in_heading == "meta":
            self._cur["meta"] += data
        elif self._in_heading:
            self._cur["heading"] += data


def parse(path):
    p = Collector()
    p.feed(Path(path).read_text(encoding="utf-8"))
    p.close()
    return p


def clean_title(t):
    return collapse(re.split(r"\s+[—–|-]\s+", collapse(t))[0]) if t.strip() else ""


# ---------------------------------------------------------------- site pages

def page_type(slug):
    first = slug.split("/")[0]
    return {"work": "work", "notes": "note", "method": "method", "architecture": "architecture", "engineering": "engineering",
            "glossary": "glossary"}.get(first, "page")


def facts_from_section(sec):
    """Return {dt: dd} for <dl class=facts> style blocks, from raw text pairs. Parsed separately."""
    return {}


FACT_RE = re.compile(r"<div>\s*<dt>(.*?)</dt>\s*<dd>(.*?)</dd>\s*</div>", re.S | re.I)


def work_tags(raw_html):
    tags = []
    for dt, dd in FACT_RE.findall(raw_html):
        dt = collapse(html.unescape(re.sub(r"<[^>]+>", "", dt))).lower()
        dd_clean = re.sub(r'<span class="soon-inline">.*?</span>', "", dd, flags=re.S)
        dd_clean = collapse(html.unescape(re.sub(r"<[^>]+>", " ", dd_clean)))
        if dt == "lead demand" and dd_clean:
            tags.append(dd_clean)
        elif dt in ("spine plane", "plane") and dd_clean:
            tags += [collapse(x) for x in dd_clean.split("·") if collapse(x)]
    seen, out = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def site_docs(repo):
    repo = Path(repo)
    files = []
    for p in sorted(repo.rglob("*.html")):
        rel = p.relative_to(repo)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        if rel.name == "404.html" or rel.parts[0] == "search" or rel.parts[:2] == ("book", "confirm"):
            continue  # not content: the error page, the search page itself, and the organiser's confirm page
        if rel.parts[:2] in (("resources", "read"), ("resources", "dojo")):
            continue  # page images and an app: the booklet text enters as booklet docs, linked to these readers
        files.append((rel, p))
    docs = []
    for rel, p in files:
        if rel.name == "index.html":
            slug = "/".join(rel.parts[:-1]) or "home"
            url = "/" + "/".join(rel.parts[:-1]) + ("/" if len(rel.parts) > 1 else "")
        else:
            slug = rel.with_suffix("").as_posix()
            url = "/" + rel.as_posix()
        typ = page_type(slug)
        raw = p.read_text(encoding="utf-8")
        parsed = parse(p)
        title = clean_title(parsed.title) or slug
        base_tags = work_tags(raw) if typ == "work" else []
        tags = [typ] + [t for t in base_tags if t != typ]
        used = set()
        for i, sec in enumerate(parsed.sections):
            a = sec["attrs"]
            sid = a.get("id") or a.get("aria-labelledby") or ("hero" if "hero" in sec["cls"] else f"s{i + 1}")
            n, base = 2, sid
            while sid in used:
                sid, n = f"{base}-{n}", n + 1
            used.add(sid)
            text = collapse(html.unescape("".join(sec["text"])))
            if not text:
                continue
            heading = collapse(sec["heading"]) or title
            docs.append({
                "id": f"{slug}#{sid}", "url": f"{url}#{sid}" if sid else url, "title": title,
                "section": heading, "type": typ, "source": rel.as_posix(), "text": text,
                "tags": list(tags),
            })
    return docs


# ------------------------------------------------------------------ booklets

def ordinal(word):
    return {"first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th", "fifth": "5th"}.get(word.lower())


def is_leaky(text):
    return any(p.search(text) for p in LEAK_PATTERNS)


def scrub_contacts(text):
    text = EMAIL_RE.sub("", text)
    text = PHONE_RE.sub("", text)
    return collapse(text)


def booklet_docs(booklet_dir, slug, stats):
    path = Path(booklet_dir) / f"{slug}.html"
    parsed = parse(path)
    title = BOOKLETS[slug]
    edition = None
    for sec in parsed.sections:
        if "cover" in sec["cls"]:
            m = re.search(r"(\w+)\s+edition", sec["meta"], re.I)
            edition = ordinal(m.group(1)) if m else None
            break
    docs, kept, dropped = [], 0, []
    for i, sec in enumerate(parsed.sections, 1):
        if "cover" in sec["cls"] or "back" in sec["cls"]:
            continue
        num = next((int(x) for x in reversed(sec["foot"]) if x.isdigit()), i)
        text = collapse(html.unescape("".join(sec["text"])))
        heading = collapse(sec["heading"]) or (sec["foot"][0] if sec["foot"] else title)
        if not text:
            continue
        if is_leaky(text) or is_leaky(heading):
            dropped.append(num)
            continue
        text = scrub_contacts(text)
        kept += 1
        src = f"{title}, {edition + ', ' if edition else ''}p{num}"
        docs.append({"id": f"booklet/{slug}/p{num}", "url": READER.get(slug), "title": title, "section": heading,
                     "type": "booklet", "source": src, "text": text, "tags": ["booklet", slug]})
    stats[slug] = {"kept": kept, "dropped": len(dropped), "dropped_pages": dropped, "carried": False}
    return docs


def load_existing(out):
    try:
        data = json.loads(Path(out).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def build(repo=ROOT, booklet_dir=BOOKLET_DIR, out=None):
    repo = Path(repo)
    out = Path(out) if out else repo / "data" / "corpus.json"
    existing = load_existing(out)
    docs = site_docs(repo)
    stats = {}
    for slug in BOOKLETS:
        if (Path(booklet_dir) / f"{slug}.html").is_file():
            docs += booklet_docs(booklet_dir, slug, stats)
        else:  # CI runner: carry forward unchanged
            old = [d for d in existing if d.get("type") == "booklet" and d.get("id", "").startswith(f"booklet/{slug}/")]
            for d in old:
                d["url"] = READER.get(slug)
            docs += old
            stats[slug] = {"kept": len(old), "dropped": 0, "dropped_pages": [], "carried": True}
    ids = [d["id"] for d in docs]
    assert len(ids) == len(set(ids)), "duplicate doc ids"
    docs.sort(key=lambda d: d["id"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(docs, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return docs, stats


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(ROOT))
    ap.add_argument("--booklets", default=str(BOOKLET_DIR))
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    docs, stats = build(a.repo, a.booklets, a.out)
    by = {}
    for d in docs:
        by[d["type"]] = by.get(d["type"], 0) + 1
    print(f"corpus: {len(docs)} docs {dict(sorted(by.items()))}")
    for slug, s in stats.items():
        print(f"  {slug}: kept {s['kept']}, dropped {s['dropped']}{' (carried forward)' if s['carried'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
