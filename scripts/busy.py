#!/usr/bin/env python3
"""Turn a private iCalendar feed into data/busy.json: only start and end times, no titles.

Python 3.11 standard library only. The feed address is read from the environment variable
BUSY_ICS_URL (a Proton Calendar share link, for example). It is never printed or written.
If the variable is not set, nothing happens and no file is written.

Reads:   availability.json for the horizon and time zone (assets/book/availability.json)
Writes:  data/busy.json, [{"start": "...Z", "end": "...Z"}], merged and sorted

Recurrence: FREQ=DAILY and WEEKLY (INTERVAL, BYDAY, COUNT, UNTIL, WKST), EXDATE, and
RECURRENCE-ID overrides. MONTHLY and YEARLY repeat on the same day of the month or year.
Anything else is expanded as its first occurrence only, with a note on stderr.
Events marked TRANSP:TRANSPARENT or STATUS:CANCELLED are ignored.

Usage: python3 scripts/busy.py [--ics-file FILE] [--out FILE] [--days N] [--tz ZONE]
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
AVAILABILITY = ROOT / "assets" / "book" / "availability.json"
UTC = timezone.utc
WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
MAX_OCCURRENCES = 5000

# A few Windows zone names that calendar exports use.
WINDOWS_ZONES = {
    "South Africa Standard Time": "Africa/Johannesburg",
    "W. Europe Standard Time": "Europe/Berlin",
    "GMT Standard Time": "Europe/London",
    "Eastern Standard Time": "America/New_York",
    "Pacific Standard Time": "America/Los_Angeles",
}


# ---------- reading the feed ----------

def unfold(text):
    """RFC 5545 unfolding: a line that starts with a space or tab continues the previous one."""
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def split_line(line):
    """'NAME;P=V;P2=V2:value' -> (NAME, {P: V}, value). Quoted parameter values may contain ':' and ';'."""
    in_quote = False
    colon = -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif ch == ":" and not in_quote:
            colon = i
            break
    if colon < 0:
        return None
    head, value = line[:colon], line[colon + 1:]
    parts = re.split(r';(?=(?:[^"]*"[^"]*")*[^"]*$)', head)
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def parse_events(text):
    """Return a list of dicts, one per VEVENT: each property name maps to a list of (params, value)."""
    events, cur = [], None
    for line in unfold(text):
        up = line.upper()
        if up == "BEGIN:VEVENT":
            cur = {}
        elif up == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
        elif cur is not None:
            sp = split_line(line)
            if sp:
                cur.setdefault(sp[0], []).append((sp[1], sp[2]))
    return events


def zone(name, default):
    if not name:
        return default
    try:
        return ZoneInfo(name)
    except Exception:
        mapped = WINDOWS_ZONES.get(name)
        if mapped:
            return ZoneInfo(mapped)
        return default


def parse_dt(value, params, default_tz):
    """Return (aware datetime in UTC, wall datetime, tzinfo, is_date)."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        d = datetime.strptime(value, "%Y%m%d")
        tz = default_tz
        wall = d.replace(tzinfo=tz)
        return wall.astimezone(UTC), d, tz, True
    if value.endswith("Z"):
        d = datetime.strptime(value, "%Y%m%dT%H%M%SZ")
        return d.replace(tzinfo=UTC), d, UTC, False
    d = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    tz = zone(params.get("TZID"), default_tz)
    return d.replace(tzinfo=tz).astimezone(UTC), d, tz, False


def parse_duration(value):
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return sign * timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def parse_rrule(value):
    rule = {}
    for part in value.split(";"):
        k, _, v = part.partition("=")
        rule[k.upper()] = v
    return rule


# ---------- expanding ----------

def wall_to_utc(wall, tz):
    return wall.replace(tzinfo=tz).astimezone(UTC)


def expand(rule, wall_start, tz, until_utc_limit, default_tz):
    """Yield wall-clock start datetimes (naive, in the event's zone) for the recurrence, in order.

    Stops at COUNT, UNTIL, or when wall times pass until_utc_limit. EXDATE is applied by the caller
    (EXDATE does not reduce COUNT, per RFC 5545).
    """
    freq = rule.get("FREQ", "")
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT") else None
    until = None
    if rule.get("UNTIL"):
        u = rule["UNTIL"]
        if re.fullmatch(r"\d{8}", u):
            until = datetime.strptime(u, "%Y%m%d").replace(tzinfo=tz) + timedelta(days=1) - timedelta(seconds=1)
            until = until.astimezone(UTC)
        else:
            uu, _, _, _ = parse_dt(u, {}, tz)
            until = uu
    wkst = WEEKDAYS.get(rule.get("WKST", "MO"), 0)
    produced = 0

    def ok(wall):
        nonlocal produced
        u = wall_to_utc(wall, tz)
        if until is not None and u > until:
            return None
        if u > until_utc_limit:
            return None
        return u

    if freq == "DAILY":
        n = 0
        while True:
            wall = wall_start + timedelta(days=n * interval)
            if ok(wall) is None:
                return
            yield wall
            produced += 1
            if count is not None and produced >= count:
                return
            n += 1
            if produced > MAX_OCCURRENCES:
                return
    elif freq == "WEEKLY":
        days = []
        for tok in (rule.get("BYDAY") or "").split(","):
            m = re.fullmatch(r"[+-]?\d*([A-Z]{2})", tok.strip())
            if m and m.group(1) in WEEKDAYS:
                days.append(WEEKDAYS[m.group(1)])
        if not days:
            days = [wall_start.weekday()]
        days = sorted(set(days), key=lambda d: (d - wkst) % 7)
        week0 = wall_start - timedelta(days=(wall_start.weekday() - wkst) % 7)
        n = 0
        while True:
            week = week0 + timedelta(weeks=n * interval)
            for d in days:
                wall = week + timedelta(days=(d - wkst) % 7)
                if wall < wall_start:
                    continue
                if ok(wall) is None:
                    return
                yield wall
                produced += 1
                if count is not None and produced >= count:
                    return
                if produced > MAX_OCCURRENCES:
                    return
            n += 1
    elif freq in ("MONTHLY", "YEARLY"):
        n = 0
        while True:
            if freq == "MONTHLY":
                months = wall_start.month - 1 + n * interval
                y, m = wall_start.year + months // 12, months % 12 + 1
            else:
                y, m = wall_start.year + n * interval, wall_start.month
            n += 1
            try:
                wall = wall_start.replace(year=y, month=m)
            except ValueError:  # e.g. the 31st in a short month: skipped, as RFC 5545 says
                if y > until_utc_limit.year + 1:
                    return
                continue
            if ok(wall) is None:
                return
            yield wall
            produced += 1
            if count is not None and produced >= count:
                return
            if produced > MAX_OCCURRENCES:
                return
    else:
        print(f"note: FREQ={freq or '?'} is not supported; using the first occurrence only", file=sys.stderr)
        yield wall_start


def event_periods(ev, window_start, window_end, default_tz):
    """Busy (start_utc, end_utc) pairs for one VEVENT, clipped to nothing: whole periods that overlap the window."""
    def first(name):
        return ev.get(name, [(None, None)])[0]

    transp = (first("TRANSP")[1] or "").upper()
    status = (first("STATUS")[1] or "").upper()
    if transp == "TRANSPARENT" or status == "CANCELLED":
        return []
    p, v = first("DTSTART")
    if v is None:
        return []
    try:
        start_utc, wall, tz, is_date = parse_dt(v, p, default_tz)
        dtend = first("DTEND")
        duration = first("DURATION")
        if dtend[1]:
            end_utc, _, _, _ = parse_dt(dtend[1], dtend[0], default_tz)
            length = end_utc - start_utc
        elif duration[1] and parse_duration(duration[1]) is not None:
            length = parse_duration(duration[1])
        else:
            length = timedelta(days=1) if is_date else timedelta(0)
    except (ValueError, KeyError):
        return []
    if length <= timedelta(0):
        return []

    excluded = set()
    for ep, evalue in ev.get("EXDATE", []):
        for item in evalue.split(","):
            if item.strip():
                try:
                    u, _, _, _ = parse_dt(item, ep, tz)
                    excluded.add(u)
                except ValueError:
                    pass
    starts = []
    if "RRULE" in ev:
        rule = parse_rrule(ev["RRULE"][0][1])
        for w in expand(rule, wall, tz, window_end, default_tz):
            u = wall_to_utc(w, tz)
            if u in excluded:
                continue
            starts.append(u)
    else:
        starts.append(start_utc)
    return [(s, s + length) for s in starts if s + length > window_start and s < window_end]


def busy_periods(text, now, days, default_tz):
    window_start, window_end = now, now + timedelta(days=days)
    events = parse_events(text)
    # An override (RECURRENCE-ID) replaces one occurrence of its series; the series skips that start.
    replaced = {}
    for ev in events:
        if "RECURRENCE-ID" in ev and "UID" in ev:
            p, v = ev["RECURRENCE-ID"][0]
            try:
                u, _, _, _ = parse_dt(v, p, default_tz)
            except ValueError:
                continue
            replaced.setdefault(ev["UID"][0][1], set()).add(u)
    out = []
    for ev in events:
        uid = ev.get("UID", [(None, None)])[0][1]
        if "RECURRENCE-ID" not in ev and uid in replaced:
            ev = dict(ev)
            ev["EXDATE"] = ev.get("EXDATE", []) + [({}, ",".join(x.strftime("%Y%m%dT%H%M%SZ") for x in replaced[uid]))]
        out.extend(event_periods(ev, window_start, window_end, default_tz))
    return merge(out)


def merge(periods):
    periods = sorted(periods)
    merged = []
    for s, e in periods:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def to_json(periods):
    fmt = lambda d: d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [{"start": fmt(s), "end": fmt(e)} for s, e in periods]


# ---------- running ----------

def fetch(url):
    url = re.sub(r"^webcal://", "https://", url.strip(), flags=re.I)
    if not url.lower().startswith("https://"):
        raise ValueError("the feed address must be https")
    req = urllib.request.Request(url, headers={"User-Agent": "nmbambo-busy-feed/1"})
    with urllib.request.urlopen(req, timeout=30) as res:  # nosec: address is the owner's own secret
        return res.read().decode("utf-8", errors="replace")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ics-file", help="read this file instead of fetching BUSY_ICS_URL (for testing)")
    ap.add_argument("--out", default=str(ROOT / "data" / "busy.json"))
    ap.add_argument("--days", type=int, help="horizon in days (default: horizonDays + 2 from availability.json)")
    ap.add_argument("--tz", help="zone for all-day and floating times (default: availability.json timezone)")
    args = ap.parse_args(argv)

    avail = {}
    try:
        avail = json.loads(AVAILABILITY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    days = args.days or int(avail.get("horizonDays", 21)) + 2
    default_tz = zone(args.tz or avail.get("timezone"), ZoneInfo("Africa/Johannesburg"))

    if args.ics_file:
        text = Path(args.ics_file).read_text(encoding="utf-8")
    else:
        url = os.environ.get("BUSY_ICS_URL", "").strip()
        if not url:
            print("BUSY_ICS_URL is not set; nothing to do.")
            return 0
        try:
            text = fetch(url)
        except Exception as exc:  # never echo the exception text: it can contain the address
            print(f"Could not fetch the feed ({type(exc).__name__}).", file=sys.stderr)
            return 1
    if "BEGIN:VCALENDAR" not in text:
        print("The feed did not look like a calendar; leaving busy.json as it is.", file=sys.stderr)
        return 1

    periods = busy_periods(text, datetime.now(UTC), days, default_tz)
    body = json.dumps(to_json(periods), indent=1) + "\n"
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists() or out.read_text(encoding="utf-8") != body:
        out.write_text(body, encoding="utf-8")
        print(f"Wrote {len(periods)} busy periods.")
    else:
        print("No change.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
