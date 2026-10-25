#!/usr/bin/env python3
"""Install the view-only Decision Dojo into resources/dojo/.

Build the app first, in the decision-dojo repository:
    cd decision-dojo && npm run build:viewonly      # writes dist-viewonly/
then:
    python3 scripts/build_dojo.py <path-to>/decision-dojo/dist-viewonly

The app's view-only build records nothing and stores nothing in the browser; when a
visitor tries to log, claim or file something it raises a `dojo:viewonly` event.
This script wraps that build with the site's head, the back link, the view-only
note (which flashes on that event) and the register-interest form.
"""
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "resources" / "dojo"

HEAD = """<title>The Decision Dojo — Resources — Nkosinathi Mbambo</title>
<meta name="description" content="A courseware engine for the craft of deciding: a training hall of katas, ten gates, challenges, a quest board and an XP ledger. Browse it freely.">
<meta name="theme-color" content="#0C0A10">
<link rel="icon" href="../../assets/rose-small.svg" type="image/svg+xml">"""

FLASH = """<script>window.addEventListener('dojo:viewonly',()=>{const n=document.querySelector('.dojo-viewonly');if(n){n.classList.remove('flash');void n.offsetWidth;n.classList.add('flash');}});</script>"""

HOST = """<a class="dojo-back" href="../">← Resources</a>
<p class="dojo-viewonly" role="note">View-only edition. Logging XP, claiming challenges and filing insights open to subscribers. <a href="#dojo-interest">Register interest</a> · <a href="../">Back to the library</a></p>
<style>.dojo-viewonly{position:fixed;left:50%;bottom:14px;transform:translateX(-50%);z-index:50;margin:0;max-width:calc(100% - 28px);box-sizing:border-box;font:13px/1.45 "Bodoni Moda",Georgia,serif;color:#B4AEA3;background:rgba(12,10,16,.94);border:1px solid #7E745C;padding:10px 14px;text-align:center}.dojo-viewonly a{color:#BFB08A}.dojo-viewonly.flash{animation:dvo 1.2s ease}@keyframes dvo{0%,40%{border-color:#BFB08A;color:#F1EEE7}100%{border-color:#7E745C;color:#B4AEA3}}@media (prefers-reduced-motion:reduce){.dojo-viewonly.flash{animation:none;border-color:#BFB08A}}body{padding-bottom:72px}</style>
<style>.dojo-back{position:absolute;right:16px;top:12px;z-index:50;font:11px/1 "Bodoni Moda",Georgia,serif;letter-spacing:.18em;text-transform:uppercase;color:#BFB08A;background:rgba(12,10,16,.92);border:1px solid #7E745C;padding:8px 10px;text-decoration:none}.dojo-back:hover{color:#F1EEE7;border-color:#BFB08A}</style>
<section id="dojo-interest" class="dojo-capture" aria-label="Register interest in Dojo training"><div data-capture="Decision Dojo training"></div></section>
<style>.dojo-capture{--obsidian-raised:#16111D;--pearl:#F1EEE7;--pearl-muted:#B4AEA3;--gold:#BFB08A;--gold-quiet:#7E745C;--amber:#B8793E;--hairline:1px;max-width:860px;margin:48px auto 24px;padding:0 16px;font:16px/1.55 "Bodoni Moda",Georgia,serif;color:var(--pearl)}.dojo-capture .k{margin:0 0 10px;font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--gold)}.dojo-capture .bk-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px 24px}.dojo-capture .bk-field.wide{grid-column:1/-1}.dojo-capture .bk-pick{display:flex;align-items:baseline;gap:10px;border:1px solid var(--gold-quiet);padding:10px 14px;cursor:pointer;color:var(--pearl-muted);font-size:15px}.dojo-capture .bk-pick strong{display:block;font-weight:500;color:var(--pearl)}.dojo-capture .bk-hint,.dojo-capture .status{font-size:13px;font-style:italic;color:var(--pearl-muted)}.dojo-capture .bk-err{color:var(--amber);font-size:14px}.dojo-capture .cta-primary{font:inherit;font-size:12px;letter-spacing:.2em;text-transform:uppercase;padding:14px 22px;border:1px solid var(--gold);color:var(--pearl);background:transparent;cursor:pointer;margin-right:16px}.dojo-capture .as-hp{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}@media (max-width:620px){.dojo-capture .bk-grid{grid-template-columns:1fr}}</style>
<link rel="stylesheet" href="../../assets/assess/assess.css">
<script type="module" src="../../assets/assess/capture.mjs"></script>
""" + FLASH + """
<script type="module" src="/assets/source.mjs"></script>"""


def main(dist: Path) -> None:
    html = (dist / "index.html").read_text(encoding="utf-8")
    if "dojo:viewonly" not in "".join(p.read_text(encoding="utf-8") for p in (dist / "assets").glob("*.js")):
        sys.exit("refusing: this is not a view-only build (run `npm run build:viewonly`)")
    html = re.sub(r"<title>.*?</title>\s*", "", html, flags=re.S)
    html = re.sub(r'<link rel="icon"[^>]*>\s*', "", html)
    html = html.replace('<meta name="viewport" content="width=device-width, initial-scale=1">',
                        '<meta name="viewport" content="width=device-width, initial-scale=1">\n' + HEAD, 1)
    html = html.replace("</body>", HOST + "\n</body>", 1)
    for old in ("assets", "fonts", "js"):
        if (OUT / old).exists():
            shutil.rmtree(OUT / old)
    shutil.copytree(dist / "assets", OUT / "assets")
    (OUT / "index.html").write_text(html, encoding="utf-8")
    print(f"installed {dist} into {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]).resolve())
