#!/usr/bin/env python3
"""Render an Ingqiqo quote card (PNG) for Instagram and other image-first channels.

The background is the brand itself, not a photograph: an obsidian ground, a cold-gold hairline frame, and the
construction lines of the rose mark (its rings, its eight-fold rays and its chevron band) drawn large in gold-quiet
hairline, cropped by the frame. The rose itself appears once, small, with clear ground around it. Type is Bodoni
Moda in pearl; the motto is the one script line. No other colours, no fills, no shadows, no gradients.

Usage:
  python3 scripts/social/quote_card.py --slug 2026-10-06-cost --pillar "The cost no one counts" \
      --text "An open decision is not a free decision." [--size portrait|square] [--out assets/social]
  python3 scripts/social/quote_card.py --batch cards.json      # [{"slug","pillar","text","size"?}, ...]

Writes <out>/<slug>.png (portrait 1080x1350, square 1080x1080) and prints its public URL on nmbambo.github.io.
Needs Playwright with Chromium (preinstalled in the workspace). Fonts are vendored under scripts/social/fonts (SIL OFL).
"""
import argparse
import base64
import html
import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SITE = "https://nmbambo.github.io"
SIZES = {"portrait": (1080, 1350), "square": (1080, 1080)}
MAX_TEXT = 320
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,80}$")
C = {"obsidian": "#0C0A10", "pearl": "#F1EEE7", "muted": "#B4AEA3", "gold": "#BFB08A", "quiet": "#7E745C"}


def _font(name):
    return base64.b64encode((HERE / "fonts" / name).read_bytes()).decode()


def _rose_svg():
    svg = (ROOT / "assets" / "rose.svg").read_text()
    return re.sub(r"<title>.*?</title>", "", svg)


def _chevron_path():
    paths = re.findall(r'<path d="([^"]+)"', (ROOT / "assets" / "rose.svg").read_text())
    return paths[2]  # layer 4: the continuous zigzag band


def _construction(w, h):
    """The rose's construction drawing, centred low and right, cropped by the frame."""
    cx, cy = w * 0.86, h * 0.90
    rays = []
    for i in range(16):
        a = math.pi * i / 8
        rays.append(f'<line x1="{cx + 250 * math.cos(a):.1f}" y1="{cy + 250 * math.sin(a):.1f}" '
                    f'x2="{cx + 1100 * math.cos(a):.1f}" y2="{cy + 1100 * math.sin(a):.1f}"/>')
    rings = "".join(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}"/>' for r in (250, 420, 590, 760))
    band = (f'<path d="{_chevron_path()}" transform="translate({cx:.1f},{cy:.1f}) scale(6.6) translate(-100,-100)" '
            'vector-effect="non-scaling-stroke"/>')
    inset = 44
    return (f'<svg class="bg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" aria-hidden="true">'
            # The drawing lives only in the field below the byline (top edge set by the fit step) and, in the
            # signature band, only to the right of the motto, so no line ever crosses type.
            f'<defs><clipPath id="f"><rect id="field" x="{inset}" y="{h // 2}" width="{w - 2 * inset}" height="{h - 260 - h // 2}"/>'
            f'<rect x="680" y="{h - 260}" width="{w - inset - 680}" height="{260 - inset}"/></clipPath></defs>'
            f'<g id="draw" clip-path="url(#f)" fill="none" stroke="{C["quiet"]}" stroke-width="1">{rings}{"".join(rays)}{band}</g>'
            f'<rect x="{inset}" y="{inset}" width="{w - 2 * inset}" height="{h - 2 * inset}" fill="none" '
            f'stroke="{C["gold"]}" stroke-width="1.5"/></svg>')


def card_html(text, pillar, size="portrait"):
    w, h = SIZES[size]
    t = html.escape(text.strip())
    p = html.escape(pillar.strip().upper())
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
@font-face {{ font-family: Bodoni; font-weight: 400; src: url(data:font/woff2;base64,{_font('bodoni-moda-latin-400-normal.woff2')}) format('woff2'); }}
@font-face {{ font-family: Bodoni; font-weight: 500; src: url(data:font/woff2;base64,{_font('bodoni-moda-latin-500-normal.woff2')}) format('woff2'); }}
@font-face {{ font-family: Pinyon; src: url(data:font/woff2;base64,{_font('pinyon-script-latin-400-normal.woff2')}) format('woff2'); }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: {w}px; height: {h}px; background: {C['obsidian']}; overflow: hidden; }}
.bg {{ position: absolute; inset: 0; }}
.pillar {{ position: absolute; left: 120px; top: 132px; font: 500 21px/1 Bodoni, serif; letter-spacing: .3em; color: {C['gold']}; }}
.quote {{ position: absolute; left: 120px; top: 206px; width: 820px; font: 400 72px/1.16 Bodoni, serif; color: {C['pearl']};
          letter-spacing: -.005em; text-wrap: pretty; }}
.rule {{ position: absolute; left: 120px; width: 120px; height: 0; border-top: 1.5px solid {C['gold']}; }}
.by {{ position: absolute; left: 120px; font: 400 19px/1 Bodoni, serif; letter-spacing: .26em; color: {C['muted']}; }}
.mark {{ position: absolute; left: 120px; bottom: 120px; width: 92px; height: 92px; }}
.mark svg {{ width: 92px; height: 92px; }}
.motto {{ position: absolute; left: 236px; bottom: 146px; font: 400 40px/1 Pinyon, cursive; color: {C['gold']}; }}
</style></head><body>
{_construction(w, h)}
<div class="pillar">{p}</div>
<div class="quote" id="q">{t}</div>
<div class="rule" id="r"></div>
<div class="by" id="b">NKOSINATHI MBAMBO &nbsp;·&nbsp; INGQIQO EXECUTABLES</div>
<div class="mark">{_rose_svg()}</div>
<div class="motto">Meaning, made coherent.</div>
<script>
  // Fit, run after the fonts load: shrink the quote until it ends above the signature band, place the rule and
  // byline under it, and start the construction drawing a clear gap below the byline.
  window.fit = () => {{
    const q = document.getElementById('q'), limit = {h} - 420;
    let size = 72; q.style.fontSize = size + 'px';
    // Prefer leaving the lower field for the drawing; give it up only for long quotes.
    while (q.offsetTop + q.offsetHeight > {h} - 640 && size > 54) {{ size -= 2; q.style.fontSize = size + 'px'; }}
    while (q.offsetTop + q.offsetHeight > limit && size > 34) {{ size -= 2; q.style.fontSize = size + 'px'; }}
    const end = q.offsetTop + q.offsetHeight;
    document.getElementById('r').style.top = (end + 44) + 'px';
    document.getElementById('b').style.top = (end + 74) + 'px';
    const field = document.getElementById('field'), y0 = end + 140;
    field.setAttribute('y', y0); field.setAttribute('height', Math.max(0, {h} - 260 - y0));
    // Too little room for the drawing to read as a drawing: leave the ground plain inside the frame.
    if ({h} - 260 - y0 < 300) document.getElementById('draw').style.display = 'none';
    return (end <= limit) ? 'ok' : 'overflow';
  }};
</script></body></html>"""


def render(cards, out_dir):
    from playwright.sync_api import sync_playwright
    out_dir.mkdir(parents=True, exist_ok=True)
    urls = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for c in cards:
            slug, text, pillar = c["slug"], c["text"], c.get("pillar", "")
            size = c.get("size", "portrait")
            if not SLUG.match(slug):
                raise SystemExit(f"bad slug {slug!r}: lowercase letters, digits and hyphens")
            if not text.strip() or len(text) > MAX_TEXT:
                raise SystemExit(f"{slug}: text must be 1-{MAX_TEXT} characters")
            if size not in SIZES:
                raise SystemExit(f"{slug}: size must be one of {sorted(SIZES)}")
            w, h = SIZES[size]
            page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)
            page.set_content(card_html(text, pillar, size))
            page.evaluate("document.fonts.ready")
            if page.evaluate("window.fit()") != "ok":
                raise SystemExit(f"{slug}: text too long for the card; shorten it")
            path = out_dir / f"{slug}.png"
            page.screenshot(path=str(path), clip={"x": 0, "y": 0, "width": w, "height": h})
            page.close()
            rel = path.resolve().relative_to(ROOT).as_posix() if path.resolve().is_relative_to(ROOT) else path.name
            urls.append(f"{SITE}/{rel}")
        browser.close()
    return urls


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--slug")
    ap.add_argument("--text")
    ap.add_argument("--pillar", default="")
    ap.add_argument("--size", default="portrait", choices=sorted(SIZES))
    ap.add_argument("--batch", help="JSON file: a list of {slug, text, pillar, size}")
    ap.add_argument("--out", default=str(ROOT / "assets" / "social"))
    a = ap.parse_args(argv)
    if a.batch:
        cards = json.loads(Path(a.batch).read_text())
    elif a.slug and a.text:
        cards = [{"slug": a.slug, "text": a.text, "pillar": a.pillar, "size": a.size}]
    else:
        ap.error("give --slug and --text, or --batch")
    for u in render(cards, Path(a.out)):
        print(u)


if __name__ == "__main__":
    sys.exit(main())
