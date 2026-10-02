import test from 'node:test';
import assert from 'node:assert/strict';
import { escapeText, foldLine, formatUtc, requestHold, invite, publish, deterministicUuid, PRODID } from '../../assets/book/ics.mjs';
import { makeRequest, confirmed } from './fixtures.mjs';

const enc = new TextEncoder();
const NOW = new Date('2026-10-02T08:30:15.123Z');
const unfold = (s) => s.replace(/\r\n[ \t]/g, '');
const lines = (ics) => unfold(ics).split('\r\n').filter(Boolean);

const meeting = () => ({
  uid: confirmed.uid, start: confirmed.start, end: confirmed.end,
  summary: 'Conversation: Nkosinathi Mbambo and Thandi Nkosi',
  reason: 'Why, we keep reversing; a decision.\nSecond line \\ done',
  platformInstructions: 'Proton Meet is end-to-end encrypted.', link: confirmed.link,
  organizer: { name: 'Nkosinathi Mbambo', email: 'nmbambo@ingqiqo-executables.run' },
  attendee: { name: 'Thandi Nkosi', email: 'thandi@example.co.za' },
});

test('TEXT escaping: backslash, semicolon, comma, newline', () => {
  assert.equal(escapeText('a\\b;c,d\ne\r\nf'), 'a\\\\b\;c\\,d\\ne\\nf');
  assert.equal(escapeText(undefined), '');
});

test('folding: no physical line exceeds 75 octets, and unfolding restores the text', () => {
  const long = `DESCRIPTION:${'word '.repeat(80)}`;
  const folded = foldLine(long);
  for (const l of folded.split('\r\n')) assert.ok(enc.encode(l).length <= 75, `${enc.encode(l).length} octets`);
  assert.ok(folded.split('\r\n').slice(1).every((l) => l.startsWith(' ')));
  assert.equal(unfold(folded), long);
  assert.equal(foldLine('SHORT:line'), 'SHORT:line');
});

test('folding never splits a multi-byte character', () => {
  const text = `SUMMARY:${'é'.repeat(60)}${'😀'.repeat(20)}${'ŋ'.repeat(30)}`;
  const folded = foldLine(text);
  for (const l of folded.split('\r\n')) assert.ok(enc.encode(l).length <= 75);
  assert.equal(unfold(folded), text);
  assert.ok(!folded.includes('�'));
  const exact = `X:${'a'.repeat(73)}`; // exactly 75 octets
  assert.equal(foldLine(exact), exact);
  assert.equal(foldLine(`${exact}a`).split('\r\n').length, 2);
});

test('UTC stamps are YYYYMMDDTHHMMSSZ', () => {
  assert.equal(formatUtc(NOW), '20261002T083015Z');
  assert.equal(formatUtc('2026-10-05T07:00:00.000Z'), '20261005T070000Z');
  assert.throws(() => formatUtc('nope'), RangeError);
});

test('every line ends CRLF and there is no bare LF or CR', () => {
  for (const ics of [requestHold(makeRequest(), { now: NOW }), invite(meeting(), { now: NOW }), publish(meeting(), { now: NOW })]) {
    assert.ok(ics.endsWith('\r\n'));
    assert.ok(!/(^|[^\r])\n/.test(ics), 'bare LF found');
    assert.ok(!/\r(?!\n)/.test(ics), 'bare CR found');
  }
});

test('request hold: tentative, UTC times, uid from the request id, no method', () => {
  const ics = requestHold(makeRequest(), { now: NOW });
  const l = lines(ics);
  assert.equal(l[0], 'BEGIN:VCALENDAR');
  assert.equal(l.at(-1), 'END:VCALENDAR');
  assert.ok(l.includes('VERSION:2.0'));
  assert.ok(l.includes(`PRODID:${PRODID}`));
  assert.equal(PRODID, '-//Ingqiqo Executables//Book//EN');
  assert.ok(!l.some((x) => x.startsWith('METHOD:')));
  assert.ok(l.includes('STATUS:TENTATIVE'));
  assert.ok(l.includes('DTSTAMP:20261002T083015Z'));
  assert.ok(l.includes('DTSTART:20261006T070000Z'));
  assert.ok(l.includes('DTEND:20261006T073000Z'));
  assert.ok(l.includes('UID:3f2b8c1e-5d4a-4e6f-9a7b-1c2d3e4f5a6b@nmbambo.github.io'));
  assert.ok(l.some((x) => x.startsWith('SUMMARY:Tentative')));
  assert.throws(() => requestHold(makeRequest({ preferred: null })), TypeError);
});

test('invite: METHOD:REQUEST, organiser, attendee with RSVP, link, alarm, sequence 0', () => {
  const l = lines(invite(meeting(), { now: NOW }));
  assert.ok(l.includes('METHOD:REQUEST'));
  assert.ok(l.includes('SEQUENCE:0'));
  assert.ok(l.includes('STATUS:CONFIRMED'));
  assert.ok(l.includes(`UID:${confirmed.uid}@nmbambo.github.io`));
  assert.ok(l.includes('ORGANIZER;CN=Nkosinathi Mbambo:mailto:nmbambo@ingqiqo-executables.run'));
  assert.ok(l.includes('ATTENDEE;CN=Thandi Nkosi;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:thandi@example.co.za'));
  assert.ok(l.includes('LOCATION:https://meet.proton.me/join/abc123'));
  assert.ok(l.includes('URL:https://meet.proton.me/join/abc123'));
  assert.ok(l.includes('BEGIN:VALARM') && l.includes('TRIGGER:-PT15M') && l.includes('ACTION:DISPLAY') && l.includes('END:VALARM'));
  const desc = l.find((x) => x.startsWith('DESCRIPTION:Why'));
  assert.ok(desc.includes('Why\\, we keep reversing\; a decision.\\nSecond line \\\\ done'));
  assert.ok(desc.includes('Proton Meet is end-to-end encrypted.'));
});

test('invite: commas in a name are quoted in CN; quotes are stripped', () => {
  const m = meeting();
  m.attendee = { name: 'Nkosi, "T" Thandi', email: 'thandi@example.co.za' };
  const l = lines(invite(m, { now: NOW }));
  assert.ok(l.some((x) => x.startsWith('ATTENDEE;CN="Nkosi, T Thandi";')));
});

test('invite: required properties are all present', () => {
  const l = lines(invite(meeting(), { now: NOW }));
  for (const prop of ['UID', 'DTSTAMP', 'DTSTART', 'DTEND', 'SUMMARY', 'ORGANIZER', 'ATTENDEE']) {
    assert.ok(l.some((x) => x.startsWith(`${prop}:`) || x.startsWith(`${prop};`)), `${prop} missing`);
  }
  assert.equal(l.filter((x) => x === 'BEGIN:VEVENT').length, 1);
  assert.equal(l.filter((x) => x === 'END:VEVENT').length, 1);
});

test('invite refuses an incomplete meeting', () => {
  const m = meeting(); delete m.attendee;
  assert.throws(() => invite(m), TypeError);
  assert.throws(() => invite({ ...meeting(), organizer: {} }), TypeError);
});

test('publish (attendee copy): METHOD:PUBLISH and no ATTENDEE line', () => {
  const ics = publish(meeting(), { now: NOW });
  const l = lines(ics);
  assert.ok(l.includes('METHOD:PUBLISH'));
  assert.ok(!l.some((x) => x.startsWith('ATTENDEE')));
  assert.ok(!l.includes('METHOD:REQUEST'));
  assert.ok(l.some((x) => x.startsWith('ORGANIZER')));
});

test('signal at my number: location falls back to the stated text', () => {
  const m = { ...meeting(), link: '', location: 'Signal call to +27 81 321 3766' };
  const l = lines(invite(m, { now: NOW }));
  assert.ok(l.includes('LOCATION:Signal call to +27 81 321 3766'));
  assert.ok(!l.some((x) => x.startsWith('URL:')));
});

test('uids: uuid-shaped, stable per seed, different across seeds', () => {
  const a = deterministicUuid('x:meeting');
  assert.match(a, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.equal(a, deterministicUuid('x:meeting'));
  assert.notEqual(a, deterministicUuid('y:meeting'));
  assert.notEqual(a, deterministicUuid('x:meeting '));
});
