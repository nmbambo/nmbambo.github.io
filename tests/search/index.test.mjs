import test from 'node:test';
import assert from 'node:assert/strict';
import { SearchIndex, FIELD_STRIDE } from '../../assets/search/index.mjs';
import { FIXTURE_DOCS } from './fixtures.mjs';

const build = () => { const ix = new SearchIndex(); for (const d of FIXTURE_DOCS) ix.add(d); return ix; };

test('add/get/docs/stats', () => {
  const ix = build();
  assert.equal(ix.size, FIXTURE_DOCS.length);
  assert.equal(ix.get('home#e1').title, 'Ingqiqo Executables');
  assert.equal(ix.docs().length, FIXTURE_DOCS.length);
  const s = ix.stats();
  assert.equal(s.docs, FIXTURE_DOCS.length);
  assert.ok(s.terms > 100);
  assert.ok(s.avgLength.text > 10);
});

test('invalid documents are ignored, not thrown', () => {
  const ix = new SearchIndex();
  assert.equal(ix.add(null), false);
  assert.equal(ix.add({ title: 'no id' }), false);
  assert.equal(ix.size, 0);
});

test('postings are field-aware', () => {
  const ix = build();
  assert.ok(ix.fieldPostings('spine', 'title').has('architecture#e1'));
  assert.ok(!ix.fieldPostings('spine', 'text').has('architecture#e1'));
  assert.ok(ix.fieldPostings('instrument', 'tags').has('work#canvas'));
  assert.ok(ix.fieldPostings('note', 'type').has('notes#hiring'));
  const merged = ix.postings('spine');
  assert.ok(merged.has('architecture#e1'));
  assert.ok(merged.get('architecture#e1').every((p) => Number.isInteger(p)));
  // title position 0..; text positions carry the text offset
  assert.ok(ix.postings('evidence').get('architecture#e1').some((p) => p >= 2 * FIELD_STRIDE));
});

test('vocabulary holds stems of scoring fields only; type/source are not in it', () => {
  const ix = build();
  assert.ok(ix.vocabulary().includes('warranty'));
  assert.ok(ix.vocabulary().includes('decision'));
  const t = new SearchIndex([{ id: 'x', title: 'a', text: 'b', type: 'zzztype', source: 'zzzsource' }]);
  assert.ok(!t.hasTerm('zzztype') && !t.hasTerm('zzzsource'), 'type/source values must not leak into the vocabulary');
  assert.ok(t.fieldPostings('zzztype', 'type').has('x'));
  assert.deepEqual(ix.vocabulary(), [...ix.vocabulary()].sort());
});

test('remove is incremental and fully clears postings and vocabulary', () => {
  const ix = build();
  assert.ok(ix.hasTerm('moloto'));
  assert.equal(ix.remove('method#e3'), true);
  assert.equal(ix.remove('method#e3'), false);
  assert.ok(!ix.hasTerm('moloto'));
  assert.equal(ix.postings('moloto').size, 0);
  assert.equal(ix.get('method#e3'), undefined);
  assert.equal(ix.size, FIXTURE_DOCS.length - 1);
});

test('re-adding a document replaces it (idempotent, update-friendly)', () => {
  const ix = build();
  const before = JSON.stringify(ix.stats().tokens);
  ix.add(FIXTURE_DOCS[0]);
  assert.equal(ix.size, FIXTURE_DOCS.length);
  assert.equal(JSON.stringify(ix.stats().tokens), before);
  ix.add({ ...FIXTURE_DOCS[0], text: 'completely different zebra text' });
  assert.ok(ix.hasTerm('zebra'));
  assert.ok(!ix.postings('organisation').has('home#e1'));
});

test('incremental index equals a rebuilt index', () => {
  const a = build();
  a.remove('home#e2'); a.remove('notes#time');
  a.add({ ...FIXTURE_DOCS[5], text: 'rewritten judgement text about solvers' });
  const docs = FIXTURE_DOCS.filter((d) => d.id !== 'home#e2' && d.id !== 'notes#time').map((d) => (d.id === FIXTURE_DOCS[5].id ? { ...d, text: 'rewritten judgement text about solvers' } : d));
  const b = new SearchIndex(docs);
  assert.deepEqual(a.vocabulary(), b.vocabulary());
  assert.deepEqual(a.stats().tokens, b.stats().tokens);
  for (const t of b.vocabulary()) assert.equal(a.docFreq(t), b.docFreq(t), t);
});

test('phraseMatches finds consecutive terms, including through stopwords and hyphenation', () => {
  const ix = build();
  const hits = ix.phraseMatches(['human', 'in', 'the', 'loop']);
  assert.ok(hits.has('method#e5'));
  assert.ok(hits.has('engage#e3'), 'hyphenated form must match the spaced phrase');
  assert.ok(!hits.has('home#e1'));
  assert.equal(ix.phraseMatches(['loop', 'human']).size, 0);
});

test('tags are separate pieces: a phrase cannot span two tags', () => {
  const ix = new SearchIndex([{ id: 't', title: '', text: '', tags: ['alpha', 'beta'] }]);
  assert.equal(ix.phraseMatches(['alpha', 'beta'], 'tags').size, 0);
});

test('prefixTerms', () => {
  const ix = build();
  assert.ok(ix.prefixTerms('warrant').includes('warranty'));
  assert.deepEqual(ix.prefixTerms('zzz'), []);
  assert.ok(ix.prefixTerms('no', 'type').includes('note'));
});
