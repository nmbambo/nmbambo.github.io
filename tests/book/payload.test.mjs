import test from 'node:test';
import assert from 'node:assert/strict';
import { encode, decode, validate, parseFragment, PayloadError, MAX_ENCODED, isEmail, isHttpsUrl } from '../../assets/book/payload.mjs';
import { makeRequest, confirmed } from './fixtures.mjs';

test('round trip, including accents and emoji', () => {
  const req = makeRequest({ name: 'Zodwa Nkosî', org: 'Ñandú & Sons', reason: 'Ukuthi sizoxoxa ngesinqumo — “is it right?” 😀 We need to talk about the board decision.' });
  assert.deepEqual(decode(encode(req)), req);
});

test('encode adds v:1 and is base64url with no padding or unsafe characters', () => {
  const { v, ...rest } = makeRequest();
  const s = encode(rest);
  assert.match(s, /^[A-Za-z0-9_-]+$/);
  assert.equal(JSON.parse(Buffer.from(s, 'base64url').toString()).v, 1);
  assert.equal(Object.keys(JSON.parse(Buffer.from(s, 'base64url').toString()))[0], 'v');
  assert.throws(() => encode({ ...rest, v: 2 }), PayloadError);
  assert.throws(() => encode(null), TypeError);
  assert.throws(() => encode([1]), TypeError);
});

test('a request with only a suggestion and no slots is valid', () => {
  const req = makeRequest({ preferred: null, alternative: null, suggestion: 'Thursday mornings after 8' });
  assert.deepEqual(decode(encode(req)), req);
});

test('rejects malformed input', () => {
  const bad = (s) => assert.throws(() => decode(s), PayloadError, String(s).slice(0, 40));
  bad('');
  bad(undefined);
  bad(null);
  bad(42);
  bad('not base64!');
  bad('abc$def');
  bad('a'.repeat(MAX_ENCODED + 1));
  bad(Buffer.from('not json').toString('base64url'));
  bad(Buffer.from('[1,2,3]').toString('base64url'));
  bad(Buffer.from('null').toString('base64url'));
  bad(Buffer.from('"string"').toString('base64url'));
  bad(Buffer.from([0xff, 0xfe, 0xfd]).toString('base64url'));
  bad('A');
});

test('rejects wrong versions and missing or bad fields', () => {
  const enc = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
  const good = makeRequest();
  const bad = (o, why) => assert.throws(() => decode(enc(o)), PayloadError, why);
  bad({ ...good, v: 2 }, 'version');
  bad({ ...good, v: undefined }, 'no version');
  bad({ ...good, id: 'abc' }, 'id');
  bad({ ...good, createdAt: 'yesterday' }, 'createdAt');
  bad({ ...good, name: '' }, 'name');
  bad({ ...good, name: 'x'.repeat(121) }, 'name length');
  bad({ ...good, name: 'a\nb' }, 'control chars in name');
  bad({ ...good, email: 'nope' }, 'email');
  bad({ ...good, email: 'a@b.co?bcc=evil@x.com' }, 'mailto smuggling');
  bad({ ...good, email: 'a@b.co,c@d.co' }, 'two addresses');
  bad({ ...good, org: 5 }, 'org type');
  bad({ ...good, reason: 'too short' }, 'reason min');
  bad({ ...good, reason: 'x'.repeat(801) }, 'reason max');
  bad({ ...good, duration: 0 }, 'duration');
  bad({ ...good, duration: 30.5 }, 'duration fraction');
  bad({ ...good, duration: '30' }, 'duration type');
  bad({ ...good, platform: 'zoom' }, 'platform');
  bad({ ...good, preferred: { start: 'x', end: 'y' } }, 'slot');
  bad({ ...good, preferred: { start: good.preferred.end, end: good.preferred.start } }, 'backwards slot');
  bad({ ...good, preferred: null, alternative: good.alternative, suggestion: '' }, 'no time at all');
  bad({ ...good, preferred: null, alternative: good.alternative, suggestion: 'any time' }, 'alternative without preferred');
  bad({ ...good, viewerTz: 'Mars/Olympus' }, 'tz');
  bad({ ...good, suggestion: 5 }, 'suggestion type');
  bad({ ...good, view: 'organiser' }, 'view value');
  bad({ ...good, view: 'attendee' }, 'attendee without a confirmation');
});

test('decode returns a clean copy: unknown keys and prototype tricks are dropped', () => {
  const enc = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
  const out = decode(enc({ ...makeRequest(), extra: '<script>', __proto__: { x: 1 } }));
  assert.ok(!('extra' in out));
  assert.equal(Object.getPrototypeOf(out), Object.prototype);
});

test('confirmation: accepted when sound, rejected when not', () => {
  const ok = makeRequest({ confirmed, view: 'attendee' });
  assert.deepEqual(decode(encode(ok)).confirmed, confirmed);
  const bad = (c) => assert.throws(() => validate({ ...ok, confirmed: c }), PayloadError);
  bad({ ...confirmed, link: 'javascript:alert(1)' });
  bad({ ...confirmed, link: 'http://insecure.example' });
  bad({ ...confirmed, link: 'https://x.example/a b' });
  bad({ ...confirmed, link: '' });
  bad({ ...confirmed, platform: 'zoom' });
  bad({ ...confirmed, uid: 'nope' });
  bad({ ...confirmed, start: 'x' });
  bad('string');
  const signalNumber = { ...confirmed, platform: 'signal', link: '', signalNumber: true };
  assert.equal(validate({ ...ok, confirmed: signalNumber }).confirmed.signalNumber, true);
});

test('parseFragment reads #payload and an &view=attendee suffix', () => {
  const org = makeRequest();
  assert.equal(parseFragment(`#${encode(org)}`).view, undefined);
  const withConf = makeRequest({ confirmed });
  assert.equal(parseFragment(`#${encode(withConf)}&view=attendee`).view, 'attendee');
  assert.equal(parseFragment(`#${encode(makeRequest({ confirmed, view: 'attendee' }))}`).view, 'attendee');
  assert.equal(parseFragment(`#${encode(org)}&view=attendee`).view, undefined, 'no confirmation, so it is not an attendee view');
  assert.throws(() => parseFragment(''), PayloadError);
  assert.throws(() => parseFragment('#'), PayloadError);
});

test('field helpers', () => {
  assert.ok(isEmail('nkosi+book@sub.example.co.za'));
  assert.ok(!isEmail('a@b') && !isEmail('a b@c.de') && !isEmail('@c.de'));
  assert.ok(isHttpsUrl('https://meet.proton.me/join/x#frag'));
  assert.ok(!isHttpsUrl('ftp://x.y') && !isHttpsUrl('https://') && !isHttpsUrl('not a url'));
});
