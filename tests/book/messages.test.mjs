import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { requestMessage, confirmationMessage, proposeMessage, declineMessage, organiserLink, attendeeLink,
  describeSlot, platformInstructions, MAX_MAILTO, ORGANISER_EMAIL, CC_EMAIL } from '../../assets/book/messages.mjs';
import { decode, parseFragment } from '../../assets/book/payload.mjs';
import { makeRequest, confirmed } from './fixtures.mjs';

const body = (href) => decodeURIComponent(new URL(href).search.match(/body=([^&]*)/)[1]);
const param = (href, name) => decodeURIComponent(href.split('?')[1].split('&').find((p) => p.startsWith(`${name}=`)).slice(name.length + 1));

test('request mailto goes to the organiser, cc the personal address, and carries the organiser link', () => {
  const req = makeRequest();
  const m = requestMessage(req);
  assert.ok(m.href.startsWith(`mailto:${ORGANISER_EMAIL}?cc=${CC_EMAIL}&subject=`));
  assert.equal(ORGANISER_EMAIL, 'nmbambo@ingqiqo-executables.run');
  assert.equal(CC_EMAIL, 'nmbambo@gmail.com');
  const link = organiserLink(req);
  assert.ok(link.startsWith('https://nmbambo.github.io/book/confirm/#'));
  assert.ok(body(m.href).includes(link));
  assert.deepEqual(decode(link.split('#')[1]), req);
  assert.ok(m.href.length <= MAX_MAILTO);
  assert.equal(m.shortened, false);
  assert.ok(!m.href.includes('\n'));
  assert.ok(m.href.includes('%0D%0A'), 'RFC 6068 line breaks');
});

test('request body is a plain summary of what was asked', () => {
  const text = requestMessage(makeRequest()).text;
  for (const s of ['Thandi Nkosi', 'Acme Holdings', 'thandi@example.co.za', 'Proton Meet', '30 minutes', 'Preferred time:', 'Alternative time:', 'SAST', 'Europe/London']) assert.ok(text.includes(s), s);
});

test('a long reason is shortened in the email body but kept whole in the payload and on the clipboard', () => {
  const reason = ('We keep reversing a decision and I want to understand why. ').repeat(14).slice(0, 420).trim();
  const req = makeRequest({ reason, alternative: null, platform: 'signal' });
  const m = requestMessage(req);
  assert.ok(m.shortened);
  assert.ok(!m.tooLong);
  assert.ok(m.href.length <= MAX_MAILTO);
  assert.ok(body(m.href).includes('full text in the link below'));
  assert.ok(!body(m.href).includes(reason), 'the email body does not carry the whole reason');
  assert.ok(m.text.includes(reason), 'clipboard text keeps the whole reason');
  const link = body(m.href).match(/https:\/\/\S+/)[0];
  assert.equal(decode(link.split('#')[1]).reason, reason);
});

test('a reason that fits is not touched; one that nearly fits is trimmed to fit', () => {
  const req = makeRequest({ reason: 'x'.repeat(200), alternative: null });
  assert.equal(requestMessage(req).shortened, false);
  const mid = makeRequest({ reason: 'y '.repeat(150).trim(), alternative: null });
  const m = requestMessage(mid, { max: 1900 });
  if (m.shortened) assert.ok(m.href.length <= 1900 || m.tooLong);
});

test('when even the summary cannot fit, the lean email still carries the link', () => {
  const req = makeRequest({ reason: 'z'.repeat(800), alternative: null });
  const m = requestMessage(req, { max: 1900 });
  assert.ok(m.shortened);
  assert.ok(body(m.href).includes('https://nmbambo.github.io/book/confirm/#'));
  const tiny = requestMessage(req, { max: 500 });
  assert.equal(tiny.tooLong, true);
});

test('suggestion-only request', () => {
  const req = makeRequest({ preferred: null, alternative: null, suggestion: 'Thursday mornings' });
  const m = requestMessage(req);
  assert.ok(m.text.includes('Suggested time: Thursday mornings'));
  assert.ok(!m.text.includes('Preferred time:'));
});

test('special characters survive encoding', () => {
  const req = makeRequest({ name: 'Zodwa & Sons?', reason: 'Is 100% of it #1 priority? Yes & no; maybe, "quoted" — fine. ' + 'x'.repeat(30) });
  assert.ok(body(requestMessage(req).href).includes('Zodwa & Sons?'));
  assert.ok(body(requestMessage(req).href).includes('100% of it #1'));
});

test('slot description shows SAST and the viewer zone only when different', () => {
  const slot = makeRequest().preferred;
  const lon = describeSlot(slot, 'Europe/London');
  assert.match(lon, /^Tuesday 6 October 2026, 09:00 to 09:30 SAST \(Tue 6 October 08:00 to 08:30 BST, Europe\/London\)$/);
  assert.equal(describeSlot(slot, 'Africa/Johannesburg'), 'Tuesday 6 October 2026, 09:00 to 09:30 SAST');
  assert.equal(describeSlot(slot, 'Africa/Maputo'), 'Tuesday 6 October 2026, 09:00 to 09:30 SAST', 'same offset, nothing to add');
});

test('confirmation mail: to the requester, both zones, link, and an attendee link that decodes', () => {
  const req = makeRequest();
  const m = confirmationMessage(req, confirmed);
  assert.ok(m.href.startsWith('mailto:thandi@example.co.za?subject='));
  assert.match(param(m.href, 'subject'), /^Confirmed: Wed 7 October 10:00 SAST$/);
  const text = body(m.href);
  assert.ok(text.includes('Dear Thandi,'));
  assert.ok(text.includes('SAST') && text.includes('Europe/London'));
  assert.ok(text.includes(confirmed.link));
  assert.ok(text.includes(m.link));
  assert.ok(m.link.endsWith('&view=attendee'));
  const att = parseFragment(new URL(m.link).hash);
  assert.equal(att.view, 'attendee');
  assert.deepEqual(att.confirmed, confirmed);
  assert.equal(att.name, req.name);
});

test('attendee link trims a long reason but keeps it valid', () => {
  const req = makeRequest({ reason: 'r'.repeat(800) });
  const att = parseFragment(new URL(attendeeLink(req, confirmed)).hash);
  assert.ok(att.reason.length <= 140 && att.reason.length >= 30);
});

test('propose and decline mails are courteous and go to the requester', () => {
  const req = makeRequest();
  const p = proposeMessage(req, { start: '2026-10-08T07:00:00.000Z', end: '2026-10-08T07:30:00.000Z' });
  assert.ok(p.href.startsWith('mailto:thandi@example.co.za?'));
  assert.ok(body(p.href).includes('Thursday 8 October 2026, 09:00 to 09:30 SAST'));
  const d = declineMessage(req);
  assert.ok(d.href.startsWith('mailto:thandi@example.co.za?'));
  assert.ok(/Thank you/.test(body(d.href)) && /wish you well/.test(body(d.href)));
});

test('platform instructions', () => {
  assert.match(platformInstructions('proton-meet', { link: 'https://meet.proton.me/join/x' }), /end-to-end encrypted.*https:\/\/meet\.proton\.me/);
  assert.match(platformInstructions('signal', { link: 'https://signal.link/call/#key=x' }), /call link/);
  assert.match(platformInstructions('signal', { signalNumber: true }), /\+27 81 321 3766/);
});

test('only the two agreed contact addresses appear anywhere in the booking code or pages', () => {
  const root = new URL('../../', import.meta.url);
  const files = [
    ...readdirSync(new URL('assets/book/', root)).filter((f) => /\.(mjs|json)$/.test(f)).map((f) => `assets/book/${f}`),
    'book/index.html', 'book/confirm/index.html', 'scripts/busy.py', '.github/workflows/busy.yml',
  ];
  const allowed = new Set(['nmbambo@ingqiqo-executables.run', 'nmbambo@gmail.com']);
  for (const f of files) {
    const found = readFileSync(new URL(f, root), 'utf8').match(/[A-Za-z0-9._+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+/g) || [];
    for (const addr of found) assert.ok(allowed.has(addr), `${f} contains ${addr}`);
  }
});

test('the pure modules do not touch window or document', () => {
  const root = new URL('../../assets/book/', import.meta.url);
  for (const f of ['tz', 'slots', 'ics', 'payload', 'messages']) {
    const src = readFileSync(new URL(`${f}.mjs`, root), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
    assert.ok(!/\b(window|document|localStorage|navigator)\b/.test(src), `${f}.mjs references the DOM`);
  }
});
