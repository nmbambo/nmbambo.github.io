#!/usr/bin/env python3
"""Build the online readers and public PDF editions for the Resources library.

Each booklet PDF (image-only, as printed) is rendered page by page to WebP under
resources/read/img/<slug>/, and a reader page resources/read/<slug>.html shows the pages in order.
Pages listed in OMIT never leave this script: they carry prices, capacity figures, tender
particulars, unfinished "to complete" fields or rand amounts, none of which go on a public surface.
Where a free PDF is offered, the public edition is the same page set (qpdf).

A booklet with "preview": N is sold as a PDF edition: only its first N pages (the outline and the
opening part) are rendered, and nothing after page N leaves this script.

Usage:
  python3 scripts/build_readers.py --seeing-clearly A.pdf --company-profile B.pdf \
      --engagement-pack C.pdf --progress-record D.pdf --price-of-better E.pdf
Pass any subset; booklets without a PDF argument are left as they are.
Needs poppler (pdftoppm), qpdf and Pillow with WebP.
"""
import argparse
import html
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "resources"

BOOKLETS = {
    "seeing-clearly": {
        "title": "Seeing Clearly", "label": "A guide to decision enablement",
        "lede": "The instruments, what they measure and what they will not, in plain language. Where most readers should start.",
        # Printed as landscape spreads turned onto portrait sheets: turn them upright and trim the bands.
        "rotate": -90,
        # Illustrative rand figures (pages 6, 23, 26) stay off the public site.
        "omit": {6, 23, 26},
        "pdf": None,
    },
    "company-profile": {
        "title": "Company Profile", "label": "Capability and compliance",
        "lede": "Who we are, the method, the platform beneath the work, and the documents a procurement committee asks for.",
        "rotate": 0,
        # 11, 15, 16, 24 prices · 20 delivery ceiling · 26 unfinished fields · 27 tender particulars
        "omit": {11, 15, 16, 20, 24, 26, 27},
        "pdf": "company-profile.pdf",
    },
    "engagement-pack": {
        "title": "Engagement Pack", "label": "Issued at the start of every engagement",
        "lede": "How we work, what we commit to and refuse, who decides what, and the answers you would otherwise ask for one at a time.",
        "rotate": 0, "omit": set(), "pdf": "engagement-pack.pdf",
    },
    "the-progress-record": {
        "title": "The Progress Record", "label": "Judging an engagement fairly",
        "lede": "How progress is tracked and impairments logged, so a sponsor can judge time, effort and results without guesswork.",
        "rotate": 0, "omit": set(), "pdf": None,
    },
    "the-price-of-better": {
        "title": "The Price of Better", "label": "Decision failures, seen from every seat",
        "lede": "Why organisations choose what looks better and end up worse off: the Braess paradox, the Beer Game and the system archetypes, the same decision seen from ten seats, what each failure costs, and how the method closes the way in.",
        "rotate": 0, "omit": set(), "pdf": None,
        # Sold as a PDF edition. The free reader is the cover, how to read it, the contents and Part One.
        "preview": 10,
        "bmac": "https://buymeacoffee.com/idealilk/extras",
        "outline": [
            ("The paradox of better", "The road that made traffic worse, the game that is simple until it is played, and the parity premium."),
            ("A register of failure", "Twelve patterns, eight system archetypes and Senge's eleven laws."),
            ("Every seat at the table", "One decision, seen by the ten people it touches, then read as a system."),
            ("The ledger of cost", "Eight kinds of cost, who carries each, and one worked year."),
            ("Eight cases", "Composite stories for the seminar room, with questions and katas."),
            ("Where these decisions enter", "The planes of the Spine and the steps of IDDC, loops and delays included."),
            ("How Ingqiqo closes it", "Eight mechanisms, and which failure or archetype each one stops."),
            ("Once and for all", "What that promise can honestly mean, and what it cannot."),
            ("Where to start", "Twelve questions to ask before you adopt anything, and a glossary."),
        ],
    },
}

DPI = 150
QUALITY = 80


def trim_bands(im):
    """Drop the flat bands either side of a rotated spread (the column colour at the left edge)."""
    px = im.load()
    w, h = im.size
    edge = px[0, h // 2]

    def differs(x):
        for y in range(0, h, 7):
            p = px[x, y]
            if sum(abs(p[i] - edge[i]) for i in range(3)) > 18:
                return True
        return False

    left = next((x for x in range(w) if differs(x)), 0)
    right = next((x for x in range(w - 1, -1, -1) if differs(x)), w - 1)
    return im.crop((left, 0, right + 1, h))


def render(slug, spec, pdf):
    img_dir = OUT / "read" / "img" / slug
    if img_dir.exists():
        shutil.rmtree(img_dir)
    img_dir.mkdir(parents=True)
    pages = []
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["pdftoppm", "-r", str(DPI), "-png", str(pdf), f"{tmp}/p"], check=True)
        for png in sorted(Path(tmp).glob("p-*.png")):
            n = int(png.stem.split("-")[-1])
            if n in spec["omit"] or n > spec.get("preview", n):
                continue
            im = Image.open(png).convert("RGB")
            if spec["rotate"]:
                im = trim_bands(im.rotate(spec["rotate"], expand=True))
            name = f"{n:02d}.webp"
            im.save(img_dir / name, "WEBP", quality=QUALITY, method=6)
            pages.append((n, name, im.size))
    return pages


def public_pdf(spec, pdf, total):
    keep = [str(n) for n in range(1, total + 1) if n not in spec["omit"]]
    dest = OUT / "pdf" / spec["pdf"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    # --empty starts from a blank document, so the source's info dictionary and metadata stay behind.
    subprocess.run(["qpdf", "--empty", "--pages", str(pdf), ",".join(keep), "--", str(dest)], check=True)


def page_count(pdf):
    out = subprocess.run(["qpdf", "--show-npages", str(pdf)], capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


def reader_html(slug, spec, pages, total):
    t = html.escape(spec["title"])
    omitted = len(spec["omit"])
    note = ("The full booklet, page by page." if not omitted else
            f"The public edition: {omitted} of {total} pages are held back because they carry prices, capacity "
            "figures, tender particulars or worked rand amounts. Ask for the full edition in a conversation.")
    if spec.get("preview"):
        note = (f"The opening: the outline and Part One, {len(pages)} of {total} pages. The whole book is in the PDF "
                "edition, for whatever you think it is worth.")
    pdf_link = (f'<a class="act-quiet" href="../pdf/{spec["pdf"]}">Free PDF of this edition →</a>' if spec["pdf"] else "")
    if spec.get("bmac"):
        # Shown as "coming soon" until the Buy Me a Coffee extra is published; then make it a link.
        pdf_link = (f'<span class="act-quiet" style="color:var(--gold-quiet)" data-bmac="{spec["bmac"]}">'
                    'PDF edition coming soon</span>')
    outline = ""
    if spec.get("outline"):
        romans = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]
        rows = "\n".join(
            f'      <li{" class=\"open\"" if i == 0 else ""}><span class="num">{romans[i]}</span><div><h3>{html.escape(t_)}</h3>'
            f'<p>{html.escape(d)}</p><p class="state">{"Read below" if i == 0 else "In the PDF edition"}</p></div></li>'
            for i, (t_, d) in enumerate(spec["outline"]))
        outline = f"""
  <section class="outline" aria-labelledby="outline">
    <h2 id="outline">The outline</h2>
    <ol>
{rows}
    </ol>
  </section>"""
    figs = "\n".join(
        f'    <figure class="leaf" id="p{n}"><img src="img/{slug}/{name}" width="{w}" height="{h}" '
        f'alt="{t}, page {n}" loading="{"eager" if i < 2 else "lazy"}" decoding="async">'
        f'<figcaption>{n:02d}</figcaption></figure>'
        for i, (n, name, (w, h)) in enumerate(pages))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t} — Resources — Nkosinathi Mbambo</title>
<meta name="description" content="{html.escape(spec['lede'])}">
<meta name="theme-color" content="#0C0A10">
<meta property="og:image" content="https://nmbambo.github.io/resources/read/img/{slug}/{pages[0][1]}">
<link rel="icon" href="../../assets/rose-small.svg" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bodoni+Moda:ital,opsz,wght@0,6..96,400;0,6..96,500;1,6..96,400&family=Pinyon+Script&display=swap" rel="stylesheet">
<link rel="stylesheet" href="../../assets/site.css">
<style>
  .reader {{ max-width: 980px; margin: 0 auto; }}
  .leaves {{ display: grid; gap: 28px; padding: 0 0 clamp(48px, 8vw, 96px); }}
  .leaf {{ margin: 0; }}
  .leaf img {{ display: block; width: 100%; height: auto; border: var(--hairline) solid var(--gold-quiet); background: var(--obsidian-deep); }}
  .leaf figcaption {{ margin-top: 8px; font-size: 11px; letter-spacing: .18em; color: var(--gold-quiet); text-align: right; }}
  .reader .acts {{ display: flex; flex-wrap: wrap; gap: 10px 22px; align-items: center; margin: 28px 0 0; }}
  .act-quiet {{ font-size: 11px; letter-spacing: .2em; text-transform: uppercase; color: var(--gold); }}
  .reader .note {{ margin: 18px 0 0; max-width: 62ch; color: var(--pearl-muted); font-size: 15px; font-style: italic; }}
  .outline {{ padding: 0 0 clamp(40px, 6vw, 64px); }}
  .outline h2 {{ font-size: clamp(22px, 3vw, 28px); margin: 0 0 18px; }}
  .outline ol {{ list-style: none; margin: 0; padding: 0; border-top: var(--hairline) solid var(--gold-quiet); }}
  .outline li {{ display: grid; grid-template-columns: 48px 1fr; gap: 0 12px; padding: 14px 0; border-bottom: var(--hairline) dashed var(--gold-quiet); }}
  .outline li .num {{ font-size: 12px; letter-spacing: .18em; color: var(--pearl-muted); padding-top: 4px; }}
  .outline li h3 {{ margin: 0 0 2px; font-size: 18px; font-weight: 400; }}
  .outline li p {{ margin: 0; color: var(--pearl-muted); font-size: 15px; }}
  .outline li .state {{ margin-top: 4px; font-size: 11px; letter-spacing: .18em; text-transform: uppercase; color: var(--gold-quiet); }}
  .outline li.open .state {{ color: var(--gold); }}
</style>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="top">
  <a class="mark" href="../../" aria-label="Home"><img src="../../assets/rose-small.svg" alt="" width="28" height="28"></a>
  <nav aria-label="Sections">
    <a href="../../method/">Method</a>
    <a href="../../architecture/">Architecture</a>
    <a href="../../#work">Work</a>
    <a href="../../engage/">Engage</a>
    <a href="../" aria-current="page">Resources</a>
    <a href="../../search/">Search</a>
    <a href="../../book/">Book</a>
    <a href="../../#contact">Contact</a>
  </nav>
</header>
<main id="main" class="project reader">
  <section class="hero project-hero">
    <p class="label"><a href="../">Resources</a> · {html.escape(spec['label'])}</p>
    <h1>{t}</h1>
    <p class="role">{html.escape(spec['lede'])}</p>
    <p class="note">{note} Examples are composite or fictitious, and every figure is illustrative.</p>
    <p class="acts"><a class="act-quiet" href="../">← The library</a>{pdf_link}</p>
  </section>{outline}
  <div class="leaves">
{figs}
  </div>
</main>
<footer>
  <img src="../../assets/rose.svg" alt="Ingqiqo rose" width="40" height="40">
  <p>© 2026 Nkosinathi Mbambo · Ingqiqo Executables (Pty) Ltd</p>
</footer>
<script type="module" src="/assets/source.mjs"></script>
</body>
</html>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    for slug in BOOKLETS:
        ap.add_argument(f"--{slug.replace('the-', '')}", dest=slug.replace("-", "_"))
    a = ap.parse_args(argv)
    for slug, spec in BOOKLETS.items():
        arg = getattr(a, slug.replace("-", "_"))
        if not arg:
            continue
        pdf = Path(arg)
        total = page_count(pdf)
        pages = render(slug, spec, pdf)
        (OUT / "read" / f"{slug}.html").write_text(reader_html(slug, spec, pages, total))
        if spec["pdf"]:
            public_pdf(spec, pdf, total)
        print(f"{slug}: {len(pages)} of {total} pages")


if __name__ == "__main__":
    main()
