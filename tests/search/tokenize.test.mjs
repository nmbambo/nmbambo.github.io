import test from 'node:test';
import assert from 'node:assert/strict';
import { tokenize, tokenizeParts, stem, scan, STOPWORDS, fold } from '../../assets/search/tokenize.mjs';

test('tokenize folds case and diacritics and splits on non-alphanumerics', () => {
  assert.deepEqual(tokenize('Café, Résumé!  Naïve’s test'), ['cafe', 'resume', 'naive', 's', 'test']);
  assert.deepEqual(tokenize(''), []);
  assert.deepEqual(tokenize(null), []);
});

test('hyphenated words keep their parts and the compound', () => {
  assert.deepEqual(tokenize('human-in-the-loop'), ['human', 'in', 'the', 'loop', 'human-in-the-loop']);
  assert.deepEqual(tokenizeParts('human-in-the-loop'), ['human', 'in', 'the', 'loop']);
});

test('compound shares the first part position so neighbours stay adjacent', () => {
  const items = scan('the human-in-the-loop boundary');
  const by = Object.fromEntries(items.map((i) => [i.term, i.pos]));
  assert.equal(by['human-in-the-loop'], by.human);
  assert.equal(by.boundary, by.loop + 1);
});

test('scan offsets point into the original string, even with diacritics', () => {
  const s = 'Résumé of Zoë';
  for (const it of scan(s)) {
    assert.equal(fold(s.slice(it.start, it.end)), it.term);
  }
});

test('stem: light suffix stripping, never below 3 characters', () => {
  assert.equal(stem('decisions'), 'decision');
  assert.equal(stem('warranties'), 'warranty');
  assert.equal(stem('warranty'), 'warranty');
  assert.equal(stem('boxes'), 'box');
  assert.equal(stem('classes'), 'class');
  assert.equal(stem('class'), 'class');
  assert.equal(stem('analysis'), 'analysis');
  assert.equal(stem('running'), 'run');
  assert.equal(stem('planned'), 'plan');
  assert.equal(stem('quickly'), 'quick');
  assert.equal(stem('applied'), 'apply');
  assert.equal(stem('sing'), 'sing');
  assert.equal(stem('bed'), 'bed');
  assert.equal(stem('string'), 'string');
  assert.equal(stem('sdfs'), 'sdf');
  for (const w of ['bus', 'is', 'ties', 'uses', 'owed', 'ring']) assert.ok(stem(w).length >= Math.min(3, w.length), w);
  assert.equal(stem('x1s'), 'x1s');
});

test('stem is stable under repeated queries and leaves compounds alone', () => {
  assert.equal(stem('human-in-the-loop'), 'human-in-the-loop');
  assert.equal(stem('Decisions'), stem('decisions'));
});

test('STOPWORDS contains the usual suspects', () => {
  for (const w of ['the', 'and', 'of', 'in']) assert.ok(STOPWORDS.has(w));
  assert.ok(!STOPWORDS.has('warranty'));
});
