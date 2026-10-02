#!/usr/bin/env python3
"""End-to-end check of the booking flow in Chromium (Playwright), against `python3 -m http.server`.

Run: PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers python3 tests/book/e2e_book.py [--shots DIR]
Not picked up by `unittest discover` (the file name does not start with test).
"""
import argparse
import base64
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
ALLOWED = {"nmbambo@ingqiqo-executables.run", "nmbambo@gmail.com"}


# ---------- a tiny ICS parser, to prove the files are well formed ----------

def parse_ics(raw):
    assert "\r\n" in raw and not re.search(r"(?<!\r)\n", raw), "line endings must be CRLF"
    for line in raw.split("\r\n"):
        assert len(line.encode()) <= 75, f"line over 75 octets: {line!r}"
    lines = re.sub(r"\r\n[ \t]", "", raw).split("\r\n")
    assert lines[-1] == "" and lines[0] == "BEGIN:VCALENDAR" and lines[-2] == "END:VCALENDAR"
    stack, props, alarm = [], {}, False
    for line in lines[:-1]:
        name, _, value = line.partition(":")
        key = name.split(";")[0]
        if key == "BEGIN":
            stack.append(value)
        elif key == "END":
            assert stack and stack.pop() == value, f"unbalanced END:{value}"
        elif stack and stack[-1] == "VEVENT":
            props.setdefault(key, []).append((name, value))
        elif stack == ["VCALENDAR"]:
            props.setdefault("CAL-" + key, []).append((name, value))
        elif stack and stack[-1] == "VALARM":
            alarm = True
    assert not stack, "unclosed component"
    one = lambda k: props[k][0][1]
    for k in ("UID", "DTSTAMP", "DTSTART", "DTEND", "SUMMARY"):
        assert k in props, f"missing {k}"
    for k in ("DTSTAMP", "DTSTART", "DTEND"):
        assert re.fullmatch(r"\d{8}T\d{6}Z", one(k)), f"{k} is not UTC: {one(k)}"
    assert one("DTEND") > one("DTSTART")
    assert one("UID").endswith("@nmbambo.github.io")
    assert one("CAL-VERSION") == "2.0" and one("CAL-PRODID") == "-//Ingqiqo Executables//Book//EN"
    props["_alarm"] = alarm
    return props


# ---------- harness ----------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Checks:
    def __init__(self):
        self.passed, self.failed = [], []

    def ok(self, cond, label):
        (self.passed if cond else self.failed).append(label)
        print(("  ok   " if cond else "  FAIL ") + label)
        return cond


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default="/home/claude/es-search-build/03_review")
    args = ap.parse_args()
    shots = Path(args.shots)
    shots.mkdir(parents=True, exist_ok=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                              cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    c = Checks()
    errors = []
    dl_dir = Path(tempfile.mkdtemp())
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])

            def new_context(width, height, mobile=False):
                ctx = browser.new_context(viewport={"width": width, "height": height}, accept_downloads=True,
                                          timezone_id="Europe/London", locale="en-GB",
                                          is_mobile=mobile, has_touch=mobile, device_scale_factor=2 if mobile else 1)
                ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=base)
                ctx.route(re.compile(r"https://fonts\.(googleapis|gstatic)\.com/.*"),
                          lambda r: r.fulfill(status=200, body="", content_type="text/css"))
                return ctx

            def watch(page, label):
                page.on("console", lambda m: errors.append(f"[{label}] console {m.type}: {m.text}") if m.type in ("error", "warning") else None)
                page.on("pageerror", lambda e: errors.append(f"[{label}] pageerror: {e}"))
                page.on("requestfailed", lambda r: errors.append(f"[{label}] request failed: {r.url}"))
                page.on("response", lambda r: errors.append(f"[{label}] HTTP {r.status}: {r.url}")
                        if r.status >= 400 and "busy.json" not in r.url and "favicon" not in r.url else None)

            def overflow(page):
                return page.evaluate("document.documentElement.scrollWidth - window.innerWidth")

            # ===== desktop: request =====
            ctx = new_context(1280, 900)
            page = ctx.new_page()
            watch(page, "desktop-book")
            page.goto(f"{base}/book/")
            page.wait_for_selector("html[data-book-ready='1']")
            c.ok(page.locator("header.top nav a:text-is('Book')").count() == 1, "nav has Book on /book/")
            nav_texts = [t.strip() for t in page.locator("header.top nav a").all_text_contents()]
            c.ok(nav_texts.index("Book") == nav_texts.index("Contact") - 1, "Book sits immediately before Contact")
            c.ok(page.locator(".bk-slot").count() > 100, "slot grid renders many slots")
            c.ok(page.locator("input[name=duration]").count() == 3, "three durations offered")
            first_day = page.locator(".bk-day").first
            c.ok(first_day.get_attribute("open") is not None, "first day is open")
            c.ok(page.locator(".bk-day").count() >= 10, "days are grouped (weekdays only)")
            dates = [d.get_attribute("data-date") for d in page.locator(".bk-day").all()]
            import datetime as dt
            c.ok(all(dt.date.fromisoformat(x).weekday() < 5 for x in dates), "no weekend day is offered")

            # validation first
            page.click("#bk-submit")
            c.ok(page.locator("#bk-done").is_hidden(), "empty submit does not proceed")
            c.ok(page.locator("#bk-name-err").is_visible() and page.locator("#bk-time-err").is_visible(), "errors shown for empty form")
            page.fill("#bk-name", "Thandi Nkosi")
            page.fill("#bk-email", "not-an-email")
            page.fill("#bk-reason", "too short")
            page.click("#bk-submit")
            c.ok("look right" in page.locator("#bk-email-err").inner_text(), "bad email is explained")
            c.ok("at least 30" in page.locator("#bk-reason-err").inner_text(), "short reason is explained")
            c.ok("9 of 800" in page.locator("#bk-count").inner_text(), "character counter counts")

            # choose a length, preferred then alternative
            page.check("input[name=duration][value='45']")
            page.wait_for_selector(".bk-slot")
            c.ok(page.locator(".bk-slot").first.get_attribute("data-start") is not None, "grid redrawn for 45 minutes")
            slots = page.locator(".bk-slot")
            pref_start = slots.nth(2).get_attribute("data-start")
            slots.nth(2).click()
            c.ok(slots.nth(2).get_attribute("aria-pressed") == "true", "preferred slot is marked")
            page.check("input[name=role][value=alternative]")
            page.locator(".bk-day").nth(1).locator("summary").click()
            alt = page.locator(".bk-day").nth(1).locator(".bk-slot").nth(4)
            alt_start = alt.get_attribute("data-start")
            alt.click()
            c.ok(alt.get_attribute("data-role") == "alternative", "alternative slot is marked")
            c.ok("Preferred:" in page.locator("#bk-picks").inner_text() and "Alternative:" in page.locator("#bk-picks").inner_text(), "picks are summarised")
            c.ok("Europe/London" in page.locator("#bk-tz").inner_text(), "viewer zone is shown when different from SAST")
            c.ok("SAST" in page.locator(".bk-slot .bk-s").first.inner_text(), "slots show SAST beside local time")

            page.fill("#bk-email", "thandi@example.co.za")
            page.fill("#bk-org", "Acme Holdings")
            reason = "We keep reversing the same pricing decision, and I want to see whether a decision record would stop it."
            page.fill("#bk-reason", reason)
            page.check("input[name=platform][value=signal]")
            page.screenshot(path=str(shots / "book-desktop.png"), full_page=True)
            page.click("#bk-submit")
            page.wait_for_selector("#bk-done:not([hidden])")
            href = page.get_attribute("#bk-mailto", "href")
            c.ok(href.startswith("mailto:nmbambo@ingqiqo-executables.run?cc=nmbambo@gmail.com&subject="), "mailto goes to the organiser with cc")
            body = urllib.parse.unquote(re.search(r"body=([^&]*)", href).group(1))
            m = re.search(r"https://nmbambo\.github\.io/book/confirm/#([A-Za-z0-9_-]+)", body)
            c.ok(bool(m), "mailto body contains the organiser link")
            c.ok(len(href) <= 1900, f"mailto is {len(href)} characters (under 1900)")
            c.ok("Signal" in body and reason in body, "body summarises platform and reason")
            payload = json.loads(base64.urlsafe_b64decode(m.group(1) + "=" * (-len(m.group(1)) % 4)))
            c.ok(payload["v"] == 1 and payload["email"] == "thandi@example.co.za" and payload["duration"] == 45, "payload carries the request")
            c.ok(payload["preferred"]["start"][:16] == pref_start[:16] and payload["alternative"]["start"][:16] == alt_start[:16], "payload carries both slots")
            c.ok(payload["viewerTz"] == "Europe/London", "payload carries the viewer zone")
            c.ok("Not a booking" not in page.inner_text("#bk-done") and "request, not a booking" in page.inner_text("#bk-done"), "plain note: a request, not a booking")
            c.ok(page.locator("#bk-form").is_hidden(), "form is replaced by the confirmation panel")
            page.screenshot(path=str(shots / "book-sent-desktop.png"), full_page=True)

            # copy
            page.click("#bk-copy")
            page.wait_for_function("document.querySelector('#bk-copy-status').textContent.length > 0")
            clip = page.evaluate("navigator.clipboard.readText()")
            c.ok(clip == page.input_value("#bk-copy-text") and "book/confirm/#" in clip, "Copy request puts the text on the clipboard")
            # hold
            with page.expect_download() as d:
                page.click("#bk-hold")
            hold_path = dl_dir / d.value.suggested_filename
            d.value.save_as(hold_path)
            c.ok(d.value.suggested_filename == "request-hold.ics", "hold downloads as request-hold.ics")
            hold = parse_ics(hold_path.read_bytes().decode())
            c.ok(hold["STATUS"][0][1] == "TENTATIVE" and "CAL-METHOD" not in hold, "hold is tentative")
            c.ok(hold["DTSTART"][0][1][:13] == pref_start.replace("-", "").replace(":", "")[:13], "hold starts at the preferred slot")
            # local event, no personal data
            events = page.evaluate("""() => new Promise((res) => {
                const r = indexedDB.open('ingqiqo-es');
                r.onerror = () => res(null);
                r.onsuccess = () => { const tx = r.result.transaction('events'); const q = tx.objectStore('events').getAll();
                  q.onsuccess = () => res(q.result); };
            })""")
            booking = [e for e in (events or []) if e["type"] == "BookingRequested"]
            c.ok(len(booking) == 1, "BookingRequested event was appended locally")
            if booking:
                blob = json.dumps(booking[0])
                c.ok(set(booking[0]["data"]) == {"duration", "platform", "hasAlternative", "hasSuggestion"}, "event carries only the four allowed fields")
                c.ok(not any(w in blob for w in ("Thandi", "thandi", "Acme", "pricing", "@")), "event carries no personal data")
                c.ok(booking[0]["data"] == {"duration": 45, "platform": "signal", "hasAlternative": True, "hasSuggestion": False}, "event values are right")
            # edit link returns to the form
            page.click("#bk-edit")
            c.ok(page.locator("#bk-form").is_visible() and page.input_value("#bk-name") == "Thandi Nkosi", "Change my request returns to the filled form")
            ctx.close()

            # ===== desktop: organiser =====
            local_link = f"{base}/book/confirm/#{m.group(1)}"
            ctx = new_context(1280, 900)
            page = ctx.new_page()
            watch(page, "desktop-confirm")
            page.goto(local_link)
            page.wait_for_selector("html[data-book-ready='1']")
            c.ok(page.get_attribute("meta[name=robots]", "content") == "noindex", "confirm page is noindex")
            c.ok("Thandi Nkosi" in page.inner_text("#cf-title"), "organiser sees who asked")
            c.ok(reason in page.inner_text("#cf-reason"), "organiser sees the reason")
            c.ok("Europe/London" in page.inner_text("#cf-asked") and "SAST" in page.inner_text("#cf-asked"), "times in SAST and the requester's zone")
            c.ok(page.locator("input[name=cftime]").count() == 3, "preferred, alternative and another time offered")
            c.ok(page.locator("input[name=cfplat]:checked").get_attribute("value") == "signal", "requested platform is preselected")
            # nothing works until the link is supplied
            c.ok(page.get_attribute("#cf-confirm", "aria-disabled") == "true", "confirmation is disabled without a link")
            page.click("#cf-invite", force=True)
            c.ok("Signal call link" in page.inner_text("#cf-hint"), "download without a link explains what is missing")
            # Proton path with the alternative slot
            page.check("input[name=cfplat][value=proton-meet]")
            c.ok(page.get_attribute("a[href='https://calendar.proton.me/']", "target") == "_blank", "Proton Calendar opens in a new tab")
            c.ok(page.locator("a[href='https://proton.me/support/calendar-meet']").count() == 1, "Proton support link is present")
            page.check("input[name=cftime][value=alternative]")
            page.fill("#cf-proton-link", "javascript:alert(1)")
            c.ok(page.get_attribute("#cf-confirm", "aria-disabled") == "true", "a non-https link is refused")
            meet = "https://meet.proton.me/join/abc123xyz"
            page.fill("#cf-proton-link", meet)
            c.ok(page.get_attribute("#cf-confirm", "aria-disabled") == "false", "confirmation enabled once the Meet link is pasted")
            page.screenshot(path=str(shots / "confirm-desktop.png"), full_page=True)
            with page.expect_download() as d:
                page.click("#cf-invite")
            inv_path = dl_dir / "invite.ics"
            d.value.save_as(inv_path)
            c.ok(d.value.suggested_filename == "invite.ics", "invite downloads as invite.ics")
            inv = parse_ics(inv_path.read_bytes().decode())
            c.ok(inv["CAL-METHOD"][0][1] == "REQUEST", "invite is METHOD:REQUEST")
            c.ok(inv["DTSTART"][0][1][:13] == alt_start.replace("-", "").replace(":", "")[:13], "invite starts at the chosen alternative")
            dur_min = (dt.datetime.strptime(inv["DTEND"][0][1], "%Y%m%dT%H%M%SZ") - dt.datetime.strptime(inv["DTSTART"][0][1], "%Y%m%dT%H%M%SZ")).seconds // 60
            c.ok(dur_min == 45, "invite lasts the requested 45 minutes")
            c.ok(inv["URL"][0][1] == meet and inv["LOCATION"][0][1] == meet, "invite carries the Meet link as URL and LOCATION")
            c.ok(inv["ATTENDEE"][0][1] == "mailto:thandi@example.co.za" and "RSVP=TRUE" in inv["ATTENDEE"][0][0], "attendee with RSVP")
            c.ok(inv["ORGANIZER"][0][1] == "mailto:nmbambo@ingqiqo-executables.run", "organiser is the primary address")
            c.ok(inv["_alarm"] and inv["SEQUENCE"][0][1] == "0", "15 minute alarm and sequence 0")
            c.ok(reason.replace(",", "\\,") in inv["DESCRIPTION"][0][1], "description carries the reason, escaped")
            # confirmation mail
            chref = page.get_attribute("#cf-confirm", "href")
            c.ok(chref.startswith("mailto:thandi@example.co.za?"), "confirmation mail goes to the requester")
            cbody = urllib.parse.unquote(re.search(r"body=([^&]*)", chref).group(1))
            c.ok(meet in cbody and "SAST" in cbody and "Europe/London" in cbody, "confirmation carries link and both zones")
            alink = re.search(r"https://nmbambo\.github\.io/book/confirm/#\S+", cbody).group(0)
            c.ok(alink.endswith("&view=attendee"), "confirmation carries the attendee link")
            # another time, propose
            c.ok(page.get_attribute("#cf-propose", "aria-disabled") == "true", "propose is disabled until another time is set")
            page.fill("#cf-other", "2026-10-14T11:00")
            c.ok(page.locator("input[name=cftime][value=other]").is_checked(), "setting another time selects the Another time option")
            c.ok(page.get_attribute("#cf-propose", "aria-disabled") == "false", "propose enabled with another time")
            phref = page.get_attribute("#cf-propose", "href")
            pbody = urllib.parse.unquote(re.search(r"body=([^&]*)", phref).group(1))
            c.ok("Wednesday 14 October 2026, 11:00 to 11:45 SAST" in pbody, "proposal states the time in SAST")
            dhref = page.get_attribute("#cf-decline", "href")
            c.ok(dhref.startswith("mailto:thandi@example.co.za?") and "wish you well" in urllib.parse.unquote(dhref), "decline mail is courteous")
            # another time chosen for the invite
            page.check("input[name=cftime][value=other]")
            with page.expect_download() as d:
                page.click("#cf-invite")
            d.value.save_as(dl_dir / "invite2.ics")
            inv2 = parse_ics((dl_dir / "invite2.ics").read_bytes().decode())
            c.ok(inv2["DTSTART"][0][1] == "20261014T090000Z", "another time is read in SAST (11:00 SAST = 09:00Z)")
            c.ok(inv2["UID"][0][1] == inv["UID"][0][1], "the same request keeps the same calendar UID")
            # Signal at my number
            page.check("input[name=cfplat][value=signal]")
            page.check("#cf-signal-number")
            c.ok(page.get_attribute("#cf-confirm", "aria-disabled") == "false", "Signal at my number needs no link")
            with page.expect_download() as d:
                page.click("#cf-invite")
            d.value.save_as(dl_dir / "invite3.ics")
            inv3 = parse_ics((dl_dir / "invite3.ics").read_bytes().decode())
            c.ok("+27 81 321 3766" in inv3["LOCATION"][0][1], "Signal-at-my-number invite names the number")
            ctx.close()

            # ===== attendee =====
            ctx = new_context(1280, 900)
            page = ctx.new_page()
            watch(page, "attendee")
            page.goto(alink.replace("https://nmbambo.github.io", base))
            page.wait_for_selector("html[data-book-ready='1']")
            c.ok("confirmed" in page.inner_text("#cf-title").lower(), "attendee sees the confirmation")
            c.ok(page.locator("#cf-organiser").is_hidden() and page.locator("#cf-attendee").is_visible(), "organiser tools are hidden from the attendee")
            txt = page.text_content("#at-facts")
            c.ok("SAST" in txt and "Europe/London" in txt and meet in txt, "attendee sees time in both zones and the link")
            c.ok(page.get_attribute("#at-join", "href") == meet, "join link is the validated https link")
            page.screenshot(path=str(shots / "attendee-desktop.png"), full_page=True)
            with page.expect_download() as d:
                page.click("#at-add")
            d.value.save_as(dl_dir / "attendee.ics")
            att = parse_ics((dl_dir / "attendee.ics").read_bytes().decode())
            c.ok(att["CAL-METHOD"][0][1] == "PUBLISH" and "ATTENDEE" not in att, "attendee copy is METHOD:PUBLISH with no ATTENDEE")
            c.ok(att["UID"][0][1] == inv["UID"][0][1] and att["DTSTART"] == inv["DTSTART"], "attendee copy matches the invite")
            ctx.close()

            # ===== bad links =====
            ctx = new_context(1280, 900)
            page = ctx.new_page()
            watch(page, "badlink")
            for frag in ("", "#", "#!!!", "#eyJ2IjoyfQ", "#" + base64.urlsafe_b64encode(b'{"v":1}').decode().rstrip("=")):
                page.goto("about:blank")
                page.goto(f"{base}/book/confirm/{frag}")
                page.wait_for_selector("html[data-book-ready='1'], #cf-error:not([hidden])")
                c.ok(page.locator("#cf-error").is_visible() and page.locator("#cf-organiser").is_hidden(), f"bad link {frag[:12]!r} shows the friendly error")
            # injected markup is shown as text, never run
            evil = {**{k: payload[k] for k in payload}, "name": "<img src=x onerror=window.__pwned=1>", "org": "<b>bold</b>"}
            frag = base64.urlsafe_b64encode(json.dumps(evil).encode()).decode().rstrip("=")
            page.goto("about:blank")
            page.goto(f"{base}/book/confirm/#{frag}")
            page.wait_for_selector("html[data-book-ready='1']")
            c.ok(page.evaluate("window.__pwned === undefined") and page.locator("#cf-facts img").count() == 0, "markup in the payload is inert")
            c.ok("<img" in page.inner_text("#cf-title"), "markup is displayed as plain text")
            ctx.close()

            # ===== suggestion-only request =====
            ctx = new_context(1280, 900)
            page = ctx.new_page()
            watch(page, "suggestion")
            page.goto(f"{base}/book/")
            page.wait_for_selector("html[data-book-ready='1']")
            page.check("#bk-none")
            c.ok(page.locator("#bk-suggest").is_visible(), "None of these suit me reveals the free-text box")
            page.fill("#bk-name", "Sipho Dlamini")
            page.fill("#bk-email", "sipho@example.org")
            page.fill("#bk-reason", "I lead a data team and we cannot agree what a customer is. Where would you begin?")
            page.click("#bk-submit")
            c.ok(page.locator("#bk-done").is_hidden() and page.locator("#bk-suggestion-err").is_visible(), "a suggestion is required when none of the slots suit")
            page.fill("#bk-suggestion", "Thursday mornings after 8, any week in November")
            page.click("#bk-submit")
            page.wait_for_selector("#bk-done:not([hidden])")
            c.ok(page.locator("#bk-hold-li").is_hidden(), "no calendar hold without a slot")
            sb = urllib.parse.unquote(re.search(r"body=([^&]*)", page.get_attribute("#bk-mailto", "href")).group(1))
            c.ok("Suggested time: Thursday mornings" in sb, "the suggestion goes into the email")
            sp = re.search(r"#([A-Za-z0-9_-]+)", sb).group(1)
            ctx.close()
            ctx = new_context(1280, 900)
            page = ctx.new_page()
            watch(page, "suggestion-confirm")
            page.goto(f"{base}/book/confirm/#{sp}")
            page.wait_for_selector("html[data-book-ready='1']")
            c.ok(page.locator("input[name=cftime]").count() == 1 and "Thursday mornings" in page.inner_text("#cf-asked"), "organiser sees the suggestion and sets another time")
            ctx.close()

            # ===== busy feed =====
            ctx = new_context(1280, 900)
            page = ctx.new_page()
            watch(page, "busy-none")
            page.goto(f"{base}/book/")
            page.wait_for_selector("html[data-book-ready='1']")
            first = page.locator(".bk-slot").first.get_attribute("data-start")
            n_before = page.locator(".bk-slot").count()
            ctx.close()
            ctx = new_context(1280, 900)
            page = ctx.new_page()
            watch(page, "busy-feed")
            t0 = dt.datetime.fromisoformat(first.replace("Z", "+00:00"))
            busy = [{"start": t0.isoformat().replace("+00:00", "Z"), "end": (t0 + dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z")}]
            ctx.route("**/data/busy.json", lambda r: r.fulfill(status=200, body=json.dumps(busy), content_type="application/json"))
            page.goto(f"{base}/book/")
            page.wait_for_selector("html[data-book-ready='1']")
            starts = [b.get_attribute("data-start") for b in page.locator(".bk-slot").all()]
            c.ok(first not in starts and page.locator(".bk-slot").count() < n_before, "busy time from data/busy.json removes slots")
            c.ok(not any(abs((dt.datetime.fromisoformat(x.replace("Z", "+00:00")) - t0).total_seconds()) < 3600 + 15 * 60 and
                         dt.datetime.fromisoformat(x.replace("Z", "+00:00")) > t0 - dt.timedelta(minutes=45) for x in starts), "slots stay clear of the busy hour and its 15 minute buffer")
            ctx.close()
            ctx = new_context(1280, 900)
            page = ctx.new_page()
            seen = []
            page.on("pageerror", lambda e: seen.append(str(e)))
            ctx.route("**/data/busy.json", lambda r: r.fulfill(status=404, body="not found"))
            page.goto(f"{base}/book/")
            page.wait_for_selector("html[data-book-ready='1']")
            c.ok(page.locator(".bk-slot").count() == n_before and not seen, "a missing busy.json is normal: the page still works")
            ctx.close()

            # ===== mobile 390 =====
            ctx = new_context(390, 844, mobile=True)
            page = ctx.new_page()
            watch(page, "mobile")
            for path in ("/", "/book/", "/engage/", "/method/", "/search/", "/work/decision-ir/"):
                page.goto(f"{base}{path}")
                page.wait_for_load_state("networkidle")
                c.ok(overflow(page) <= 0, f"390px: no horizontal scroll on {path} (overflow {overflow(page)}px)")
                nav = page.evaluate("""() => { const n = document.querySelector('header.top nav'); const r = n.getBoundingClientRect();
                   const vis = [...n.querySelectorAll('a')].filter(a => getComputedStyle(a).display !== 'none');
                   return {right: r.right, w: window.innerWidth, items: vis.map(a => a.textContent)}; }""")
                c.ok(nav["right"] <= nav["w"] and "Book" in nav["items"], f"390px: nav fits on {path} and keeps Book ({', '.join(nav['items'])})")
            page.goto(f"{base}/book/")
            page.wait_for_selector("html[data-book-ready='1']")
            page.screenshot(path=str(shots / "book-mobile.png"), full_page=True)
            page.locator(".bk-slot").nth(1).tap()
            page.fill("#bk-name", "Thandi Nkosi")
            page.fill("#bk-email", "thandi@example.co.za")
            page.fill("#bk-reason", reason)
            page.tap("#bk-submit")
            page.wait_for_selector("#bk-done:not([hidden])")
            c.ok(overflow(page) <= 0, "390px: no horizontal scroll after submitting")
            page.screenshot(path=str(shots / "book-sent-mobile.png"), full_page=True)
            mm = re.search(r"#([A-Za-z0-9_-]+)", urllib.parse.unquote(page.get_attribute("#bk-mailto", "href"))).group(1)
            page.goto("about:blank")
            page.goto(f"{base}/book/confirm/#{mm}")
            page.wait_for_selector("html[data-book-ready='1']")
            c.ok(overflow(page) <= 0, f"390px: no horizontal scroll on /book/confirm/ (overflow {overflow(page)}px)")
            page.screenshot(path=str(shots / "confirm-mobile.png"), full_page=True)
            page.goto("about:blank")
            page.goto(alink.replace("https://nmbambo.github.io", base))
            page.wait_for_selector("html[data-book-ready='1']")
            c.ok(overflow(page) <= 0, "390px: no horizontal scroll on the attendee view")
            ctx.close()
            browser.close()
    finally:
        server.terminate()

    noise = [e for e in errors]
    c.ok(not noise, "no console errors, warnings, failed requests or 4xx/5xx responses" + ("" if not noise else ": " + "; ".join(noise[:5])))
    print(f"\n{len(c.passed)} passed, {len(c.failed)} failed")
    for f in c.failed:
        print("  FAILED:", f)
    return 1 if c.failed else 0


if __name__ == "__main__":
    sys.exit(main())
