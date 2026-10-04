import test from 'node:test';
import assert from 'node:assert/strict';
import { sourceFrom, isSource, capture, readSource, KEY } from '../../assets/source.mjs';
import { encode, decode, PayloadError } from '../../assets/book/payload.mjs';
import { requestMessage } from '../../assets/book/messages.mjs';
import { makeRequest } from './fixtures.mjs';

const memory = () => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)) }; };

test('sourceFrom reads utm tags in order and drops unsafe ones', () => {
  assert.equal(sourceFrom('?utm_source=linkedin&utm_campaign=signal&utm_content=2026-10-06-cost'), 'linkedin / signal / 2026-10-06-cost');
  assert.equal(sourceFrom('?utm_campaign=signal'), '');
  assert.equal(sourceFrom('?utm_source=linked%20in'), '');
  assert.equal(sourceFrom('?utm_source=youtube&utm_content=<script>'), 'youtube');
  assert.equal(sourceFrom(''), '');
});

test('capture keeps the first tagged arrival only; readSource never throws', () => {
  const s = memory();
  capture('?utm_source=linkedin&utm_campaign=signal', s);
  capture('?utm_source=youtube', s);
  assert.equal(readSource(s), 'linkedin / signal');
  const bad = memory(); bad.setItem(KEY, 'x@y.com');
  assert.equal(readSource(bad), '');
  assert.equal(readSource({ getItem: () => { throw new Error('blocked'); } }), '');
  assert.doesNotThrow(() => capture('?utm_source=a', { getItem: () => { throw new Error('blocked'); } }));
});

test('a request carries a safe source and the email names it', () => {
  const req = makeRequest({ source: 'linkedin / signal / 2026-10-06-cost' });
  assert.equal(decode(encode(req)).source, 'linkedin / signal / 2026-10-06-cost');
  assert.match(requestMessage(req).text, /Came from: linkedin \/ signal \/ 2026-10-06-cost/);
  assert.throws(() => decode(encode(makeRequest({ source: 'mailto:x?cc=y' }))), PayloadError);
  assert.equal(isSource('linkedin'), true);
  const plain = makeRequest();
  assert.equal('source' in decode(encode(plain)), false);
  assert.doesNotMatch(requestMessage(plain).text, /Came from/);
});
