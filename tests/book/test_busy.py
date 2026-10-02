import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import busy  # noqa: E402

UTC = timezone.utc
SAST = ZoneInfo("Africa/Johannesburg")
NOW = datetime(2026, 10, 5, 5, 0, tzinfo=UTC)  # Monday 07:00 SAST


def cal(*events):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//x//y//EN\r\n" + "".join(events) + "END:VCALENDAR\r\n"


def vevent(*props):
    return "BEGIN:VEVENT\r\n" + "".join(p + "\r\n" for p in props) + "END:VEVENT\r\n"


def run(text, days=21, tz=SAST, now=NOW):
    return busy.to_json(busy.busy_periods(text, now, days, tz))


def starts(out):
    return [o["start"] for o in out]


class Parsing(unittest.TestCase):
    def test_unfold_joins_continuation_lines(self):
        self.assertEqual(busy.unfold("SUMMARY:Very long\r\n  title\r\nUID:1\r\n"), ["SUMMARY:Very long title", "UID:1"])

    def test_split_line_handles_params_and_quoted_colons(self):
        name, params, value = busy.split_line('DTSTART;TZID="Africa/Johannesburg";VALUE=DATE-TIME:20261006T090000')
        self.assertEqual((name, params["TZID"], value), ("DTSTART", "Africa/Johannesburg", "20261006T090000"))
        name, params, value = busy.split_line('ATTENDEE;CN="A: B":mailto:a@b.co')
        self.assertEqual((name, params["CN"], value), ("ATTENDEE", "A: B", "mailto:a@b.co"))

    def test_duration(self):
        self.assertEqual(busy.parse_duration("PT1H30M").total_seconds(), 5400)
        self.assertEqual(busy.parse_duration("P1DT2H").total_seconds(), 93600)
        self.assertEqual(busy.parse_duration("P1W").days, 7)
        self.assertIsNone(busy.parse_duration("soon"))


class Stripping(unittest.TestCase):
    def test_only_start_and_end_survive(self):
        text = cal(vevent(
            "UID:secret-uid", "SUMMARY:Board meeting with Acme", "DESCRIPTION:Confidential numbers",
            "LOCATION:Room 4", "ATTENDEE:mailto:ceo@acme.example", "ORGANIZER:mailto:me@x.example",
            "DTSTART:20261006T070000Z", "DTEND:20261006T080000Z"))
        out = run(text)
        self.assertEqual(out, [{"start": "2026-10-06T07:00:00Z", "end": "2026-10-06T08:00:00Z"}])
        blob = json.dumps(out)
        for leak in ("Board", "Acme", "Confidential", "Room", "secret", "ceo@", "mailto"):
            self.assertNotIn(leak, blob)
        self.assertEqual(set(out[0]), {"start", "end"})

    def test_transparent_and_cancelled_are_ignored(self):
        text = cal(
            vevent("DTSTART:20261006T070000Z", "DTEND:20261006T080000Z", "TRANSP:TRANSPARENT"),
            vevent("DTSTART:20261007T070000Z", "DTEND:20261007T080000Z", "STATUS:CANCELLED"),
            vevent("DTSTART:20261008T070000Z", "DTEND:20261008T080000Z", "TRANSP:OPAQUE", "STATUS:CONFIRMED"))
        self.assertEqual(starts(run(text)), ["2026-10-08T07:00:00Z"])

    def test_events_outside_the_window_are_dropped(self):
        text = cal(
            vevent("DTSTART:20260901T070000Z", "DTEND:20260901T080000Z"),
            vevent("DTSTART:20261201T070000Z", "DTEND:20261201T080000Z"),
            vevent("DTSTART:20261006T070000Z", "DTEND:20261006T080000Z"))
        self.assertEqual(starts(run(text, days=23)), ["2026-10-06T07:00:00Z"])

    def test_overlapping_periods_merge(self):
        text = cal(
            vevent("DTSTART:20261006T070000Z", "DTEND:20261006T080000Z"),
            vevent("DTSTART:20261006T073000Z", "DTEND:20261006T090000Z"),
            vevent("DTSTART:20261006T090000Z", "DTEND:20261006T093000Z"))
        self.assertEqual(run(text), [{"start": "2026-10-06T07:00:00Z", "end": "2026-10-06T09:30:00Z"}])

    def test_zero_length_and_broken_events_are_skipped(self):
        text = cal(vevent("DTSTART:20261006T070000Z"), vevent("SUMMARY:no start"),
                   vevent("DTSTART:garbage", "DTEND:20261006T080000Z"))
        self.assertEqual(run(text), [])


class TimeZones(unittest.TestCase):
    def test_tzid_is_converted_to_utc(self):
        text = cal(vevent("DTSTART;TZID=Africa/Johannesburg:20261006T090000", "DTEND;TZID=Africa/Johannesburg:20261006T100000"))
        self.assertEqual(run(text), [{"start": "2026-10-06T07:00:00Z", "end": "2026-10-06T08:00:00Z"}])

    def test_duration_instead_of_dtend(self):
        text = cal(vevent("DTSTART:20261006T070000Z", "DURATION:PT45M"))
        self.assertEqual(run(text)[0]["end"], "2026-10-06T07:45:00Z")

    def test_all_day_event_covers_the_local_day(self):
        text = cal(vevent("DTSTART;VALUE=DATE:20261007", "DTEND;VALUE=DATE:20261008"))
        self.assertEqual(run(text), [{"start": "2026-10-06T22:00:00Z", "end": "2026-10-07T22:00:00Z"}])

    def test_floating_time_uses_the_default_zone(self):
        text = cal(vevent("DTSTART:20261006T090000", "DTEND:20261006T100000"))
        self.assertEqual(run(text)[0]["start"], "2026-10-06T07:00:00Z")

    def test_unknown_tzid_falls_back_to_default(self):
        text = cal(vevent("DTSTART;TZID=Nowhere/Land:20261006T090000", "DTEND;TZID=Nowhere/Land:20261006T100000"))
        self.assertEqual(run(text)[0]["start"], "2026-10-06T07:00:00Z")

    def test_windows_zone_name(self):
        text = cal(vevent("DTSTART;TZID=South Africa Standard Time:20261006T090000", "DTEND;TZID=South Africa Standard Time:20261006T100000"))
        self.assertEqual(run(text)[0]["start"], "2026-10-06T07:00:00Z")

    def test_recurrence_keeps_local_wall_time_across_dst(self):
        text = cal(vevent("DTSTART;TZID=Europe/London:20261022T090000", "DTEND;TZID=Europe/London:20261022T100000",
                          "RRULE:FREQ=DAILY;COUNT=6"))
        out = run(text, now=datetime(2026, 10, 20, tzinfo=UTC))
        got = {o["start"] for o in out}
        self.assertIn("2026-10-24T08:00:00Z", got)   # BST
        self.assertIn("2026-10-26T09:00:00Z", got)   # GMT after the clocks go back


class Recurrence(unittest.TestCase):
    def test_daily_with_count(self):
        text = cal(vevent("DTSTART:20261006T070000Z", "DTEND:20261006T073000Z", "RRULE:FREQ=DAILY;COUNT=3"))
        self.assertEqual(starts(run(text)), ["2026-10-06T07:00:00Z", "2026-10-07T07:00:00Z", "2026-10-08T07:00:00Z"])

    def test_daily_interval_and_until(self):
        text = cal(vevent("DTSTART:20261006T070000Z", "DTEND:20261006T073000Z", "RRULE:FREQ=DAILY;INTERVAL=2;UNTIL=20261012T235959Z"))
        self.assertEqual(starts(run(text)), ["2026-10-06T07:00:00Z", "2026-10-08T07:00:00Z", "2026-10-10T07:00:00Z", "2026-10-12T07:00:00Z"])

    def test_until_as_a_date_includes_that_day(self):
        text = cal(vevent("DTSTART:20261006T070000Z", "DTEND:20261006T073000Z", "RRULE:FREQ=DAILY;UNTIL=20261008"))
        self.assertEqual(len(run(text)), 3)

    def test_weekly_byday(self):
        text = cal(vevent("DTSTART:20261006T070000Z", "DTEND:20261006T080000Z", "RRULE:FREQ=WEEKLY;BYDAY=TU,TH;COUNT=5"))
        self.assertEqual(starts(run(text)), ["2026-10-06T07:00:00Z", "2026-10-08T07:00:00Z", "2026-10-13T07:00:00Z",
                                             "2026-10-15T07:00:00Z", "2026-10-20T07:00:00Z"])

    def test_weekly_without_byday_uses_the_start_weekday(self):
        text = cal(vevent("DTSTART:20261006T070000Z", "DTEND:20261006T080000Z", "RRULE:FREQ=WEEKLY;COUNT=3"))
        self.assertEqual(starts(run(text)), ["2026-10-06T07:00:00Z", "2026-10-13T07:00:00Z", "2026-10-20T07:00:00Z"])

    def test_weekly_interval_two(self):
        text = cal(vevent("DTSTART:20261006T070000Z", "DTEND:20261006T080000Z", "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=TU"))
        self.assertEqual(starts(run(text, days=25)), ["2026-10-06T07:00:00Z", "2026-10-20T07:00:00Z"])

    def test_weekly_starting_midweek_skips_earlier_days_of_the_first_week(self):
        text = cal(vevent("DTSTART:20261008T070000Z", "DTEND:20261008T080000Z", "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,TH;COUNT=3"))
        self.assertEqual(starts(run(text)), ["2026-10-08T07:00:00Z", "2026-10-12T07:00:00Z", "2026-10-13T07:00:00Z"])

    def test_exdate_removes_an_occurrence_without_shortening_count(self):
        text = cal(vevent("DTSTART:20261006T070000Z", "DTEND:20261006T073000Z", "RRULE:FREQ=DAILY;COUNT=4",
                          "EXDATE:20261007T070000Z"))
        self.assertEqual(starts(run(text)), ["2026-10-06T07:00:00Z", "2026-10-08T07:00:00Z", "2026-10-09T07:00:00Z"])

    def test_exdate_with_tzid_and_several_values(self):
        text = cal(vevent("DTSTART;TZID=Africa/Johannesburg:20261006T090000", "DTEND;TZID=Africa/Johannesburg:20261006T093000",
                          "RRULE:FREQ=DAILY;COUNT=4", "EXDATE;TZID=Africa/Johannesburg:20261007T090000,20261008T090000"))
        self.assertEqual(starts(run(text)), ["2026-10-06T07:00:00Z", "2026-10-09T07:00:00Z"])

    def test_open_ended_rule_stops_at_the_horizon(self):
        text = cal(vevent("DTSTART:20260901T070000Z", "DTEND:20260901T073000Z", "RRULE:FREQ=DAILY"))
        out = run(text, days=10)
        self.assertEqual(len(out), 10)
        self.assertTrue(out[-1]["start"] <= "2026-10-15T07:00:00Z")

    def test_series_that_started_long_ago_still_appears(self):
        text = cal(vevent("DTSTART:20250106T070000Z", "DTEND:20250106T080000Z", "RRULE:FREQ=WEEKLY;BYDAY=MO"))
        self.assertEqual(starts(run(text, days=15))[:2], ["2026-10-05T07:00:00Z", "2026-10-12T07:00:00Z"])

    def test_override_replaces_one_occurrence(self):
        text = cal(
            vevent("UID:a", "DTSTART:20261006T070000Z", "DTEND:20261006T080000Z", "RRULE:FREQ=DAILY;COUNT=3"),
            vevent("UID:a", "RECURRENCE-ID:20261007T070000Z", "DTSTART:20261007T120000Z", "DTEND:20261007T130000Z"))
        self.assertEqual(starts(run(text)), ["2026-10-06T07:00:00Z", "2026-10-07T12:00:00Z", "2026-10-08T07:00:00Z"])

    def test_monthly_and_yearly(self):
        monthly = cal(vevent("DTSTART:20260815T070000Z", "DTEND:20260815T080000Z", "RRULE:FREQ=MONTHLY"))
        self.assertEqual(starts(run(monthly, days=60)), ["2026-10-15T07:00:00Z", "2026-11-15T07:00:00Z"])
        yearly = cal(vevent("DTSTART:20250910T070000Z", "DTEND:20250910T080000Z", "RRULE:FREQ=YEARLY"))
        self.assertEqual(run(yearly, days=30), [])

    def test_unsupported_rule_gives_its_first_occurrence_only(self):
        text = cal(vevent("DTSTART:20261006T070000Z", "DTEND:20261006T080000Z", "RRULE:FREQ=HOURLY;COUNT=5"))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            out = run(text)
        self.assertEqual(len(out), 1)
        self.assertIn("not supported", err.getvalue())


class Running(unittest.TestCase):
    def test_no_secret_means_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "busy.json"
            with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()) as so:
                self.assertEqual(busy.main(["--out", str(out)]), 0)
            self.assertFalse(out.exists())
            self.assertIn("nothing to do", so.getvalue())

    def test_blank_secret_means_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "busy.json"
            with mock.patch.dict(os.environ, {"BUSY_ICS_URL": "  "}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(busy.main(["--out", str(out)]), 0)
            self.assertFalse(out.exists())

    def test_file_input_writes_json_once_and_reports_no_change_after(self):
        text = cal(vevent("SUMMARY:Private", "DTSTART:20261006T070000Z", "DTEND:20261006T080000Z"))
        with tempfile.TemporaryDirectory() as d:
            src, out = Path(d) / "f.ics", Path(d) / "sub" / "busy.json"
            src.write_text(text)
            fixed = datetime(2026, 10, 5, 5, 0, tzinfo=UTC)
            with mock.patch.object(busy, "datetime") as dt, contextlib.redirect_stdout(io.StringIO()) as so:
                dt.now.return_value = fixed
                dt.side_effect = lambda *a, **k: datetime(*a, **k)
                dt.strptime = datetime.strptime
                self.assertEqual(busy.main(["--ics-file", str(src), "--out", str(out)]), 0)
                first = out.read_text()
                self.assertEqual(busy.main(["--ics-file", str(src), "--out", str(out)]), 0)
            self.assertEqual(json.loads(first), [{"start": "2026-10-06T07:00:00Z", "end": "2026-10-06T08:00:00Z"}])
            self.assertIn("No change", so.getvalue())
            self.assertNotIn("Private", first)

    def test_fetch_failure_never_prints_the_secret_address(self):
        secret = "https://calendar.proton.me/api/calendar/v1/url/SECRETTOKEN/calendar.ics?CacheKey=xyz"
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "busy.json"
            with mock.patch.dict(os.environ, {"BUSY_ICS_URL": secret}), \
                    mock.patch.object(busy, "fetch", side_effect=OSError(f"failed {secret}")), \
                    contextlib.redirect_stderr(io.StringIO()) as se, contextlib.redirect_stdout(io.StringIO()) as so:
                self.assertEqual(busy.main(["--out", str(out)]), 1)
            self.assertNotIn("SECRETTOKEN", se.getvalue() + so.getvalue())
            self.assertFalse(out.exists())

    def test_a_page_that_is_not_a_calendar_leaves_the_file_alone(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "busy.json"
            out.write_text("[]\n")
            with mock.patch.dict(os.environ, {"BUSY_ICS_URL": "https://x.example/c.ics"}), \
                    mock.patch.object(busy, "fetch", return_value="<html>login</html>"), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(busy.main(["--out", str(out)]), 1)
            self.assertEqual(out.read_text(), "[]\n")

    def test_non_https_address_is_refused(self):
        with self.assertRaises(ValueError):
            busy.fetch("http://insecure.example/c.ics")

    def test_webcal_is_upgraded_to_https(self):
        with mock.patch.object(busy.urllib.request, "urlopen") as op:
            op.return_value.__enter__.return_value.read.return_value = b"BEGIN:VCALENDAR\nEND:VCALENDAR"
            busy.fetch("webcal://calendar.example/c.ics")
            self.assertTrue(op.call_args[0][0].full_url.startswith("https://"))


if __name__ == "__main__":
    unittest.main()
