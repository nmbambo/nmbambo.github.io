#!/usr/bin/env python3
"""Build the Ingqiqo Executables logo and icon kit from the rose mark.

The mark is the Ingqiqo rose (assets/rose.svg, revision 2; assets/rose-small.svg for small sizes).
The wordmark is set in Bodoni Moda and converted to outlines, so every SVG renders the same anywhere
without fonts. Pinyon Script appears only for the motto. Colours: obsidian #0C0A10, pearl #F1EEE7,
cold gold #BFB08A, amber sigil #B8793E. No gradients, shadows or other colours.

Writes assets/brand/ (01_icon, 02_logo, 03_web, README.md) and prints what it made.
Usage: python3 scripts/brand/build_brand_kit.py
Needs fonttools + brotli, Pillow, and Playwright with Chromium.
"""
import io
import re
import shutil
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
FONTS = ROOT / "scripts" / "social" / "fonts"
OUT = ROOT / "assets" / "brand"
OBSIDIAN, PEARL, GOLD, AMBER = "#0C0A10", "#F1EEE7", "#BFB08A", "#B8793E"

# Palettes: ground (None = transparent), ink for strokes and type, the eye's fill, the motto colour.
SCHEMES = {
    "dark": {"ground": OBSIDIAN, "ink": GOLD, "type": PEARL, "eye": OBSIDIAN, "motto": GOLD},
    "light": {"ground": PEARL, "ink": OBSIDIAN, "type": OBSIDIAN, "eye": PEARL, "motto": OBSIDIAN},
    "gold-transparent": {"ground": None, "ink": GOLD, "type": GOLD, "eye": "none", "motto": GOLD},
    "ink-transparent": {"ground": None, "ink": OBSIDIAN, "type": OBSIDIAN, "eye": "none", "motto": OBSIDIAN},
}


# ------------------------------------------------------------------ type to outlines

class Face:
    def __init__(self, name):
        self.font = TTFont(FONTS / name)
        self.upem = self.font["head"].unitsPerEm
        self.cmap = self.font.getBestCmap()
        self.glyphs = self.font.getGlyphSet()
        self.hmtx = self.font["hmtx"]
        os2 = self.font["OS/2"]
        self.cap = getattr(os2, "sCapHeight", 0) or int(self.upem * 0.7)

    def run(self, text, size, tracking=0.0):
        """Return (path d, advance width, cap height) for text at size px, origin at the baseline."""
        s = size / self.upem
        pen = SVGPathPen(self.glyphs)
        x = 0.0
        names = [self.cmap[ord(ch)] for ch in text]
        for i, g in enumerate(names):
            tp = TransformPen(pen, (s, 0, 0, -s, x, 0))
            self.glyphs[g].draw(tp)
            x += self.hmtx[g][0] * s
            if i < len(names) - 1:
                x += tracking * size
        return pen.getCommands(), x, self.cap * s


BODONI_500 = Face("bodoni-moda-latin-500-normal.woff2")
BODONI_400 = Face("bodoni-moda-latin-400-normal.woff2")
PINYON = Face("pinyon-script-latin-400-normal.woff2")


# ------------------------------------------------------------------ the rose

def rose_body(scheme, small=False, weight=1.0):
    src = (ROOT / "assets" / ("rose-small.svg" if small else "rose.svg")).read_text()
    inner = re.sub(r"^<svg[^>]*>|</svg>$", "", src.strip())
    inner = re.sub(r"<title>.*?</title>", "", inner)
    inner = inner.replace(f'fill="{OBSIDIAN}"', f'fill="{scheme["eye"]}"')
    # The site draws the rose in hairlines; a logo or icon needs more weight to hold at a distance.
    inner = re.sub(r'stroke-width="([\d.]+)"', lambda m: f'stroke-width="{float(m.group(1)) * weight:.2f}"', inner)
    return inner.replace(GOLD, scheme["ink"])  # amber sigil stays amber in every scheme


def rose_at(scheme, x, y, size, small=False, weight=2.0):
    k = size / 200
    return f'<g transform="translate({x:.2f},{y:.2f}) scale({k:.5f})">{rose_body(scheme, small, weight)}</g>'


def svg(w, h, body, scheme, title):
    ground = f'<rect width="{w}" height="{h}" fill="{scheme["ground"]}"/>' if scheme["ground"] else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} {h:.0f}" width="{w:.0f}" height="{h:.0f}">'
            f'<title>{title}</title>{ground}{body}</svg>\n')


# ------------------------------------------------------------------ compositions

def icon(scheme, small=False, scale=0.74, weight=2.0):
    s = 1000
    r = s * scale
    return svg(s, s, rose_at(scheme, (s - r) / 2, (s - r) / 2, r, small, weight), scheme, "Ingqiqo Executables")


def wordmark_parts(name_size):
    name_d, name_w, name_cap = BODONI_500.run("INGQIQO", name_size, 0.16)
    # EXECUTABLES is tracked to sit exactly under INGQIQO.
    sub_size = name_size * 0.36
    _, plain_w, sub_cap = BODONI_400.run("EXECUTABLES", sub_size, 0)
    tracking = (name_w - plain_w) / (sub_size * 10)
    sub_d, sub_w, sub_cap = BODONI_400.run("EXECUTABLES", sub_size, tracking)
    return name_d, name_w, name_cap, sub_d, sub_w, sub_cap


def horizontal(scheme):
    name_size = 120
    name_d, name_w, name_cap, sub_d, sub_w, sub_cap = wordmark_parts(name_size)
    gap_lines = name_cap * 0.42
    block_h = name_cap + gap_lines + sub_cap
    mark = block_h * 1.55
    pad = mark * 0.30
    gap = mark * 0.24
    w = pad + mark + gap + name_w + pad
    h = pad + mark + pad
    tx = pad + mark + gap
    top = pad + (mark - block_h) / 2
    t = scheme["type"]
    body = (rose_at(scheme, pad, pad, mark)
            + f'<path transform="translate({tx:.2f},{top + name_cap:.2f})" d="{name_d}" fill="{t}"/>'
            + f'<path transform="translate({tx:.2f},{top + name_cap + gap_lines + sub_cap:.2f})" d="{sub_d}" fill="{t}"/>')
    return svg(w, h, body, scheme, "Ingqiqo Executables")


def stacked(scheme, motto=False):
    name_size = 120
    name_d, name_w, name_cap, sub_d, sub_w, sub_cap = wordmark_parts(name_size)
    mark = name_w * 0.62
    pad = name_w * 0.20
    w = pad + name_w + pad
    y = pad
    body = rose_at(scheme, (w - mark) / 2, y, mark)
    y += mark + name_cap * 0.9 + name_cap
    t = scheme["type"]
    body += f'<path transform="translate({(w - name_w) / 2:.2f},{y:.2f})" d="{name_d}" fill="{t}"/>'
    y += name_cap * 0.42 + sub_cap
    body += f'<path transform="translate({(w - sub_w) / 2:.2f},{y:.2f})" d="{sub_d}" fill="{t}"/>'
    if motto:
        m_d, m_w, m_cap = PINYON.run("Meaning, made coherent.", name_size * 0.52, 0)
        y += name_cap * 1.15 + m_cap
        body += f'<path transform="translate({(w - m_w) / 2:.2f},{y:.2f})" d="{m_d}" fill="{scheme["motto"]}"/>'
        y += m_cap * 0.5
    h = y + pad
    return svg(w, h, body, scheme, "Ingqiqo Executables")


# ------------------------------------------------------------------ rendering

def render(pages):
    """pages: list of (svg_text, out_png, width_px). Height follows the SVG's aspect ratio."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        for text, out, width in pages:
            vb = [float(v) for v in re.search(r'viewBox="([^"]+)"', text).group(1).split()]
            height = round(width * vb[3] / vb[2])
            sized = re.sub(r'width="[^"]+" height="[^"]+"', f'width="{width}" height="{height}"', text, count=1)
            p = b.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
            p.set_content(f'<html><body style="margin:0;background:transparent">{sized}</body></html>')
            p.screenshot(path=str(out), omit_background=True, clip={"x": 0, "y": 0, "width": width, "height": height})
            p.close()
        b.close()


README = """# Ingqiqo Executables: logo and icon kit

The mark is the Ingqiqo rose. The wordmark is Bodoni Moda, converted to outlines, so the SVGs need no fonts.
Rebuild everything with `python3 scripts/brand/build_brand_kit.py`.

## 01_icon: the rose alone
- `icon-dark-*.png`, `icon-dark.svg`: gold rose on obsidian. The default avatar and profile picture
  (Buy Me a Coffee, LinkedIn page, Google Business Profile, Facebook, Instagram, X, YouTube).
  Upload the 1024 or 512 PNG; platforms crop to a circle or round the corners themselves.
- `icon-light-*.png`, `icon-light.svg`: obsidian rose on pearl, for light settings.
- `icon-*-small.svg`: the simplified rose with heavier lines, for anything under 64 px.
- `mark-gold`, `mark-ink`: the rose on a transparent ground, for placing on your own backgrounds.

## 02_logo: the rose with the name
- `logo-horizontal-*`: the everyday logo. Letterheads, email signatures, the top of documents, slides.
- `logo-stacked-*`: for square spaces and covers.
- `logo-stacked-motto-*`: with "Meaning, made coherent.", for title pages and formal uses only.
- `-dark` obsidian ground · `-light` pearl ground · `-gold-transparent` for dark photographs or
  grounds you control · `-ink-transparent` for white paper and print.

## 03_web
- `favicon.ico` (16, 32, 48), `apple-touch-icon.png` (180), `icon-192.png`, `icon-512.png`.

## Rules
- Colours: obsidian #0C0A10, pearl #F1EEE7, cold gold #BFB08A, amber sigil #B8793E. Nothing else.
- Keep clear space around the logo of at least half the rose's width. Never stretch, recolour,
  outline, shadow or put it on a busy photograph.
- Smallest sizes: horizontal logo 160 px wide on screen (35 mm in print); stacked 96 px; icon 16 px
  (use the small rose under 64 px).
- The amber sigil at the centre of the rose stays amber in every version.
"""


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    d_icon, d_logo, d_web = OUT / "01_icon", OUT / "02_logo", OUT / "03_web"
    for d in (d_icon, d_logo, d_web):
        d.mkdir(parents=True)
    jobs = []

    # Icons: the rose alone. Squares for avatars and app icons; platforms round the corners themselves.
    for name in ("dark", "light"):
        sch = SCHEMES[name]
        big, small = icon(sch), icon(sch, small=True, scale=0.92, weight=2.2)
        (d_icon / f"icon-{name}.svg").write_text(big)
        (d_icon / f"icon-{name}-small.svg").write_text(small)
        for px in (1024, 512, 400):
            jobs.append((big, d_icon / f"icon-{name}-{px}.png", px))
    for name in ("gold-transparent", "ink-transparent"):
        t = icon(SCHEMES[name], scale=0.96)
        (d_icon / f"mark-{name.replace('-transparent', '')}.svg").write_text(t)
        jobs.append((t, d_icon / f"mark-{name.replace('-transparent', '')}-1024.png", 1024))

    # Logos: horizontal and stacked lockups, plus the stacked lockup with the motto.
    for name, sch in SCHEMES.items():
        for kind, text in (("horizontal", horizontal(sch)), ("stacked", stacked(sch)),
                           ("stacked-motto", stacked(sch, motto=True))):
            f = d_logo / f"logo-{kind}-{name}.svg"
            f.write_text(text)
            jobs.append((text, d_logo / f"logo-{kind}-{name}.png", 2400 if kind == "horizontal" else 1600))

    # Web: favicon, touch and PWA icons from the dark icon (small-size rose below 64 px).
    dark, dark_small = icon(SCHEMES["dark"]), icon(SCHEMES["dark"], small=True, scale=0.92, weight=2.2)
    tiny = icon(SCHEMES["dark"], small=True, scale=0.98, weight=3.4)  # 16 px needs the heaviest line
    for px, text in ((512, dark), (192, dark), (180, dark), (48, dark_small), (32, dark_small), (16, tiny)):
        name = "apple-touch-icon.png" if px == 180 else f"icon-{px}.png"
        jobs.append((text, d_web / name, px))

    (OUT / "README.md").write_text(README)
    render(jobs)
    ico = [Image.open(d_web / f"icon-{px}.png") for px in (16, 32, 48)]
    ico[2].save(d_web / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)], append_images=ico[:2])
    for f in sorted(OUT.rglob("*")):
        if f.is_file():
            print(f.relative_to(ROOT))


if __name__ == "__main__":
    main()
