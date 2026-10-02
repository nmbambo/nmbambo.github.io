import test from 'node:test';
import assert from 'node:assert/strict';
import { SearchAgent, seededRng, sampleBeta, sampleGamma, applyReward, rewardsFromEvent, foldEvents, PRIORS } from '../../assets/search/agent.mjs';
import { fuzzy } from '../../assets/search/algorithms.mjs';
import { SearchIndex } from '../../assets/search/index.mjs';
import { FIXTURE_DOCS } from './fixtures.mjs';

const ix = new SearchIndex(FIXTURE_DOCS);
const pick = (q, seed = 11, state = {}) => new SearchAgent({ rng: seededRng(seed), state }).choose(q, ix);
const share = (q, name, n = 200, state = {}) => {
  let hit = 0;
  for (let s = 1; s <= n; s++) if (pick(q, s, state).algorithm === name) hit++;
  return hit / n;
};

test('Beta sampler: correct mean and variance, always in (0,1)', () => {
  const rng = seededRng(42);
  for (const [a, b] of [[2, 5], [8, 1], [0.5, 0.5], [30, 30]]) {
    const xs = Array.from({ length: 20000 }, () => sampleBeta(a, b, rng));
    assert.ok(xs.every((x) => x >= 0 && x <= 1));
    const mean = xs.reduce((s, x) => s + x, 0) / xs.length;
    const v = xs.reduce((s, x) => s + (x - mean) ** 2, 0) / xs.length;
    assert.ok(Math.abs(mean - a / (a + b)) < 0.01, `mean a=${a} b=${b}: ${mean}`);
    const tv = (a * b) / ((a + b) ** 2 * (a + b + 1));
    assert.ok(Math.abs(v - tv) < 0.005, `var a=${a} b=${b}: ${v} vs ${tv}`);
  }
  assert.ok(sampleGamma(0.3, seededRng(1)) >= 0);
  assert.equal(sampleGamma(-1), 0);
});

test('seeded rng is deterministic; different seeds differ', () => {
  const a = seededRng(5), b = seededRng(5), c = seededRng(6);
  const sa = [a(), a(), a()], sb = [b(), b(), b()], sc = [c(), c(), c()];
  assert.deepEqual(sa, sb);
  assert.notDeepEqual(sa, sc);
  assert.ok(sa.every((x) => x >= 0 && x < 1));
  assert.deepEqual(pick('decision warranty', 3), pick('decision warranty', 3));
});

test('features: operators, quotes, fields, unknown terms, short partials, key', () => {
  const ag = new SearchAgent();
  const f = (q) => ag.features(q, ix);
  assert.equal(f('decision AND warranty').key, 'ops');
  assert.equal(f('decision AND warranty').hasOperators, true);
  assert.equal(f('title:spine').hasFields, true);
  assert.equal(f('title:spine').key, 'ops');
  assert.equal(f('solver -quantum').hasOperators, true);
  assert.equal(f('warrant*').key, 'ops');
  assert.equal(f('(a OR b)').key, 'ops');
  assert.equal(f('"human in the loop"').hasQuotes, true);
  assert.equal(f('"human in the loop"').key, 'quoted');
  assert.deepEqual(f('warrnaty').unknownTerms, ['warrnaty']);
  assert.equal(f('warrnaty').key, 'typo');
  assert.equal(f('arch').shortPartial, true);
  assert.equal(f('arch').key, 'short');
  assert.equal(f('deci').key, 'short');
  assert.equal(f('spine').key, 'plain');
  assert.equal(f('decision warranty').key, 'plain');
  assert.deepEqual(f('decision warranty').unknownTerms, []);
  assert.deepEqual(f('decisions Warranty').terms, ['decision', 'warranty']);
  assert.equal(f('human-in-the-loop').key, 'plain', 'a hyphen inside a word is not a NOT operator');
  assert.equal(f('canvas and warranty').hasOperators, false, 'lowercase and is a word');
  assert.equal(f('Hello!').hasOperators, false);
  assert.equal(f('').key, 'plain');
  assert.equal(f('???').key, 'plain');
});

test('operators or fields: proposes boolean', () => {
  for (const q of ['decision AND (warranty OR canvas)', 'title:spine', 'solver -quantum', 'NOT pricing', '+moloto framework', 'warrant*', 'tag:instrument OR type:note']) {
    assert.equal(pick(q).algorithm, 'boolean', q);
    assert.ok(share(q, 'boolean', 100) > 0.9, q);
  }
});

test('misspellings: proposes fuzzy, and fuzzy finds the right words', () => {
  for (const q of ['warrnaty', 'cohrence']) {
    assert.equal(pick(q).algorithm, 'fuzzy', q);
    assert.ok(share(q, 'fuzzy', 100) > 0.9, q);
  }
  assert.ok(fuzzy.run('warrnaty', ix).some((r) => r.matched.includes('warranty')));
  assert.ok(fuzzy.run('cohrence', ix).some((r) => r.matched.includes('coherence')));
});

test('short partial: proposes prefix', () => {
  for (const q of ['arch', 'deci']) {
    assert.equal(pick(q).algorithm, 'prefix', q);
    assert.ok(share(q, 'prefix', 100) > 0.9, q);
  }
});

test('quoted multiword: proposes phrase or boolean', () => {
  for (let s = 1; s <= 60; s++) assert.ok(['phrase', 'boolean'].includes(pick('"human in the loop"', s).algorithm));
  assert.ok(share('"human in the loop"', 'bm25', 100) < 0.1);
});

test('plain: proposes bm25', () => {
  for (const q of ['decision warranty', 'spine', 'evidence identity meaning']) {
    assert.equal(pick(q).algorithm, 'bm25', q);
    assert.ok(share(q, 'bm25', 100) > 0.9, q);
  }
});

test('choose returns the contract shape and ranks by sample', () => {
  const c = pick('decision warranty');
  assert.deepEqual(Object.keys(c).sort(), ['algorithm', 'explanation', 'features', 'ranked']);
  assert.equal(c.ranked.length, 5);
  assert.equal(c.ranked[0].name, c.algorithm);
  for (const r of c.ranked) { assert.equal(typeof r.prior, 'number'); assert.ok(r.sample >= 0 && r.sample <= 1); }
  for (let i = 1; i < c.ranked.length; i++) assert.ok(c.ranked[i - 1].sample >= c.ranked[i].sample);
});

test('choose does not change state', () => {
  const ag = new SearchAgent({ rng: seededRng(1), state: {} });
  ag.choose('decision warranty', ix);
  assert.deepEqual(ag.state, {});
});

test('learning: repeated rewards favouring another algorithm shift the choice for that bucket only', () => {
  const key = 'plain';
  const q = 'decision warranty';
  assert.equal(pick(q, 5).algorithm, 'bm25');
  let state = {};
  for (let i = 0; i < 40; i++) {
    state = applyReward(state, key, 'phrase', 1);
    state = applyReward(state, key, 'bm25', 0);
  }
  const learnedShare = share(q, 'phrase', 200, state);
  assert.ok(learnedShare > 0.85, `phrase share after learning: ${learnedShare}`);
  assert.equal(pick(q, 5, state).algorithm, 'phrase');
  // other buckets untouched
  assert.equal(pick('warrnaty', 5, state).algorithm, 'fuzzy');
  assert.equal(pick('title:spine', 5, state).algorithm, 'boolean');
  // and the explanation says why it deviated from the rule
  assert.match(pick(q, 5, state).explanation, /gone better|so far/i);
});

test('learning through the agent API with a seeded rng', () => {
  const ag = new SearchAgent({ rng: seededRng(99) });
  for (let i = 0; i < 30; i++) { ag.learn('typo', 'prefix', 1); ag.learn('typo', 'fuzzy', 0); }
  let prefixWins = 0;
  for (let i = 0; i < 100; i++) if (ag.choose('warrnaty', ix).algorithm === 'prefix') prefixWins++;
  assert.ok(prefixWins > 80, String(prefixWins));
});

test('a weak rule prior can be exploited away from, but not instantly', () => {
  let state = applyReward({}, 'plain', 'fuzzy', 1);
  assert.ok(share('decision warranty', 'bm25', 200, state) > 0.9, 'one reward does not flip a strong prior');
});

test('reward() is pure: input state is unchanged and the result is new', () => {
  const state = Object.freeze({ plain: Object.freeze({ bm25: Object.freeze({ a: 3, b: 2 }) }) });
  const snapshot = JSON.stringify(state);
  const ag = new SearchAgent({ state });
  const next = ag.reward('plain', 'bm25', 1);
  assert.equal(JSON.stringify(state), snapshot);
  assert.deepEqual(next.plain.bm25, { a: 4, b: 2 });
  assert.notEqual(next, state);
  assert.notEqual(next.plain, state.plain);
  assert.equal(ag.state, state, 'reward does not mutate the agent either');
  const n2 = applyReward(next, 'plain', 'bm25', 0.25);
  assert.deepEqual(n2.plain.bm25, { a: 4.25, b: 2.75 });
  assert.deepEqual(next.plain.bm25, { a: 4, b: 2 });
});

test('reward() starts from the rule prior and clamps r into [0,1]', () => {
  const n = applyReward({}, 'typo', 'fuzzy', 1);
  assert.deepEqual(n.typo.fuzzy, { a: PRIORS.typo.fuzzy.a + 1, b: PRIORS.typo.fuzzy.b });
  assert.deepEqual(applyReward({}, 'typo', 'fuzzy', 5).typo.fuzzy, n.typo.fuzzy);
  assert.deepEqual(applyReward({}, 'typo', 'fuzzy', -3).typo.fuzzy, { a: PRIORS.typo.fuzzy.a, b: PRIORS.typo.fuzzy.b + 1 });
  assert.deepEqual(applyReward({ x: 1 }, 'typo', 'fuzzy', NaN), { x: 1 });
  assert.deepEqual(applyReward(undefined, 'typo', 'fuzzy', 'abc'), {});
});

test('events map to rewards per the contract', () => {
  const ev = (type, data) => ({ type, data });
  assert.deepEqual(rewardsFromEvent(ev('ResultOpened', { key: 'plain', algorithm: 'bm25', rank: 1 })), [{ key: 'plain', algorithm: 'bm25', r: 1 }]);
  assert.deepEqual(rewardsFromEvent(ev('ResultOpened', { key: 'plain', algorithm: 'bm25', rank: 3 })), [{ key: 'plain', algorithm: 'bm25', r: 1 }]);
  assert.deepEqual(rewardsFromEvent(ev('ResultOpened', { key: 'plain', algorithm: 'bm25', rank: 7 })), [{ key: 'plain', algorithm: 'bm25', r: 0.6 }]);
  assert.deepEqual(rewardsFromEvent(ev('SearchSubmitted', { key: 'typo', algorithm: 'fuzzy', resultCount: 0 })), [{ key: 'typo', algorithm: 'fuzzy', r: 0 }]);
  assert.deepEqual(rewardsFromEvent(ev('SearchSubmitted', { key: 'typo', algorithm: 'fuzzy', resultCount: 4 })), []);
  assert.deepEqual(rewardsFromEvent(ev('AlgorithmOverridden', { key: 'plain', proposed: 'bm25', chosen: 'phrase' })), [
    { key: 'plain', algorithm: 'bm25', r: 0 }, { key: 'plain', algorithm: 'phrase', r: 1 },
  ]);
  assert.deepEqual(rewardsFromEvent(ev('Whatever', {})), []);
  assert.deepEqual(rewardsFromEvent(null), []);
  const s = foldEvents({}, [
    ev('AlgorithmOverridden', { key: 'plain', proposed: 'bm25', chosen: 'phrase' }),
    ev('AlgorithmOverridden', { key: 'plain', proposed: 'bm25', chosen: 'phrase' }),
  ]);
  assert.ok(s.plain.phrase.a > PRIORS.plain.phrase.a && s.plain.bm25.b > PRIORS.plain.bm25.b);
  assert.deepEqual(foldEvents(s, []), s);
});

test('explanation: a non-empty sentence that names the reason', () => {
  const reasons = {
    'decision AND warranty': /operator|field/i,
    'title:spine': /operator|field/i,
    '"human in the loop"': /quoted phrase/i,
    warrnaty: /vocabulary/i,
    arch: /start of a word/i,
    'decision warranty': /plain keyword/i,
  };
  for (const [q, re] of Object.entries(reasons)) {
    const e = pick(q).explanation;
    assert.equal(typeof e, 'string');
    assert.ok(e.trim().length > 20, q);
    assert.match(e, re, q);
    assert.match(e, /You can switch\./);
    assert.ok(e.split(/(?<=\.)\s/).length <= 2, 'one or two sentences');
  }
  assert.match(pick('warrnaty').explanation, /"warrnaty"/);
});
