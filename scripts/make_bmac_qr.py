#!/usr/bin/env python3
"""Regenerate assets/bmac-qr.svg, the Buy Me a Coffee QR code on the Resources page.

The site has no build step; this is a developer tool. It needs `pip install segno` (pure Python).
Obsidian modules on pearl (the brand kit's colours): dark on light is what every phone scans.
Error correction M, a four-module quiet zone, and a viewBox so the SVG scales to any size.
After changing the URL, decode the result with an independent reader before publishing.
Usage: python3 scripts/make_bmac_qr.py
"""
import io
from pathlib import Path

import segno

URL = "https://buymeacoffee.com/idealilk"
OUT = Path(__file__).resolve().parent.parent / "assets" / "bmac-qr.svg"

buf = io.BytesIO()
segno.make(URL, error="m", micro=False).save(
    buf, kind="svg", scale=8, border=4, dark="#0C0A10", light="#F1EEE7", xmldecl=False,
    svgclass=None, lineclass=None, title="QR code for buymeacoffee.com/idealilk", desc=URL, nl=False)
svg = buf.getvalue().decode("utf-8")
root = '<svg xmlns="http://www.w3.org/2000/svg" width="296" height="296">'
assert root in svg, "unexpected segno output; update this script"
svg = svg.replace(root, '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 296 296" width="296" '
                        'height="296" role="img" aria-labelledby="t d">', 1)
svg = svg.replace("<title>", '<title id="t">', 1).replace("<desc>", '<desc id="d">', 1)
OUT.write_text(svg + "\n", encoding="utf-8", newline="\n")
print(f"wrote {OUT} ({len(svg)} bytes) for {URL}")
