// SearchAgent: proposes the ranking algorithm best suited to a query ("feels for" it) using Thompson
// sampling over Beta(a, b) per (bucket key, algorithm), seeded with rule priors. It only PROPOSES and
// explains; the person can override, and overrides become negative/positive rewards.
import { tokenizeParts, stem, STOPWORDS } from './tokenize.mjs';
import { ALGORITHMS } from './algorithms.mjs';

/** Deterministic seeded generator (mulberry32) for tests. Returns a function () => [0,1). */
export function seededRng(seed = 1) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const open01 = (rng) => { const u = rng(); return u > 1e-12 ? (u < 1 ? u : 1 - 1e-12) : 1e-12; };
function normal(rng) { return Math.sqrt(-2 * Math.log(open01(rng))) * Math.cos(2 * Math.PI * open01(rng)); }

/** Gamma(shape, 1) sample, Marsaglia–Tsang (shape < 1 handled by the u^(1/shape) boost). */
export function sampleGamma(shape, rng = Math.random) {
  if (!(shape > 0)) return 0;
  if (shape < 1) return sampleGamma(shape + 1, rng) * Math.pow(open01(rng), 1 / shape);
  const d = shape - 1 / 3;
  const c = 1 / Math.sqrt(9 * d);
  for (;;) {
    let x, v;
    do { x = normal(rng); v = 1 + c * x; } while (v <= 0);
    v = v * v * v;
    const u = open01(rng);
    if (u < 1 - 0.0331 * x ** 4 || Math.log(u) < 0.5 * x * x + d * (1 - v + Math.log(v))) return d * v;
  }
}

/** Beta(a, b) sample via two Gamma draws. */
export function sampleBeta(a, b, rng = Math.random) {
  const x = sampleGamma(a, rng);
  const y = sampleGamma(b, rng);
  return x + y > 0 ? x / (x + y) : 0.5;
}

/** Rule priors as pseudo-counts { a, b } per bucket key and algorithm. Anything unlisted starts at Beta(1, 1). */
export const PRIORS = {
  ops:    { boolean: { a: 8, b: 1 }, bm25: { a: 2, b: 3 }, phrase: { a: 1, b: 4 }, fuzzy: { a: 1, b: 4 }, prefix: { a: 1, b: 4 } },
  quoted: { phrase: { a: 6, b: 1 }, boolean: { a: 5, b: 1.5 }, bm25: { a: 2, b: 3 }, fuzzy: { a: 1, b: 4 }, prefix: { a: 1, b: 4 } },
  typo:   { fuzzy: { a: 8, b: 1 }, bm25: { a: 2, b: 3 }, prefix: { a: 1.5, b: 3 }, phrase: { a: 1, b: 4 }, boolean: { a: 1, b: 5 } },
  short:  { prefix: { a: 8, b: 1 }, fuzzy: { a: 2, b: 3 }, bm25: { a: 2, b: 3 }, phrase: { a: 1, b: 4 }, boolean: { a: 1, b: 5 } },
  plain:  { bm25: { a: 8, b: 1 }, phrase: { a: 2, b: 3 }, fuzzy: { a: 1, b: 4 }, prefix: { a: 1, b: 4 }, boolean: { a: 1, b: 5 } },
};
export const KEYS = Object.keys(PRIORS);
const FLAT = { a: 1, b: 1 };
const priorOf = (key, algo) => PRIORS[key]?.[algo] ?? FLAT;

const LABELS = { boolean: 'Boolean search', bm25: 'keyword ranking (BM25)', fuzzy: 'fuzzy matching', prefix: 'prefix matching', phrase: 'phrase matching' };

/**
 * Pure reward update. state is { [key]: { [algo]: {a, b} } }; returns a NEW state, never mutating the input.
 * r in [0,1] (clamped; non-numbers leave the state unchanged). a += r, b += 1 - r.
 */
export function applyReward(state, key, algorithm, r) {
  const base = state && typeof state === 'object' ? state : {};
  const val = Number(r);
  if (typeof key !== 'string' || typeof algorithm !== 'string' || !Number.isFinite(val)) return { ...base };
  const x = Math.min(1, Math.max(0, val));
  const cur = base[key]?.[algorithm] ?? priorOf(key, algorithm);
  return { ...base, [key]: { ...(base[key] || {}), [algorithm]: { a: cur.a + x, b: cur.b + (1 - x) } } };
}

/**
 * Turn one event envelope into rewards [{ key, algorithm, r }], per the contract:
 * ResultOpened rank <= 3 (1-based) -> 1, lower -> 0.6; SearchSubmitted with zero results -> 0;
 * AlgorithmOverridden -> 0 for the proposed algorithm, 1 for the chosen one.
 */
export function rewardsFromEvent(event) {
  const d = event?.data;
  if (!d || typeof d !== 'object') return [];
  switch (event.type) {
    case 'ResultOpened':
      return d.key && d.algorithm ? [{ key: d.key, algorithm: d.algorithm, r: Number(d.rank) <= 3 ? 1 : 0.6 }] : [];
    case 'SearchSubmitted':
      return d.key && d.algorithm && d.resultCount === 0 ? [{ key: d.key, algorithm: d.algorithm, r: 0 }] : [];
    case 'AlgorithmOverridden':
      return d.key && d.proposed && d.chosen
        ? [{ key: d.key, algorithm: d.proposed, r: 0 }, { key: d.key, algorithm: d.chosen, r: 1 }]
        : [];
    default: return [];
  }
}

/** Fold events into agent state (pure). Handy for the agentStats projector. */
export function foldEvents(state, events) {
  let s = state || {};
  for (const e of events) for (const x of rewardsFromEvent(e)) s = applyReward(s, x.key, x.algorithm, x.r);
  return s;
}

const FIELD_RE = /\b(?:title|section|text|tags?|type|source):/i;

export class SearchAgent {
  constructor({ algorithms = ALGORITHMS, state = {}, rng = Math.random } = {}) {
    this.algorithms = Array.isArray(algorithms) ? algorithms : Object.values(algorithms);
    this.state = state || {};
    this.rng = typeof rng === 'function' ? rng : Math.random;
  }

  /** Describe a query as coarse features plus a bucket `key` (ops | quoted | short | typo | plain). */
  features(query, index) {
    const q = typeof query === 'string' ? query : '';
    const hasFields = FIELD_RE.test(q);
    const hasQuotes = q.includes('"');
    const hasWildcard = /[\p{L}\p{N}]\*/u.test(q);
    const hasOperators =
      /(^|[\s(])(AND|OR|NOT)(?=$|[\s(])/.test(q) || /\|/.test(q) || /(^|\s)&|&(\s|$)/.test(q) ||
      /[()]/.test(q) || /(^|[\s(])[-+!]\S/.test(q) || hasWildcard;
    const stripped = q.replace(/\b(?:title|section|text|tags?|type|source):/gi, ' ').replace(/\b(?:AND|OR|NOT)\b/g, ' ');
    const rawAll = tokenizeParts(stripped);
    let raws = rawAll.filter((w) => !STOPWORDS.has(w));
    if (!raws.length) raws = rawAll;
    const wildcardWords = new Set(
      (q.match(/[\p{L}\p{N}-]+\*/gu) || []).map((w) => tokenizeParts(w.replace(/\*+$/, ''))[0]).filter(Boolean),
    );
    const terms = [];
    const unknownTerms = [];
    const seen = new Set();
    for (const raw of raws) {
      const s = stem(raw);
      if (seen.has(s)) continue;
      seen.add(s);
      terms.push(s);
      if (!wildcardWords.has(raw) && !index.hasTerm(s)) unknownTerms.push(s);
    }
    const only = raws.length === 1 ? raws[0] : null;
    const shortPartial =
      !!only && only.length >= 2 && only.length <= 5 && !index.hasTerm(stem(only)) && index.prefixTerms(only).length > 0 &&
      !hasOperators && !hasQuotes && !hasFields;
    let key = 'plain';
    if (hasOperators || hasFields) key = 'ops';
    else if (hasQuotes) key = 'quoted';
    else if (shortPartial) key = 'short';
    else if (unknownTerms.length) key = 'typo';
    return { hasOperators, hasQuotes, hasFields, hasWildcard, terms, unknownTerms, shortPartial, key };
  }

  #params(key, name) {
    return this.state?.[key]?.[name] ?? priorOf(key, name);
  }

  /** Thompson-sample every algorithm for this query's bucket and propose the winner. Does not change state. */
  choose(query, index) {
    const features = this.features(query, index);
    const { key } = features;
    const ranked = this.algorithms.map((alg) => {
      const { a, b } = this.#params(key, alg.name);
      const p = priorOf(key, alg.name);
      return { name: alg.name, prior: p.a / (p.a + p.b), mean: a / (a + b), a, b, sample: sampleBeta(a, b, this.rng) };
    });
    ranked.sort((x, y) => y.sample - x.sample);
    const algorithm = ranked[0]?.name ?? 'bm25';
    return { algorithm, ranked, explanation: this.explain(features, algorithm, ranked), features };
  }

  explain(features, algorithm, ranked = []) {
    const f = features;
    const label = LABELS[algorithm] || algorithm;
    const favourite = [...ranked].sort((x, y) => y.prior - x.prior)[0]?.name;
    const learned = !!this.state?.[f.key]?.[algorithm];
    let because;
    if (favourite && algorithm !== favourite) {
      because = learned
        ? `Searches like this one have gone better with ${label} so far, so I'm proposing it.`
        : `I'm trying ${label} to see whether it suits searches like this one.`;
    } else {
      switch (f.key) {
        case 'ops': because = `Your search uses operators or fields (such as AND, OR, NOT, brackets, a minus sign or title:), so I'm proposing ${label}.`; break;
        case 'quoted': because = `Your search has a quoted phrase, so I'm proposing ${label} to keep those words together.`; break;
        case 'short': because = `That looks like the start of a word rather than a whole word, so I'm proposing ${label}.`; break;
        case 'typo': {
          const w = f.unknownTerms.slice(0, 3).map((t) => `"${t}"`).join(', ');
          because = `${f.unknownTerms.length > 1 ? 'Some words' : 'One word'} (${w}) ${f.unknownTerms.length > 1 ? "aren't" : "isn't"} in the site's vocabulary, so I'm proposing ${label}.`;
          break;
        }
        default: because = `This is a plain keyword search, so I'm proposing ${label}, which ranks by how often and how prominently your words appear.`;
      }
    }
    return `${because} You can switch.`;
  }

  /** Pure: returns a new state with the reward applied; does not touch this.state. */
  reward(key, algorithm, r) {
    return applyReward(this.state, key, algorithm, r);
  }

  /** Convenience: apply a reward and keep the result as this agent's state. */
  learn(key, algorithm, r) {
    this.state = this.reward(key, algorithm, r);
    return this.state;
  }
}
