import test from 'node:test';
import assert from 'node:assert/strict';
import { performance } from 'node:perf_hooks';
import { existsSync, readFileSync } from 'node:fs';
import { SearchIndex } from '../../assets/search/index.mjs';
import { search } from '../../assets/search/search.mjs';
import { SearchAgent, seededRng } from '../../assets/search/agent.mjs';
import { ALGORITHMS } from '../../assets/search/algorithms.mjs';

// Synthetic corpus: ~600 section-sized documents (90-200 words, mean ~145; the real site corpus averages ~125) with a Zipf-like vocabulary,
// some diacritics and hyphenation so the slow tokenizer path is exercised.
function synthetic(n = 600) {
  const rng = seededRng(12345);
  const stemsOf = ['decision', 'warranty', 'coherence', 'evidence', 'identity', 'meaning', 'spine', 'plane', 'ledger', 'instrument', 'canvas', 'solver', 'quantum', 'governance', 'method', 'cycle', 'boundary', 'human-in-the-loop', 'café'];
  const syll = ['ba', 'ko', 'ri', 'sa', 'te', 'mu', 'lo', 'ne', 'di', 'fa', 'zu', 'pe', 'ha', 'vi', 'ro'];
  const vocab = [...stemsOf];
  while (vocab.length < 3000) vocab.push(Array.from({ length: 2 + Math.floor(rng() * 3) }, () => syll[Math.floor(rng() * syll.length)]).join('') + (rng() < 0.3 ? 's' : ''));
  const word = () => vocab[Math.min(vocab.length - 1, Math.floor(Math.pow(rng(), 2.2) * vocab.length))];
  const docs = [];
  for (let i = 0; i < n; i++) {
    const text = Array.from({ length: 90 + Math.floor(rng() * 110) }, (_, k) => word() + (k % 17 === 16 ? '. ' : ' ')).join('').replace(/\s+/g, ' ');
    docs.push({ id: `syn#${i}`, url: `/p${i}/`, title: `${word()} ${word()} ${word()}`, section: `${word()} ${word()}`, type: ['page', 'work', 'note'][i % 3], source: 'synthetic', tags: [word(), word()], text });
  }
  return docs;
}

const median = (xs) => [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)];

test('benchmark: ~600 section-sized documents index in < 300 ms and queries answer in < 30 ms', () => {
  const docs = synthetic(600);
  const words = docs.reduce((s, d) => s + d.text.split(' ').length, 0);
  const t0 = performance.now();
  const ix = new SearchIndex();
  for (const d of docs) ix.add(d);
  const indexMs = performance.now() - t0;
  assert.equal(ix.size, 600);

  const agent = new SearchAgent({ rng: seededRng(1) });
  const queries = [
    'decision warranty', 'coherence', 'evidence identity meaning', '"human in the loop"', 'decision AND (warranty OR canvas)',
    'title:spine OR tag:instrument', 'solver -quantum', 'warrant*', 'warrnaty', 'cohrence', 'arch', 'deci', 'NOT decision', 'the of and',
  ];
  const timings = [];
  let worst = 0;
  for (const q of queries) {
    search(q, ix, agent); // warm-up
    const runs = [];
    for (let i = 0; i < 5; i++) { const s = performance.now(); search(q, ix, agent); runs.push(performance.now() - s); }
    timings.push([q, median(runs)]);
    worst = Math.max(worst, ...runs);
  }
  const perAlgo = {};
  for (const a of ALGORITHMS) {
    const runs = [];
    for (const q of ['decision warranty', 'coherence evidence', 'decision AND warranty']) {
      for (let i = 0; i < 3; i++) { const s = performance.now(); search(q, ix, agent, { algorithm: a.name }); runs.push(performance.now() - s); }
    }
    perAlgo[a.name] = median(runs);
    worst = Math.max(worst, ...runs);
  }
  console.log(`# bench: ${docs.length} docs, ${words} words, ${ix.stats().terms} terms; index ${indexMs.toFixed(1)} ms; ` +
    `query median ${median(timings.map((t) => t[1])).toFixed(2)} ms, worst ${worst.toFixed(2)} ms; per algorithm median ` +
    Object.entries(perAlgo).map(([k, v]) => `${k} ${v.toFixed(2)}`).join(', '));
  assert.ok(indexMs < 300, `index took ${indexMs.toFixed(1)} ms`);
  assert.ok(worst < 30, `slowest query took ${worst.toFixed(1)} ms`);
});

test('incremental updates stay cheap (add + remove one doc)', () => {
  const docs = synthetic(600);
  const ix = new SearchIndex(docs);
  const s = performance.now();
  for (let i = 0; i < 20; i++) { ix.remove(`syn#${i}`); ix.add(docs[i]); }
  const per = (performance.now() - s) / 20;
  console.log(`# bench: incremental remove+add ${per.toFixed(2)} ms per document`);
  assert.ok(per < 20, `${per} ms per replace`);
  assert.equal(ix.size, 600);
});

test('real corpus smoke test (data/corpus.json, when present)', { skip: !existsSync(new URL('../../data/corpus.json', import.meta.url)) }, () => {
  const docs = JSON.parse(readFileSync(new URL('../../data/corpus.json', import.meta.url), 'utf8'));
  const t0 = performance.now();
  const ix = new SearchIndex(docs);
  const ms = performance.now() - t0;
  const agent = new SearchAgent({ rng: seededRng(1) });
  const r = search('decision warranty', ix, agent);
  console.log(`# bench: real corpus ${docs.length} docs indexed in ${ms.toFixed(1)} ms`);
  assert.ok(ix.size > 0 && ms < 300);
  assert.ok(r.results.length > 0);
  assert.equal(search('warrnaty', ix, agent).algorithm, 'fuzzy');
});
