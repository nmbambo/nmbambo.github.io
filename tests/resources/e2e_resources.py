#!/usr/bin/env python3
"""End-to-end check of the Resources library in Chromium (Playwright), against `python3 -m http.server`.

Every local link and image on the library and reader pages resolves; no reader shows a held-back page;
the Dojo renders from its own files (no CDN), saves progress only in the browser, and keeps a way back;
nothing scrolls sideways at 360px. Usage: python3 tests/resources/e2e_resources.py [--shots DIR]
"""
import argparse
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
READERS = ["seeing-clearly", "company-profile", "engagement-pack", "the-progress-record"]
HELD_BACK = {"seeing-clearly": {6, 23, 26}, "company-profile": {11, 15, 16, 20, 24, 26, 27}}


class Check:
    def __init__(self):
        self.passed = self.failed = 0

    def ok(self, cond, msg):
        if cond:
            self.passed += 1
            print(f"  ok   {msg}")
        else:
            self.failed += 1
            print(f"  FAIL {msg}")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def status(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default=None)
    a = ap.parse_args()
    shots = Path(a.shots) if a.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    port = free_port()
    srv = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"], cwd=ROOT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    c = Check()
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/")
                break
            except OSError:
                time.sleep(0.1)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))

            # ===== library page
            page.goto(f"{base}/resources/")
            page.wait_for_load_state("networkidle")
            c.ok(page.title() == "Resources — Nkosinathi Mbambo", "library has its title")
            c.ok(page.locator("header.top nav a[aria-current=page]").text_content().strip() == "Resources", "nav marks Resources")
            links = page.eval_on_selector_all("a[href], img[src], link[href]",
                                              "els => els.map(e => e.getAttribute('href') || e.getAttribute('src'))")
            local = sorted({urljoin(f"{base}/resources/", h).split("#")[0] for h in links
                            if not urlparse(h).scheme and not h.startswith("mailto:")})
            bad = [u for u in local if status(u) != 200]
            c.ok(not bad, f"all {len(local)} local links on the library resolve {bad or ''}")
            text = page.inner_text("body")
            c.ok("Made with Claude Design" not in page.content(), "no editor badge")
            c.ok(not re.search(r"R\s?\d", text), "no rand amounts on the library")
            hosts = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            c.ok(not any("ingqiqo-executables.run" in h for h in hosts), "no links to member hosts that are not live yet")
            if shots:
                page.screenshot(path=str(shots / "resources.png"), full_page=True)

            # ===== readers
            for slug in READERS:
                page.goto(f"{base}/resources/read/{slug}.html")
                page.wait_for_load_state("networkidle")
                srcs = page.eval_on_selector_all(".leaf img", "els => els.map(e => e.getAttribute('src'))")
                nums = {int(re.search(r"(\d+)\.webp$", s).group(1)) for s in srcs}
                bad = [s for s in srcs if status(urljoin(f"{base}/resources/read/", s)) != 200]
                c.ok(srcs and not bad, f"{slug}: {len(srcs)} pages, every image resolves")
                c.ok(not (nums & HELD_BACK.get(slug, set())), f"{slug}: held-back pages are not shown")
                on_disk = {int(p.stem) for p in (ROOT / "resources/read/img" / slug).glob("*.webp")}
                c.ok(not (on_disk & HELD_BACK.get(slug, set())), f"{slug}: held-back pages are not published at all")
                alts = page.eval_on_selector_all(".leaf img", "els => els.every(e => e.alt && e.width > 0)")
                c.ok(alts, f"{slug}: every page image has alt text and dimensions")
                if slug == "seeing-clearly":
                    w, h = page.eval_on_selector(".leaf img", "e => [e.naturalWidth, e.naturalHeight]")
                    c.ok(w > h, "seeing-clearly: spreads are turned upright (landscape)")
            for pdf in ("company-profile", "engagement-pack"):
                c.ok(status(f"{base}/resources/pdf/{pdf}.pdf") == 200, f"{pdf}.pdf is served")
            npages = subprocess.run(["qpdf", "--show-npages", str(ROOT / "resources/pdf/company-profile.pdf")],
                                    capture_output=True, text=True).stdout.strip()
            c.ok(npages == "21", f"company-profile.pdf is the 21-page public edition ({npages})")

            # ===== dojo
            cdn = []
            page.on("request", lambda r: cdn.append(r.url) if "unpkg.com" in r.url else None)
            errors.clear()
            page.goto(f"{base}/resources/dojo/")
            page.wait_for_selector("text=One kick, ten thousand times", timeout=20000)
            page.wait_for_load_state("networkidle")
            c.ok(not errors, f"dojo renders with no script errors {errors[:2]}")
            c.ok(not cdn, "dojo loads React from its own files, not a CDN")
            c.ok(page.locator("a.dojo-back[href='../']").count() == 1, "dojo keeps a way back to the library")
            c.ok("Made with Claude Design" not in page.content(), "dojo has no editor badge")
            if shots:
                page.screenshot(path=str(shots / "dojo.png"))
            ctx.close()

            # ===== phone width
            ctx = browser.new_context(viewport={"width": 360, "height": 780}, is_mobile=True, has_touch=True)
            page = ctx.new_page()
            for path in ("/resources/", "/resources/read/company-profile.html", "/resources/dojo/"):
                page.goto(f"{base}{path}")
                page.wait_for_load_state("networkidle")
                over = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                c.ok(over <= 0, f"360px: no horizontal scroll on {path} ({over}px)")
                if shots and path == "/resources/":
                    page.screenshot(path=str(shots / "resources-360.png"), full_page=True)
            ctx.close()
            browser.close()
    finally:
        srv.terminate()
    print(f"{c.passed} passed, {c.failed} failed")
    return 1 if c.failed else 0


if __name__ == "__main__":
    sys.exit(main())
