// Slot computation: turns weekly hours, busy periods and rules into bookable start times.
// Pure and DOM-free. Time zone arithmetic goes through Intl (see tz.mjs).
import { tzParts, zonedToUtc, parseYmd, ymd, isValidTimeZone } from './tz.mjs';

const DAY_KEYS = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'];
const MIN = 60000;

function hhmm(s) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(s));
  if (!m) throw new RangeError(`Expected HH:MM, got ${JSON.stringify(s)}`);
  const h = Number(m[1]); const mi = Number(m[2]);
  if (h > 24 || mi > 59 || (h === 24 && mi !== 0)) throw new RangeError(`Not a time of day: ${s}`);
  return h * 60 + mi;
}

function toMs(v) {
  const t = v instanceof Date ? v.getTime() : Date.parse(v);
  return Number.isFinite(t) ? t : NaN;
}

function dayRange(from, to, tz) {
  const a = parseYmd(from); const b = parseYmd(to);
  const start = zonedToUtc(a, tz).getTime();
  const next = new Date(Date.UTC(b.year, b.month - 1, b.day + 1));
  const end = zonedToUtc({ year: next.getUTCFullYear(), month: next.getUTCMonth() + 1, day: next.getUTCDate() }, tz).getTime();
  if (end <= start) throw new RangeError(`Blackout range ends before it starts: ${from} to ${to}`);
  return [start, end];
}

/**
 * Blackout entries: "YYYY-MM-DD" (whole day in the zone), {from, to} (inclusive dates),
 * or {start, end} (ISO instants). A malformed entry throws: a typo must not open a day.
 */
export function blackoutRanges(blackout, tz) {
  const out = [];
  for (const b of blackout || []) {
    if (typeof b === 'string') out.push(dayRange(b, b, tz));
    else if (b && b.from && b.to) out.push(dayRange(b.from, b.to, tz));
    else if (b && b.start && b.end) {
      const s = toMs(b.start); const e = toMs(b.end);
      if (!(e > s)) throw new RangeError(`Bad blackout period: ${JSON.stringify(b)}`);
      out.push([s, e]);
    } else throw new RangeError(`Unreadable blackout entry: ${JSON.stringify(b)}`);
  }
  return out;
}

/**
 * computeSlots({ availability, busy, now, duration }) -> [{ start: Date, end: Date }] (UTC instants)
 *  - weekly hours are read as wall-clock times in availability.timezone
 *  - a slot must start at least minNoticeHours from now and no later than horizonDays from now
 *  - a slot is dropped if it comes within bufferMinutes of any busy period, or touches a blackout
 *  - starts fall on slotStep minutes from the opening time; the whole call must fit in the window
 */
export function computeSlots({ availability, busy = [], now = new Date(), duration } = {}) {
  if (!availability || typeof availability !== 'object') throw new TypeError('computeSlots: availability is required');
  const tz = availability.timezone;
  if (!isValidTimeZone(tz)) throw new RangeError(`Unknown time zone: ${tz}`);
  const dur = duration ?? availability.defaultDuration;
  if (!Number.isInteger(dur) || dur <= 0) throw new RangeError('computeSlots: duration must be a positive whole number of minutes');
  if (Array.isArray(availability.durations) && availability.durations.length && !availability.durations.includes(dur)) {
    throw new RangeError(`Duration ${dur} is not offered`);
  }
  const step = availability.slotStep ?? 15;
  if (!Number.isInteger(step) || step <= 0) throw new RangeError('slotStep must be a positive whole number');
  const buffer = (availability.bufferMinutes ?? 0) * MIN;
  const nowMs = toMs(now);
  if (!Number.isFinite(nowMs)) throw new TypeError('computeSlots: now is not a valid date');
  const earliest = nowMs + (availability.minNoticeHours ?? 0) * 60 * MIN;
  const latest = nowMs + (availability.horizonDays ?? 21) * 24 * 60 * MIN;
  const weekly = availability.weekly || {};
  const windows = {};
  for (const k of DAY_KEYS) {
    windows[k] = (weekly[k] || []).map(([a, b]) => {
      const s = hhmm(a); const e = hhmm(b);
      if (e <= s) throw new RangeError(`Window ends before it starts: ${a}-${b}`);
      return [s, e];
    });
  }
  const blocked = blackoutRanges(availability.blackout, tz);
  const busyMs = [];
  for (const b of busy || []) {
    const s = toMs(b && b.start); const e = toMs(b && b.end);
    if (Number.isFinite(s) && Number.isFinite(e) && e > s) busyMs.push([s - buffer, e + buffer]);
  }

  const today = tzParts(new Date(nowMs), tz);
  const slots = [];
  const days = Math.ceil((availability.horizonDays ?? 21)) + 1;
  for (let i = 0; i <= days; i++) {
    const d = new Date(Date.UTC(today.year, today.month - 1, today.day + i));
    const parts = { year: d.getUTCFullYear(), month: d.getUTCMonth() + 1, day: d.getUTCDate() };
    for (const [open, close] of windows[DAY_KEYS[d.getUTCDay()]]) {
      for (let m = open; m + dur <= close; m += step) {
        const start = zonedToUtc({ ...parts, hour: Math.floor(m / 60), minute: m % 60 }, tz).getTime();
        const end = start + dur * MIN;
        if (start < earliest || start > latest) continue;
        if (blocked.some(([s, e]) => start < e && end > s)) continue;
        if (busyMs.some(([s, e]) => start < e && end > s)) continue;
        slots.push({ start: new Date(start), end: new Date(end) });
      }
    }
  }
  slots.sort((a, b) => a.start - b.start);
  return slots;
}

/** groupByDay(slots, viewerTz) -> [{ date: 'YYYY-MM-DD', slots }] with days read in the viewer's zone. */
export function groupByDay(slots, viewerTz = 'UTC') {
  const tz = isValidTimeZone(viewerTz) ? viewerTz : 'UTC';
  const days = new Map();
  for (const s of slots) {
    const key = ymd(s.start, tz);
    if (!days.has(key)) days.set(key, []);
    days.get(key).push(s);
  }
  return [...days.entries()].sort(([a], [b]) => (a < b ? -1 : 1)).map(([date, list]) => ({ date, slots: list }));
}
