import test from 'node:test';
import assert from 'node:assert/strict';
import { ALGORITHMS, algorithmByName, bm25, fuzzy, prefix, phrase, booleanAlgorithm, damerauLevenshtein, maxEdits, queryTerms } from '../../assets/search/algorithms.mjs';
import { ParseError } from '../../assets/search/boolean.mjs';
import { SearchIndex } from '../../assets/search/index.mjs';
import { FIXTURE_DOCS } from './fixtures.mjs';

const ix = new SearchIndex(FIXTURE_DOCS);
const ids = (r) => r.map((x) => x.id);

test('the five algorithms have the contract shape', () => {
  assert.deepEqual(ALGORITHMS.map((a) => a.name).sort(), ['bm25', 'boolean', 'fuzzy', 'phrase', 'prefix']);
  for (const a of ALGORITHMS) {
    assert.equal(typeof a.label, 'string');
    assert.equal(typeof a.run, 'function');
    const r = a.run('decision', ix);
    assert.ok(Array.isArray(r));
    for (const x of r) { assert.equal(typeof x.id, 'string'); assert.equal(typeof x.score, 'number'); assert.ok(Array.isArray(x.matched)); }
  }
  assert.equal(algorithmByName('fuzzy'), fuzzy);
  assert.equal(algorithmByName('nope'), null);
});

test('results are sorted by score descending and deterministic', () => {
  for (const a of ALGORITHMS) {
    const r = a.run('decision warranty', ix);
    for (let i = 1; i < r.length; i++) assert.ok(r[i - 1].score >= r[i].score);
    assert.deepEqual(r, a.run('decision warranty', ix));
  }
});

test('bm25: title and tag hits outrank body-only hits', () => {
  const r = bm25.run('warranty', ix);
  assert.equal(r[0].id, 'architecture#e10', 'title + section + tag + text');
  assert.ok(ids(r).includes('work#coverage'));
  assert.deepEqual(bm25.run('zzzznotaword', ix), []);
  assert.deepEqual(bm25.run('', ix), []);
});

test('bm25 stems: decisions finds decision', () => {
  assert.ok(ids(bm25.run('decisions', ix)).includes('work#canvas'));
  assert.deepEqual(bm25.run('decisions', ix).map((x) => x.id), bm25.run('decision', ix).map((x) => x.id));
});

test('bm25 matched lists real vocabulary terms present in the doc', () => {
  const r = bm25.run('warranty evidence zzzz', ix).find((x) => x.id === 'architecture#e10');
  assert.deepEqual(r.matched, ['evidence', 'warranty']);
  for (const x of bm25.run('decision warranty', ix)) for (const m of x.matched) assert.ok(ix.hasTerm(m));
});

test('queries made only of stopwords still search', () => {
  assert.ok(bm25.run('the of', ix).length > 0);
  assert.deepEqual(queryTerms('the human of loop').map((t) => t.raw), ['human', 'loop']);
});

test('fuzzy: misspellings find the right documents', () => {
  const w = fuzzy.run('warrnaty', ix);
  assert.ok(ids(w).includes('architecture#e10'));
  assert.ok(w.find((x) => x.id === 'architecture#e10').matched.includes('warranty'));
  const c = fuzzy.run('cohrence', ix);
  assert.ok(ids(c).includes('home#e1') || ids(c).includes('method#e8'));
  assert.ok(c.some((x) => x.matched.includes('coherence')));
  assert.ok(ids(fuzzy.run('architecure', ix)).includes('architecture#e2') || ids(fuzzy.run('architecure', ix)).includes('architecture#e1'));
});

test('fuzzy: exact words keep exact behaviour; short words are not fuzzed', () => {
  assert.deepEqual(ids(fuzzy.run('spine', ix)), ids(bm25.run('spine', ix)));
  assert.deepEqual(fuzzy.run('qzx', ix), []);
  assert.deepEqual(fuzzy.run('wxyzqrst', ix), []);
});

test('edit distance helpers', () => {
  assert.equal(damerauLevenshtein('warrnaty', 'warranty'), 1, 'transposition counts as one edit');
  assert.equal(damerauLevenshtein('cohrence', 'coherence'), 1);
  assert.equal(damerauLevenshtein('kitten', 'sitting'), 3);
  assert.equal(damerauLevenshtein('same', 'same'), 0);
  assert.ok(damerauLevenshtein('abcdefgh', 'zzzz', 2) > 2);
  assert.equal([maxEdits('abc'), maxEdits('abcd'), maxEdits('abcdefg'), maxEdits('abcdefgh')].join(), '0,1,1,2');
});

test('prefix: partial words match longer vocabulary terms', () => {
  assert.ok(ids(prefix.run('arch', ix)).includes('architecture#e1'));
  assert.ok(prefix.run('arch', ix).some((x) => x.matched.includes('architecture')));
  const d = prefix.run('deci', ix);
  assert.ok(d.length >= 8);
  assert.ok(d.some((x) => x.matched.includes('decision')));
  assert.deepEqual(prefix.run('qqqq', ix), []);
});

test('phrase: exact phrase outranks scattered words', () => {
  const r = phrase.run('human in the loop', ix);
  assert.deepEqual(ids(r).slice(0, 2).sort(), ['engage#e3', 'method#e5']);
  const scattered = phrase.run('decision warranty', ix);
  assert.equal(scattered[0].id, 'architecture#e10');
  // proximity: adjacent words beat the same words far apart
  const mini = new SearchIndex([
    { id: 'near', title: '', text: 'alpha beta gamma delta filler filler filler filler filler filler filler filler filler filler' },
    { id: 'far', title: '', text: 'alpha filler filler filler filler filler filler filler filler filler filler filler filler beta gamma delta' },
  ]);
  assert.equal(phrase.run('alpha beta', mini)[0].id, 'near');
  assert.equal(phrase.run('alpha beta', mini).length, 2, 'proximity weights, it does not filter');
});

test('phrase with one term equals bm25', () => {
  assert.deepEqual(phrase.run('warranty', ix), bm25.run('warranty', ix));
});

test('boolean: filters by AST and ranks by BM25 over positive terms', () => {
  const r = booleanAlgorithm.run('decision AND (warranty OR canvas)', ix);
  assert.deepEqual(ids(r).sort(), ['architecture#e10', 'work#canvas']);
  assert.ok(r[0].score >= r[1].score && r[0].score > 0);
  assert.ok(r[0].matched.includes('decision'));
  const not = booleanAlgorithm.run('NOT pricing', ix);
  assert.equal(not.length, FIXTURE_DOCS.length - 1);
  assert.ok(not.every((x) => x.score === 0 && x.matched.length === 0));
  const plus = booleanAlgorithm.run('+moloto framework', ix);
  assert.deepEqual(ids(plus), ['method#e3']);
  assert.ok(plus[0].matched.includes('moloto'));
  assert.deepEqual(booleanAlgorithm.run('title:spine', ix).map((x) => x.matched).flat().filter((m) => m !== 'spine'), []);
  assert.deepEqual(ids(booleanAlgorithm.run('warrant*', ix)).sort(), ['architecture#e10', 'work#coverage']);
});

test('boolean.run throws ParseError on bad syntax (search() is what catches it)', () => {
  assert.throws(() => booleanAlgorithm.run('(oops', ix), ParseError);
});
