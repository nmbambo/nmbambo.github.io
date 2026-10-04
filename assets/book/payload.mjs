// The request payload: JSON, base64url, version 1. It travels only in a URL fragment (#...),
// which browsers never send to a server, so it never reaches a log. Pure and DOM-free.
import { isValidTimeZone } from './tz.mjs';
import { isSource } from '../source.mjs';

export const VERSION = 1;
export const MAX_ENCODED = 6000;
export const PLATFORMS = ['proton-meet', 'signal'];
export const REASON_MIN = 30;
export const REASON_MAX = 800;

export class PayloadError extends Error {
  constructor(message) { super(message); this.name = 'PayloadError'; }
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// Deliberately narrow: no ? & % # , ; so an address can never smuggle a mailto parameter.
const EMAIL = /^[A-Za-z0-9._+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$/;

export const isEmail = (s) => typeof s === 'string' && s.length <= 120 && EMAIL.test(s);
export const isUuid = (s) => typeof s === 'string' && UUID.test(s);
export const isHttpsUrl = (s) => {
  if (typeof s !== 'string' || s.length > 600 || /[\s<>"]/.test(s)) return false;
  try { return new URL(s).protocol === 'https:'; } catch { return false; }
};

function b64urlEncode(text) {
  const bytes = new TextEncoder().encode(text);
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function b64urlDecode(str) {
  const pad = str.length % 4 === 0 ? '' : '='.repeat(4 - (str.length % 4));
  const bin = atob(str.replace(/-/g, '+').replace(/_/g, '/') + pad);
  const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
}

/** encode(obj) -> base64url(JSON), with v:1 first. */
export function encode(obj) {
  if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) throw new TypeError('encode: expected an object');
  if (obj.v !== undefined && obj.v !== VERSION) throw new PayloadError(`Unsupported version ${obj.v}`);
  return b64urlEncode(JSON.stringify({ v: VERSION, ...obj }));
}

function str(obj, key, { min = 0, max = 200, multiline = false } = {}) {
  const v = obj[key];
  if (typeof v !== 'string') throw new PayloadError(`${key} must be text`);
  const bad = multiline ? /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/ : /[\u0000-\u001f\u007f]/;
  if (bad.test(v)) throw new PayloadError(`${key} has control characters`);
  if (v.length < min || v.length > max) throw new PayloadError(`${key} must be ${min}-${max} characters`);
  return v;
}

function isoDate(v, key) {
  if (typeof v !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?Z$/.test(v) || !Number.isFinite(Date.parse(v))) {
    throw new PayloadError(`${key} must be an ISO UTC time`);
  }
  return v;
}

function slot(v, key) {
  if (v === null || v === undefined) return null;
  if (typeof v !== 'object' || Array.isArray(v)) throw new PayloadError(`${key} must be a slot`);
  const start = isoDate(v.start, `${key}.start`); const end = isoDate(v.end, `${key}.end`);
  if (Date.parse(end) <= Date.parse(start)) throw new PayloadError(`${key} ends before it starts`);
  return { start, end };
}

function confirmed(v) {
  if (typeof v !== 'object' || v === null || Array.isArray(v)) throw new PayloadError('confirmed must be an object');
  const s = slot(v, 'confirmed');
  if (!s) throw new PayloadError('confirmed needs a time');
  if (!PLATFORMS.includes(v.platform)) throw new PayloadError('confirmed.platform is not recognised');
  const link = v.link === undefined || v.link === '' ? '' : v.link;
  if (link !== '' && !isHttpsUrl(link)) throw new PayloadError('confirmed.link must be an https link');
  const signalNumber = v.signalNumber === true;
  if (!link && !(v.platform === 'signal' && signalNumber)) throw new PayloadError('confirmed needs a link');
  if (!isUuid(v.uid)) throw new PayloadError('confirmed.uid must be a uuid');
  return { ...s, platform: v.platform, link, signalNumber, uid: v.uid };
}

/** Check the shape of a request (and an optional confirmation). Returns a clean copy; throws PayloadError. */
export function validate(obj) {
  if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) throw new PayloadError('Not a request');
  if (obj.v !== VERSION) throw new PayloadError(`Unsupported version ${obj.v}`);
  if (!isUuid(obj.id)) throw new PayloadError('id must be a uuid');
  const createdAt = isoDate(obj.createdAt, 'createdAt');
  const name = str(obj, 'name', { min: 1, max: 120 });
  const email = obj.email;
  if (!isEmail(email)) throw new PayloadError('email is not valid');
  const org = str(obj, 'org', { max: 120 });
  const reason = str(obj, 'reason', { min: REASON_MIN, max: REASON_MAX, multiline: true });
  if (!Number.isInteger(obj.duration) || obj.duration < 15 || obj.duration > 240) throw new PayloadError('duration must be 15-240 minutes');
  if (!PLATFORMS.includes(obj.platform)) throw new PayloadError('platform is not recognised');
  const preferred = slot(obj.preferred, 'preferred');
  const alternative = slot(obj.alternative, 'alternative');
  const suggestion = str(obj, 'suggestion', { max: 400, multiline: true });
  if (!preferred && !suggestion.trim()) throw new PayloadError('a request needs a preferred time or a suggestion');
  if (alternative && !preferred) throw new PayloadError('an alternative needs a preferred time');
  if (!isValidTimeZone(obj.viewerTz)) throw new PayloadError('viewerTz is not a time zone');
  const out = { v: VERSION, id: obj.id, createdAt, name, email, org, reason, duration: obj.duration,
    platform: obj.platform, preferred, alternative, suggestion, viewerTz: obj.viewerTz };
  if (obj.source !== undefined && obj.source !== '') {
    if (!isSource(obj.source)) throw new PayloadError('source is not recognised');
    out.source = obj.source;
  }
  if (obj.confirmed !== undefined) out.confirmed = confirmed(obj.confirmed);
  if (obj.view !== undefined) {
    if (obj.view !== 'attendee') throw new PayloadError('view is not recognised');
    if (!out.confirmed) throw new PayloadError('the attendee view needs a confirmation');
    out.view = 'attendee';
  }
  return out;
}

/** decode(str) -> validated request object; throws PayloadError on anything malformed. */
export function decode(text) {
  if (typeof text !== 'string' || !text) throw new PayloadError('Nothing to read');
  if (text.length > MAX_ENCODED) throw new PayloadError('Too long');
  if (!/^[A-Za-z0-9_-]+$/.test(text)) throw new PayloadError('Not base64url');
  let json;
  try { json = b64urlDecode(text); } catch { throw new PayloadError('Not readable text'); }
  let obj;
  try { obj = JSON.parse(json); } catch { throw new PayloadError('Not JSON'); }
  return validate(obj);
}

/**
 * Read a location.hash: '#<payload>' optionally followed by '&view=attendee'.
 * The payload's own `view` field is authoritative; the suffix is accepted for the attendee link.
 */
export function parseFragment(hash) {
  const raw = String(hash || '').replace(/^#/, '');
  const [body, ...rest] = raw.split('&');
  const req = decode(body);
  if (!req.view && rest.includes('view=attendee') && req.confirmed) req.view = 'attendee';
  return req;
}
