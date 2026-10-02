// RFC 5545 builder for the three calendar files this feature makes. Pure and DOM-free.
export const PRODID = '-//Ingqiqo Executables//Book//EN';
export const UID_DOMAIN = 'nmbambo.github.io';

const utf8 = new TextEncoder();

/** TEXT escaping: backslash, semicolon, comma, newline. */
export function escapeText(s) {
  return String(s ?? '').replace(/\r\n|\r|\n/g, '\n').replace(/\\/g, '\\\\').replace(/;/g, '\;').replace(/,/g, '\\,').replace(/\n/g, '\\n');
}

/** Fold a content line at 75 octets, never splitting a UTF-8 character. Continuations start with one space. */
export function foldLine(line) {
  if (utf8.encode(line).length <= 75) return line;
  const out = [];
  let cur = '';
  let used = 0;
  let limit = 75;
  for (const ch of line) {
    const n = utf8.encode(ch).length;
    if (used + n > limit) { out.push(cur); cur = ''; used = 0; limit = 74; }
    cur += ch; used += n;
  }
  out.push(cur);
  return out.join('\r\n ');
}

/** 'YYYYMMDDTHHMMSSZ' */
export function formatUtc(date) {
  const d = date instanceof Date ? date : new Date(date);
  if (!Number.isFinite(d.getTime())) throw new RangeError('formatUtc: invalid date');
  return d.toISOString().replace(/[-:]/g, '').replace(/\.\d{3}/, '');
}

function param(v) {
  const s = String(v).replace(/["\r\n]/g, '');
  return /[,;:]/.test(s) ? `"${s}"` : s;
}

/** Deterministic uuid-shaped id from a seed (not secret, just stable): lets one request map to one calendar event. */
export function deterministicUuid(seed) {
  let h = [0x9e3779b9, 0x85ebca6b, 0xc2b2ae35, 0x27d4eb2f];
  const bytes = utf8.encode(String(seed));
  for (let i = 0; i < bytes.length; i++) {
    const k = i % 4;
    h[k] = Math.imul(h[k] ^ bytes[i], 0x01000193) >>> 0;
    h[(k + 1) % 4] = (h[(k + 1) % 4] + Math.imul(h[k], 0x85ebca6b) + i) >>> 0;
  }
  for (let r = 0; r < 4; r++) for (let k = 0; k < 4; k++) {
    h[k] = Math.imul(h[k] ^ (h[(k + 1) % 4] >>> 15), 0x2c1b3c6d) >>> 0;
    h[k] = (h[k] ^ (h[(k + 3) % 4] >>> 13)) >>> 0;
  }
  const hex = h.map((x) => x.toString(16).padStart(8, '0')).join('');
  const variant = ((parseInt(hex[16], 16) & 0x3) | 0x8).toString(16);
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-${variant}${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

function assemble(lines) {
  return lines.map(foldLine).join('\r\n') + '\r\n';
}

function calendar(method, vevent) {
  return assemble([
    'BEGIN:VCALENDAR', 'VERSION:2.0', `PRODID:${PRODID}`, 'CALSCALE:GREGORIAN',
    ...(method ? [`METHOD:${method}`] : []),
    ...vevent,
    'END:VCALENDAR',
  ]);
}

function need(cond, msg) { if (!cond) throw new TypeError(msg); }

/** The requester's own tentative hold. Request-shaped input: { id, name, reason, preferred: {start,end} }. */
export function requestHold(req, { now = new Date() } = {}) {
  need(req && req.id && req.preferred && req.preferred.start && req.preferred.end, 'requestHold: needs id and a preferred slot');
  return calendar(null, [
    'BEGIN:VEVENT',
    `UID:${req.id}@${UID_DOMAIN}`,
    `DTSTAMP:${formatUtc(now)}`,
    `DTSTART:${formatUtc(req.preferred.start)}`,
    `DTEND:${formatUtc(req.preferred.end)}`,
    'SEQUENCE:0',
    `SUMMARY:${escapeText('Tentative: conversation with Nkosinathi Mbambo')}`,
    `DESCRIPTION:${escapeText(`This is a request, not a booking. You will hear back with a confirmed time and a Proton Meet or Signal link.\n\nYour reason: ${req.reason || ''}`)}`,
    'STATUS:TENTATIVE',
    'TRANSP:OPAQUE',
    'END:VEVENT',
  ]);
}

function meetingEvent(meeting, now, { withAttendee }) {
  need(meeting && meeting.uid && meeting.start && meeting.end, 'meeting needs uid, start and end');
  need(meeting.organizer && meeting.organizer.email, 'meeting needs an organizer email');
  if (withAttendee) need(meeting.attendee && meeting.attendee.email, 'meeting needs an attendee email');
  const link = meeting.link || '';
  const description = meeting.description
    ?? [meeting.reason, meeting.platformInstructions].filter(Boolean).join('\n\n');
  const lines = [
    'BEGIN:VEVENT',
    `UID:${meeting.uid}@${UID_DOMAIN}`,
    `DTSTAMP:${formatUtc(now)}`,
    `DTSTART:${formatUtc(meeting.start)}`,
    `DTEND:${formatUtc(meeting.end)}`,
    'SEQUENCE:0',
    `SUMMARY:${escapeText(meeting.summary || 'Conversation with Nkosinathi Mbambo')}`,
    `DESCRIPTION:${escapeText(description)}`,
  ];
  if (meeting.location || link) lines.push(`LOCATION:${escapeText(meeting.location || link)}`);
  if (link) lines.push(`URL:${link}`);
  lines.push(`ORGANIZER;CN=${param(meeting.organizer.name || 'Nkosinathi Mbambo')}:mailto:${meeting.organizer.email}`);
  if (withAttendee) {
    lines.push(`ATTENDEE;CN=${param(meeting.attendee.name || meeting.attendee.email)};ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:${meeting.attendee.email}`);
  }
  lines.push('STATUS:CONFIRMED', 'TRANSP:OPAQUE',
    'BEGIN:VALARM', 'TRIGGER:-PT15M', 'ACTION:DISPLAY', 'DESCRIPTION:Reminder', 'END:VALARM',
    'END:VEVENT');
  return lines;
}

/** The organiser's invitation (METHOD:REQUEST) for Proton Calendar to send. */
export function invite(meeting, { now = new Date() } = {}) {
  return calendar('REQUEST', meetingEvent(meeting, now, { withAttendee: true }));
}

/** The attendee's own copy (METHOD:PUBLISH, which RFC 5546 says carries no ATTENDEE). */
export function publish(meeting, { now = new Date() } = {}) {
  return calendar('PUBLISH', meetingEvent(meeting, now, { withAttendee: false }));
}
