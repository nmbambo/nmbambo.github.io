import test from 'node:test';
import assert from 'node:assert/strict';
import { search, snippet } from '../../assets/search/search.mjs';
import { SearchAgent, seededRng } from '../../assets/search/agent.mjs';
import { SearchIndex } from '../../assets/search/index.mjs';
import { FIXTURE_DOCS } from './fixtures.mjs';

const ix = new SearchIndex(FIXTURE_DOCS);
const agent = (seed = 3, state = {}) => new SearchAgent({ rng: seededRng(seed), state });

test('search returns the contract shape', () => {
  const r = search('decision warranty', ix, agent());
  for (const k of ['results', 'algorithm', 'proposed', 'explanation', 'error']) assert.ok(k in r, k);
  assert.equal(r.algorithm, 'bm25');
  assert.equal(r.proposed, 'bm25');
  assert.equal(r.error, null);
  assert.ok(r.explanation.length > 10);
  assert.equal(r.results[0].id, 'architecture#e10');
  assert.ok(r.results[0].title && r.results[0].snippet.includes('{{'));
  assert.equal(r.total, r.results.length);
});

test('the agent proposal drives the algorithm; the person can override and the proposal is preserved', () => {
  const p = search('warrnaty', ix, agent());
  assert.equal(p.algorithm, 'fuzzy');
  assert.ok(p.results.some((x) => x.id === 'architecture#e10'));
  const o = search('warrnaty', ix, agent(), { algorithm: 'bm25' });
  assert.equal(o.algorithm, 'bm25');
  assert.equal(o.proposed, 'fuzzy');
  assert.equal(o.results.length, 0);
  assert.match(o.explanation, /as you chose/i);
  assert.equal(o.error, null);
});

test('override wins even for boolean syntax; non-boolean override skips parsing', () => {
  const r = search('decision AND (warranty OR canvas)', ix, agent(), { algorithm: 'phrase' });
  assert.equal(r.algorithm, 'phrase');
  assert.equal(r.proposed, 'boolean');
  const q = search('"human in the loop', ix, agent(), { algorithm: 'bm25' });
  assert.equal(q.error, null);
  assert.ok(q.results.length > 0);
});

test('unknown override name is ignored with a friendly error', () => {
  const r = search('spine', ix, agent(), { algorithm: 'telepathy' });
  assert.equal(r.algorithm, r.proposed);
  assert.match(r.error, /telepathy/);
  assert.ok(r.results.length > 0);
});

test('malformed boolean input: friendly error and bm25 fallback over the bare terms', () => {
  for (const q of ['"human in the loop', '(decision OR warranty', 'decision OR warranty)', 'decision AND', 'title:']) {
    const r = search(q, ix, agent());
    assert.equal(typeof r.error, 'string', q);
    assert.ok(r.error.length > 10, q);
    assert.doesNotMatch(r.error, /ParseError|at .*\.mjs|undefined/, q);
    assert.equal(r.algorithm, 'bm25', q);
    assert.match(r.explanation, /BM25/, q);
  }
  const r = search('"human in the loop', ix, agent());
  assert.ok(r.results.some((x) => x.id === 'method#e5'), 'fallback still finds results from the bare terms');
  const forced = search('(decision OR warranty', ix, agent(), { algorithm: 'boolean' });
  assert.equal(forced.algorithm, 'bm25');
  assert.ok(forced.error);
  assert.equal(search('(decision OR warranty', ix, agent()).proposed, 'boolean');
});

test('well-formed boolean queries have no error', () => {
  for (const q of ['decision AND (warranty OR canvas)', '"human in the loop"', 'title:spine', 'solver -quantum', 'NOT pricing', '+moloto framework', 'warrant*', 'tag:instrument OR type:note']) {
    const r = search(q, ix, agent());
    assert.equal(r.error, null, q);
    assert.ok(r.results.length > 0, q);
  }
  const r = search('title:spine', ix, agent());
  assert.deepEqual(r.results.map((x) => x.id).sort(), ['architecture#e1', 'architecture#e2']);
});

test('empty and whitespace queries return nothing, quietly', () => {
  for (const q of ['', '   ', null, undefined, 42]) {
    const r = search(q, ix, agent());
    assert.deepEqual(r.results, []);
    assert.equal(r.error, null);
    assert.ok(r.explanation);
  }
});

test('search never throws, whatever it is fed (fuzz)', () => {
  const alphabet = ['(', ')', '"', '*', '-', '+', '!', '&', '|', ':', ' ', 'AND', 'OR', 'NOT', 'title', 'tag:', 'a', 'decision', 'é', '\u0000', '{{', '\\', '\n'];
  const rng = seededRng(2024);
  for (let i = 0; i < 1500; i++) {
    let q = '';
    const n = 1 + Math.floor(rng() * 12);
    for (let j = 0; j < n; j++) q += alphabet[Math.floor(rng() * alphabet.length)] + (rng() < 0.4 ? ' ' : '');
    const r = search(q, ix, agent(i + 1));
    assert.ok(Array.isArray(r.results), JSON.stringify(q));
    assert.ok(r.error === null || typeof r.error === 'string');
    for (const a of ['boolean', 'bm25', 'fuzzy', 'prefix', 'phrase']) {
      const o = search(q, ix, agent(i + 1), { algorithm: a });
      assert.ok(Array.isArray(o.results), `${a} ${JSON.stringify(q)}`);
    }
  }
});

test('limit caps results but total reports every match', () => {
  const r = search('NOT pricing', ix, agent(), { limit: 3 });
  assert.equal(r.results.length, 3);
  assert.equal(r.total, FIXTURE_DOCS.length - 1);
});

test('matched terms are real vocabulary terms and drive highlighting', () => {
  const r = search('warrnaty', ix, agent());
  const top = r.results.find((x) => x.id === 'architecture#e10');
  assert.ok(top.matched.includes('warranty'));
  assert.ok(top.matched.every((m) => ix.hasTerm(m)));
  assert.match(top.snippet, /\{\{warranty\}\}/);
});

test('snippet: centred on the first match, markers around matches, ellipses when trimmed', () => {
  const doc = { text: 'x '.repeat(200) + 'The warranty promises process fidelity, not good outcomes. ' + 'y '.repeat(200) };
  const s = snippet(doc, ['warranty']);
  assert.ok(s.length <= 220 + 20, `len ${s.length}`);
  assert.ok(s.startsWith('…') && s.endsWith('…'));
  assert.ok(s.includes('{{warranty}}'));
  const mid = s.indexOf('{{');
  assert.ok(mid > 60 && mid < 160, `match should sit near the middle, at ${mid}`);
});

test('snippet: stems match inflected words; all matches in the window are wrapped', () => {
  const s = snippet({ text: 'Decisions are made. A decision is checked. Deciding is different.' }, ['decision']);
  assert.equal(s, '{{Decisions}} are made. A {{decision}} is checked. Deciding is different.');
});

test('snippet: no match, empty text, tiny maxLen, and braces in the source are safe', () => {
  const long = 'alpha beta gamma delta '.repeat(30);
  const none = snippet({ text: long }, ['zzz'], 100);
  assert.ok(none.length <= 101 && !none.includes('{{') && none.endsWith('…') && !none.startsWith('…'));
  assert.equal(snippet({ text: '' }, ['a']), '');
  assert.equal(snippet({}, ['a']), '');
  assert.equal(snippet(null, null), '');
  assert.ok(snippet({ text: long }, ['gamma'], 1).length > 0);
  assert.equal(snippet({ text: 'use {{braces}} here' }, ['braces']), 'use  {{braces}}  here'.replace(/ {2}/g, ' '));
});

test('snippet: hyphenated compounds and diacritics highlight the original text', () => {
  const s = snippet({ text: 'The human-in-the-loop boundary and a Café menu.' }, ['human-in-the-loop', 'cafe']);
  assert.equal(s, 'The {{human-in-the-loop}} boundary and a {{Café}} menu.');
});

test('snippet: matches only in the title leave the excerpt unmarked', () => {
  assert.doesNotMatch(snippet({ title: 'Spine', text: 'nothing relevant here' }, ['spine']), /\{\{/);
});

test('modules do not touch window/document at import time', async () => {
  assert.equal(typeof globalThis.window, 'undefined');
  assert.equal(typeof globalThis.document, 'undefined');
});
