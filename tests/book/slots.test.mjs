import test from 'node:test';
import assert from 'node:assert/strict';
import { computeSlots, groupByDay, blackoutRanges } from '../../assets/book/slots.mjs';
import { tzParts, zonedToUtc, tzOffsetMinutes, fromLocalInput, toLocalInput, tzLabel } from '../../assets/book/tz.mjs';
import { availability, MON, sast } from './fixtures.mjs';

const SAST = 'Africa/Johannesburg';
const hhmm = (d, tz = SAST) => { const p = tzParts(d, tz); return `${String(p.hour).padStart(2, '0')}:${String(p.minute).padStart(2, '0')}`; };
const dow = (d, tz = SAST) => new Date(Date.UTC(tzParts(d, tz).year, tzParts(d, tz).month - 1, tzParts(d, tz).day)).getUTCDay();
const slots = (over = {}, extra = {}) => computeSlots({ availability: { ...availability, ...over }, now: MON, duration: 30, ...extra });

test('weekends are empty and Friday closes at 13:00', () => {
  const all = slots();
  assert.ok(all.length > 100);
  assert.ok(all.every((s) => dow(s.start) >= 1 && dow(s.start) <= 5));
  const fri = all.filter((s) => dow(s.start) === 5);
  assert.ok(fri.length > 0);
  assert.ok(fri.every((s) => hhmm(s.end) <= '13:00' && hhmm(s.start) >= '09:00'));
  assert.equal(hhmm(fri.at(-1).start), '12:30');
});

test('minimum notice: nothing starts within 24 hours of now', () => {
  const all = slots();
  const earliest = MON.getTime() + 24 * 3600e3;
  assert.ok(all.every((s) => s.start.getTime() >= earliest));
  // Monday 07:00 SAST + 24h = Tuesday 07:00, so the first slot is Tuesday 09:00 SAST.
  assert.equal(all[0].start.toISOString(), sast('2026-10-06T09:00:00').toISOString());
});

test('notice is exact: a slot starting exactly at now + notice is allowed', () => {
  const now = sast('2026-10-05T09:00:00');
  const s = computeSlots({ availability: { ...availability, minNoticeHours: 24 }, now, duration: 30 });
  assert.equal(s[0].start.toISOString(), sast('2026-10-06T09:00:00').toISOString());
});

test('horizon: no slot starts later than horizonDays from now', () => {
  const all = slots();
  const latest = MON.getTime() + 21 * 86400e3;
  assert.ok(all.every((s) => s.start.getTime() <= latest));
  assert.ok(all.at(-1).start.getTime() > latest - 4 * 86400e3);
  const short = slots({ horizonDays: 7 });
  assert.ok(short.every((s) => s.start.getTime() <= MON.getTime() + 7 * 86400e3));
});

test('step: starts fall on the step grid from opening time', () => {
  assert.ok(slots().every((s) => ['00', '15', '30', '45'].includes(hhmm(s.start).slice(3))));
  const s30 = slots({ slotStep: 30 });
  assert.ok(s30.every((s) => ['00', '30'].includes(hhmm(s.start).slice(3))));
  const hourly = slots({ slotStep: 60 }).filter((s) => s.start >= sast('2026-10-06T00:00:00') && s.start < sast('2026-10-07T00:00:00'));
  assert.deepEqual(hourly.map((s) => hhmm(s.start)), ['09:00', '10:00', '11:00', '12:00', '13:00', '14:00', '15:00']);
});

test('durations: the whole call fits inside the window', () => {
  const sixty = slots({}, { duration: 60 });
  assert.ok(sixty.every((s) => s.end - s.start === 3600e3));
  const tue = sixty.filter((s) => dow(s.start) === 2);
  assert.equal(hhmm(tue.at(-1).start), '15:00');
  const fri = sixty.filter((s) => dow(s.start) === 5);
  assert.equal(hhmm(fri.at(-1).start), '12:00');
  const f45 = slots({}, { duration: 45 }).filter((s) => dow(s.start) === 2);
  assert.equal(hhmm(f45.at(-1).start), '15:15');
});

test('a duration that is not offered is refused', () => {
  assert.throws(() => slots({}, { duration: 20 }), RangeError);
  assert.throws(() => slots({}, { duration: -5 }), RangeError);
});

test('defaults to defaultDuration', () => {
  const s = computeSlots({ availability, now: MON });
  assert.equal(s[0].end - s[0].start, 30 * 60e3);
});

test('busy time: removes overlapping slots and keeps a buffer either side', () => {
  const busy = [{ start: sast('2026-10-06T10:00:00').toISOString(), end: sast('2026-10-06T11:00:00').toISOString() }];
  const tue = slots({}, { busy }).filter((s) => hhmm(s.start) && s.start >= sast('2026-10-06T00:00:00') && s.start < sast('2026-10-07T00:00:00')).map((s) => hhmm(s.start));
  assert.ok(tue.includes('09:00') && tue.includes('09:15'), 'ends at 09:45, exactly on the buffer edge');
  assert.ok(!tue.includes('09:30'), '09:30-10:00 is inside the 15 minute buffer');
  assert.ok(!tue.includes('10:00') && !tue.includes('10:30') && !tue.includes('11:00'));
  assert.ok(tue.includes('11:15'), 'starts exactly when the buffer ends');
});

test('buffer is configurable and zero means touching is fine', () => {
  const busy = [{ start: sast('2026-10-06T10:00:00'), end: sast('2026-10-06T11:00:00') }];
  const tue = slots({ bufferMinutes: 0 }, { busy }).filter((s) => s.start >= sast('2026-10-06T00:00:00') && s.start < sast('2026-10-07T00:00:00')).map((s) => hhmm(s.start));
  assert.ok(tue.includes('09:30') && tue.includes('11:00'));
  assert.ok(!tue.includes('09:45') && !tue.includes('10:45'));
});

test('malformed busy entries are ignored rather than breaking the page', () => {
  const s = slots({}, { busy: [null, {}, { start: 'x', end: 'y' }, { start: '2026-10-06T10:00:00Z', end: '2026-10-06T09:00:00Z' }] });
  assert.equal(s.length, slots().length);
});

test('blackout: a date, a date range and an instant range', () => {
  const day = slots({ blackout: ['2026-10-07'] });
  assert.ok(!day.some((s) => s.start >= sast('2026-10-07T00:00:00') && s.start < sast('2026-10-08T00:00:00')));
  assert.ok(day.some((s) => s.start >= sast('2026-10-08T00:00:00')));
  const range = slots({ blackout: [{ from: '2026-10-07', to: '2026-10-09' }] });
  assert.ok(!range.some((s) => s.start >= sast('2026-10-07T00:00:00') && s.start < sast('2026-10-10T00:00:00')));
  const inst = slots({ blackout: [{ start: sast('2026-10-06T09:00:00').toISOString(), end: sast('2026-10-06T12:00:00').toISOString() }] });
  const tue = inst.filter((s) => s.start >= sast('2026-10-06T00:00:00') && s.start < sast('2026-10-07T00:00:00'));
  assert.equal(hhmm(tue[0].start), '12:00');
});

test('a malformed blackout throws instead of silently opening a day', () => {
  assert.throws(() => slots({ blackout: ['next Tuesday'] }), RangeError);
  assert.throws(() => slots({ blackout: [{ from: '2026-10-09', to: '2026-10-07' }] }), RangeError);
  assert.throws(() => slots({ blackout: [42] }), RangeError);
  assert.deepEqual(blackoutRanges([], SAST), []);
});

test('time zones: wall-clock hours hold across a daylight saving change', () => {
  const london = { timezone: 'Europe/London', weekly: Object.fromEntries(['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'].map((d) => [d, [['09:00', '10:00']]])),
    durations: [30], defaultDuration: 30, slotStep: 30, minNoticeHours: 0, horizonDays: 14, bufferMinutes: 0, blackout: [] };
  const all = computeSlots({ availability: london, now: new Date('2026-10-20T00:00:00Z'), duration: 30 });
  const nineAm = all.filter((s) => hhmm(s.start, 'Europe/London') === '09:00');
  const before = nineAm.find((s) => s.start.toISOString().startsWith('2026-10-24'));
  const after = nineAm.find((s) => s.start.toISOString().startsWith('2026-10-26'));
  assert.equal(before.start.toISOString(), '2026-10-24T08:00:00.000Z', 'BST is UTC+1');
  assert.equal(after.start.toISOString(), '2026-10-26T09:00:00.000Z', 'GMT is UTC+0');
});

test('tz helpers: zonedToUtc and offsets', () => {
  assert.equal(zonedToUtc({ year: 2026, month: 10, day: 5, hour: 9 }, SAST).toISOString(), '2026-10-05T07:00:00.000Z');
  assert.equal(tzOffsetMinutes(new Date('2026-07-01T12:00:00Z'), 'America/New_York'), -240);
  assert.equal(tzOffsetMinutes(new Date('2026-01-01T12:00:00Z'), 'America/New_York'), -300);
  assert.equal(tzLabel(new Date(), SAST), 'SAST');
  assert.equal(toLocalInput(new Date('2026-10-05T07:00:00Z'), SAST), '2026-10-05T09:00');
  assert.equal(fromLocalInput('2026-10-05T09:00', SAST).toISOString(), '2026-10-05T07:00:00.000Z');
  assert.equal(fromLocalInput('', SAST), null);
  assert.equal(fromLocalInput('2026-02-31T09:00', SAST), null);
});

test('groupByDay reads days in the viewer zone', () => {
  const early = { ...availability, weekly: { tue: [['06:00', '07:00']] }, minNoticeHours: 0, horizonDays: 10 };
  const s = computeSlots({ availability: early, now: MON, duration: 30 });
  const sa = groupByDay(s, SAST);
  assert.equal(sa[0].date, '2026-10-06');
  const la = groupByDay(s, 'America/Los_Angeles');
  assert.equal(la[0].date, '2026-10-05', '06:00 SAST on Tuesday is Monday evening in Los Angeles');
  assert.equal(la.reduce((n, d) => n + d.slots.length, 0), s.length);
  assert.deepEqual(groupByDay([], SAST), []);
  assert.equal(groupByDay(s, 'Not/AZone')[0].date, '2026-10-06', 'an unknown zone falls back to UTC');
});

test('slots come back sorted, as UTC Dates', () => {
  const all = slots();
  assert.ok(all.every((s) => s.start instanceof Date && s.end instanceof Date));
  for (let i = 1; i < all.length; i++) assert.ok(all[i].start > all[i - 1].start);
});

test('an empty week gives no slots', () => {
  assert.deepEqual(slots({ weekly: {} }), []);
});
