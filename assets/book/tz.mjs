// Timezone helpers built on Intl, so they stay correct for zones with daylight saving.
// Pure: nothing here touches window or document.

const dtfCache = new Map();
function dtf(tz) {
  let f = dtfCache.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat('en-US', {
      timeZone: tz, hourCycle: 'h23',
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
    dtfCache.set(tz, f);
  }
  return f;
}

export function isValidTimeZone(tz) {
  if (typeof tz !== 'string' || !tz) return false;
  try { dtf(tz); return true; } catch { return false; }
}

/** Wall-clock parts of an instant in a zone. */
export function tzParts(date, tz) {
  const out = {};
  for (const p of dtf(tz).formatToParts(date)) if (p.type !== 'literal') out[p.type] = Number(p.value);
  return { year: out.year, month: out.month, day: out.day, hour: out.hour % 24, minute: out.minute, second: out.second };
}

/** Offset of the zone from UTC at an instant, in minutes (east positive). */
export function tzOffsetMinutes(date, tz) {
  const p = tzParts(date, tz);
  const wall = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second);
  return Math.round((wall - Math.floor(date.getTime() / 1000) * 1000) / 60000);
}

/** The UTC instant at which the wall clock in `tz` reads the given time. */
export function zonedToUtc({ year, month, day, hour = 0, minute = 0 }, tz) {
  const guess = Date.UTC(year, month - 1, day, hour, minute);
  const off1 = tzOffsetMinutes(new Date(guess), tz);
  let t = guess - off1 * 60000;
  const off2 = tzOffsetMinutes(new Date(t), tz);
  if (off2 !== off1) t = guess - off2 * 60000;
  return new Date(t);
}

const two = (n) => String(n).padStart(2, '0');

/** 'YYYY-MM-DD' of an instant in a zone. */
export function ymd(date, tz) {
  const p = tzParts(date, tz);
  return `${p.year}-${two(p.month)}-${two(p.day)}`;
}

/** Parse 'YYYY-MM-DD' to {year, month, day}; throws on anything else. */
export function parseYmd(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s));
  if (!m) throw new RangeError(`Expected YYYY-MM-DD, got ${JSON.stringify(s)}`);
  const [year, month, day] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const d = new Date(Date.UTC(year, month - 1, day));
  if (d.getUTCFullYear() !== year || d.getUTCMonth() !== month - 1 || d.getUTCDate() !== day) {
    throw new RangeError(`Not a real date: ${s}`);
  }
  return { year, month, day };
}

/** Value for <input type="datetime-local"> showing the instant in a zone. */
export function toLocalInput(date, tz) {
  const p = tzParts(date, tz);
  return `${p.year}-${two(p.month)}-${two(p.day)}T${two(p.hour)}:${two(p.minute)}`;
}

/** Instant for a datetime-local value read in a zone; null when it cannot be read. */
export function fromLocalInput(value, tz) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(String(value || ''));
  if (!m) return null;
  try {
    parseYmd(`${m[1]}-${m[2]}-${m[3]}`);
  } catch { return null; }
  const hour = Number(m[4]); const minute = Number(m[5]);
  if (hour > 23 || minute > 59) return null;
  return zonedToUtc({ year: Number(m[1]), month: Number(m[2]), day: Number(m[3]), hour, minute }, tz);
}

const ABBR = { 'Africa/Johannesburg': 'SAST', UTC: 'UTC', 'Etc/UTC': 'UTC' };
/** Short zone label: SAST for Johannesburg, otherwise what Intl offers (e.g. GMT+1). */
export function tzLabel(date, tz) {
  if (ABBR[tz]) return ABBR[tz];
  const part = new Intl.DateTimeFormat('en-GB', { timeZone: tz, timeZoneName: 'short' })
    .formatToParts(date).find((p) => p.type === 'timeZoneName');
  return part ? part.value : tz;
}

export function formatTime(date, tz) {
  const p = tzParts(date, tz);
  return `${two(p.hour)}:${two(p.minute)}`;
}

/** 'Tuesday 6 October 2026' */
export function formatDay(date, tz, { weekday = 'long', year = true } = {}) {
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: tz, weekday, day: 'numeric', month: 'long', ...(year ? { year: 'numeric' } : {}),
  }).format(date).replace(/,/g, '');
}

/** True when two zones show the same wall clock at this instant. */
export function sameWallClock(date, tzA, tzB) {
  return tzOffsetMinutes(date, tzA) === tzOffsetMinutes(date, tzB);
}
