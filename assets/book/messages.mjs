// Wording and links: the request email, the organiser's replies, and the two links that carry the payload.
// Pure and DOM-free. The voice is few words, warm, precise.
import { encode } from './payload.mjs';
import { formatDay, formatTime, tzLabel, sameWallClock } from './tz.mjs';

export const SITE = 'https://nmbambo.github.io';
export const ORGANISER_EMAIL = 'nmbambo@ingqiqo-executables.run';
export const CC_EMAIL = 'nmbambo@gmail.com';
export const SIGNAL_NUMBER = '+27 81 321 3766';
export const SAST = 'Africa/Johannesburg';
export const MAX_MAILTO = 1900;

export const PLATFORM_NAME = { 'proton-meet': 'Proton Meet', signal: 'Signal' };

const crlf = (s) => s.replace(/\r?\n/g, '\r\n');
const enc = encodeURIComponent;

export function mailto(to, { cc, subject, body }) {
  const q = [];
  if (cc) q.push(`cc=${enc(cc).replace(/%40/g, '@')}`);
  q.push(`subject=${enc(subject)}`, `body=${enc(crlf(body))}`);
  return `mailto:${to}?${q.join('&')}`;
}

const firstName = (name) => String(name).trim().split(/\s+/)[0] || 'there';

/** 'Tuesday 6 October 2026, 09:00 to 09:30 SAST (08:00 to 08:30 BST, Europe/London)' */
export function describeSlot(slot, viewerTz) {
  const s = new Date(slot.start); const e = new Date(slot.end);
  const sast = `${formatDay(s, SAST)}, ${formatTime(s, SAST)} to ${formatTime(e, SAST)} SAST`;
  if (!viewerTz || sameWallClock(s, SAST, viewerTz)) return sast;
  const day = formatDay(s, viewerTz, { weekday: 'short', year: false });
  return `${sast} (${day} ${formatTime(s, viewerTz)} to ${formatTime(e, viewerTz)} ${tzLabel(s, viewerTz)}, ${viewerTz})`;
}

export function organiserLink(req) {
  return `${SITE}/book/confirm/#${encode(req)}`;
}

/** Attendee link: the request plus the confirmation. The reason is trimmed; the attendee view does not need all of it. */
export function attendeeLink(req, confirmed) {
  const { confirmed: _c, view: _v, ...base } = req;
  const slim = { ...base, reason: base.reason.length > 140 ? `${base.reason.slice(0, 139)}…` : base.reason };
  return `${SITE}/book/confirm/#${encode({ ...slim, confirmed, view: 'attendee' })}&view=attendee`;
}

export function platformInstructions(platform, { link = '', signalNumber = false } = {}) {
  if (platform === 'proton-meet') {
    return `Proton Meet is end-to-end encrypted video in the browser. Join at the time shown: ${link}`;
  }
  if (signalNumber || !link) {
    return `I will call you on Signal at the time shown. If a call fails, my number is ${SIGNAL_NUMBER}.`;
  }
  return `Signal is an encrypted call. Join with this call link at the time shown: ${link}`;
}

function summaryLines(req, reason) {
  const lines = [
    `Name: ${req.name}`,
    ...(req.org ? [`Organisation: ${req.org}`] : []),
    `Email: ${req.email}`,
    `Reason: ${reason}`,
    `Length: ${req.duration} minutes`,
    `Platform: ${PLATFORM_NAME[req.platform]}`,
  ];
  if (req.preferred) lines.push(`Preferred time: ${describeSlot(req.preferred, req.viewerTz)}`);
  if (req.alternative) lines.push(`Alternative time: ${describeSlot(req.alternative, req.viewerTz)}`);
  if (req.suggestion) lines.push(`Suggested time: ${req.suggestion}`);
  return lines;
}

function requestBody(req, reason) {
  return [
    'Hello Nkosinathi,',
    '',
    'I would like to book a conversation.',
    '',
    ...summaryLines(req, reason),
    '',
    'For you, to confirm (nothing is booked until you do):',
    organiserLink(req),
    '',
    req.name,
  ].join('\n');
}

/**
 * The visitor's request. Returns { href, text, shortened, tooLong }.
 * text is the full message (for the clipboard); href keeps under MAX_MAILTO by shortening only the reason in the body.
 */
export function requestMessage(req, { max = MAX_MAILTO } = {}) {
  const subject = `Request for a conversation: ${req.name}${req.org ? `, ${req.org}` : ''}`;
  const text = requestBody(req, req.reason);
  const build = (reason) => mailto(ORGANISER_EMAIL, { cc: CC_EMAIL, subject, body: requestBody(req, reason) });
  let href = build(req.reason);
  let shortened = false;
  if (href.length > max) {
    shortened = true;
    const note = ' … (full text in the link below)';
    let lo = 0; let hi = req.reason.length;
    while (lo < hi) {
      const mid = Math.ceil((lo + hi) / 2);
      if (build(req.reason.slice(0, mid).trimEnd() + note).length <= max) lo = mid; else hi = mid - 1;
    }
    href = build(lo > 0 ? req.reason.slice(0, lo).trimEnd() + note : '(full text in the link below)');
    if (href.length > max) {
      // Even without the reason the summary will not fit: keep greeting, link and name; the link carries everything.
      const lean = ['Hello Nkosinathi,', '', 'I would like to book a conversation. The details are in this link, for you to confirm:',
        organiserLink(req), '', req.name].join('\n');
      href = mailto(ORGANISER_EMAIL, { cc: CC_EMAIL, subject, body: lean });
    }
  }
  return { href, text, shortened, tooLong: href.length > max };
}

/** Organiser to requester: confirmed. */
export function confirmationMessage(req, confirmed) {
  const slot = { start: confirmed.start, end: confirmed.end };
  const link = attendeeLink(req, confirmed);
  const first = new Date(confirmed.start);
  const subject = `Confirmed: ${formatDay(first, SAST, { weekday: 'short', year: false })} ${formatTime(first, SAST)} SAST`;
  const body = [
    `Dear ${firstName(req.name)},`,
    '',
    'Thank you for the request. I am glad to confirm our conversation.',
    '',
    `When: ${describeSlot(slot, req.viewerTz)}`,
    `Platform: ${PLATFORM_NAME[confirmed.platform]}`,
    platformInstructions(confirmed.platform, confirmed),
    '',
    'Your confirmation, with a button to add it to your calendar:',
    link,
    '',
    'If the time stops working, tell me and we will move it.',
    '',
    'Warm regards,',
    'Nkosinathi',
  ].join('\n');
  return { href: mailto(req.email, { subject, body }), link };
}

/** Organiser to requester: another time. */
export function proposeMessage(req, slot) {
  const subject = 'A different time for our conversation';
  const body = [
    `Dear ${firstName(req.name)},`,
    '',
    'Thank you for the request. The times you suggested do not work for me, and I would like to propose another:',
    '',
    describeSlot(slot, req.viewerTz),
    '',
    'Reply to confirm, or tell me what suits you better and I will find a way.',
    '',
    'Warm regards,',
    'Nkosinathi',
  ].join('\n');
  return { href: mailto(req.email, { subject, body }) };
}

/** Organiser to requester: a courteous no. */
export function declineMessage(req) {
  const subject = 'Your request for a conversation';
  const body = [
    `Dear ${firstName(req.name)},`,
    '',
    'Thank you for reaching out, and for the care you took in describing your decision.',
    'I am not able to take this conversation on at present, and I would rather tell you plainly than keep you waiting.',
    '',
    'I wish you well with it.',
    '',
    'Warm regards,',
    'Nkosinathi',
  ].join('\n');
  return { href: mailto(req.email, { subject, body }) };
}
