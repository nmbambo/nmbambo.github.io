// Five ranking algorithms. Each: { name, label, description, run(query, index) -> [{ id, score, matched }] }.
// `matched` lists the vocabulary terms (stems) that actually matched the document, for highlighting.
import { tokenizeParts, stem, STOPWORDS } from './tokenize.mjs';
import { SCORE_FIELDS, FIELD_WEIGHTS, resolveField } from './index.mjs';
import { parse, evaluate, collectTerms } from './boolean.mjs';

export const K1 = 1.2;
export const B = 0.75;
const MAX_QUERY_TERMS = 32;
export const PROXIMITY_WINDOW = 8;

/** Optimal-string-alignment (Damerau–Levenshtein) distance with early exit; returns max+1 when over the limit. */
export function damerauLevenshtein(a, b, max = Infinity) {
  const la = a.length, lb = b.length;
  if (Math.abs(la - lb) > max) return max + 1;
  if (a === b) return 0;
  let prev2 = null;
  let prev = Array.from({ length: lb + 1 }, (_, j) => j);
  for (let i = 1; i <= la; i++) {
    const cur = [i];
    let rowMin = i;
    for (let j = 1; j <= lb; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      let v = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) v = Math.min(v, prev2[j - 2] + 1);
      cur[j] = v;
      if (v < rowMin) rowMin = v;
    }
    if (rowMin > max) return max + 1;
    prev2 = prev;
    prev = cur;
  }
  return prev[lb];
}

/** Allowed edit distance for a term: 0 for <4 chars, 1 for 4-7, 2 for 8+. */
export function maxEdits(term) {
  return term.length >= 8 ? 2 : term.length >= 4 ? 1 : 0;
}

/** Bare query terms: [{ raw, stem }], stopwords dropped unless nothing else is left, de-duplicated, capped. */
export function queryTerms(query) {
  const raws = tokenizeParts(typeof query === 'string' ? query : '');
  let kept = raws.filter((w) => !STOPWORDS.has(w));
  if (kept.length === 0) kept = raws;
  const seen = new Set();
  const out = [];
  for (const raw of kept) {
    const s = stem(raw);
    if (seen.has(s)) continue;
    seen.add(s);
    out.push({ raw, stem: s });
    if (out.length >= MAX_QUERY_TERMS) break;
  }
  return out;
}

/** Score entries [{term, weight?, field?}] with field-boosted BM25. Returns Map<id, {score, matched:Set}>. */
export function scoreTerms(index, entries, candidates = null) {
  const acc = new Map();
  const N = index.size || 1;
  for (const e of entries) {
    const f = e.field ? resolveField(e.field) : null;
    const fields = f ? [f] : SCORE_FIELDS;
    const per = new Map();
    for (const fld of fields) {
      const m = index.fieldPostings(e.term, fld);
      if (!m.size) continue;
      const w = FIELD_WEIGHTS[fld] ?? 1;
      const avg = index.avgFieldLength(fld) || 1;
      for (const [id, pos] of m) {
        const tf = pos.length;
        const len = index.fieldLength(id, fld);
        per.set(id, (per.get(id) || 0) + (w * tf * (K1 + 1)) / (tf + K1 * (1 - B + (B * len) / avg)));
      }
    }
    const df = per.size;
    if (!df) continue;
    const idf = Math.log(1 + (N - df + 0.5) / (df + 0.5));
    const weight = e.weight ?? 1;
    for (const [id, s] of per) {
      if (candidates && !candidates.has(id)) continue;
      let r = acc.get(id);
      if (!r) { r = { score: 0, matched: new Set() }; acc.set(id, r); }
      r.score += s * idf * weight;
      r.matched.add(e.term);
    }
  }
  return acc;
}

function toResults(acc, extra) {
  const out = [];
  for (const [id, r] of acc) out.push({ id, score: extra ? extra(id, r.score) : r.score, matched: [...r.matched].sort() });
  out.sort((a, b) => b.score - a.score || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  return out;
}

const bm25Entries = (query) => queryTerms(query).map((t) => ({ term: t.stem }));

export const bm25 = {
  name: 'bm25',
  label: 'Keyword ranking (BM25)',
  description: 'Ranks pages by how often and how prominently your words appear; title, section and tag hits count for more.',
  run(query, index) {
    return toResults(scoreTerms(index, bm25Entries(query)));
  },
};

export const booleanAlgorithm = {
  name: 'boolean',
  label: 'Boolean',
  description: 'Understands AND, OR, NOT, quotes, brackets, field:term and wildcard*; ranks the matches by BM25. Throws ParseError on malformed input.',
  run(query, index) {
    const ast = parse(query);
    const set = evaluate(ast, index);
    if (set.size === 0) return [];
    let terms = collectTerms(ast, index);
    const content = terms.filter((t) => !STOPWORDS.has(t.term));
    if (content.length) terms = content;
    const acc = scoreTerms(index, terms, set);
    for (const id of set) if (!acc.has(id)) acc.set(id, { score: 0, matched: new Set() });
    return toResults(acc);
  },
};

export const fuzzy = {
  name: 'fuzzy',
  label: 'Fuzzy matching',
  description: 'Forgives typos: unknown words are matched to near neighbours in the site vocabulary (1 edit for 4-7 letters, 2 for 8+).',
  run(query, index) {
    const vocab = index.vocabulary();
    const entries = [];
    for (const t of queryTerms(query)) {
      if (index.hasTerm(t.stem)) { entries.push({ term: t.stem, weight: 1 }); continue; }
      const limit = maxEdits(t.stem);
      if (!limit) continue;
      const cands = [];
      for (const v of vocab) {
        if (Math.abs(v.length - t.stem.length) > limit) continue;
        const d = damerauLevenshtein(t.stem, v, limit);
        if (d <= limit) cands.push({ term: v, d, df: index.docFreq(v) });
      }
      cands.sort((a, b) => a.d - b.d || b.df - a.df);
      for (const c of cands.slice(0, 8)) entries.push({ term: c.term, weight: c.d === 1 ? 0.6 : 0.4 });
    }
    return toResults(scoreTerms(index, entries));
  },
};

export const prefix = {
  name: 'prefix',
  label: 'Prefix matching',
  description: 'Treats each word as the start of a longer word; good for short, partly typed queries.',
  run(query, index) {
    const entries = [];
    for (const t of queryTerms(query)) {
      if (index.hasTerm(t.stem)) entries.push({ term: t.stem, weight: 1 });
      const exp = index.prefixTerms(t.raw).filter((v) => v !== t.stem).sort((a, b) => index.docFreq(b) - index.docFreq(a)).slice(0, 40);
      for (const v of exp) entries.push({ term: v, weight: 0.75 });
    }
    return toResults(scoreTerms(index, entries));
  },
};

// Best number of distinct query terms found inside one window of PROXIMITY_WINDOW positions.
function bestWindow(lists) {
  const ev = [];
  lists.forEach((l, ti) => { if (l) for (const p of l) ev.push([p, ti]); });
  ev.sort((a, b) => a[0] - b[0]);
  const counts = new Array(lists.length).fill(0);
  let distinct = 0, best = 0, l = 0;
  for (let r = 0; r < ev.length; r++) {
    if (counts[ev[r][1]]++ === 0) distinct++;
    while (ev[r][0] - ev[l][0] >= PROXIMITY_WINDOW) { if (--counts[ev[l][1]] === 0) distinct--; l++; }
    if (distinct > best) best = distinct;
  }
  return best;
}

export const phrase = {
  name: 'phrase',
  label: 'Phrase and proximity',
  description: 'BM25, boosted when your words sit close together (within 8 words) and most of all when they form the exact phrase.',
  run(query, index) {
    const terms = queryTerms(query);
    const acc = scoreTerms(index, terms.map((t) => ({ term: t.stem })));
    const stems = terms.map((t) => t.stem);
    if (stems.length < 2) return toResults(acc);
    const all = tokenizeParts(typeof query === 'string' ? query : '').map(stem);
    const exact = all.length >= 2 ? index.phraseMatches(all) : new Map();
    const posts = stems.map((s) => index.postings(s));
    return toResults(acc, (id, base) => {
      const cov = bestWindow(posts.map((m) => m.get(id)));
      let factor = 1;
      if (cov >= 2) factor += (0.75 * (cov - 1)) / (stems.length - 1);
      const n = exact.get(id);
      if (n) factor += 1.5 + Math.min(n - 1, 2) * 0.25;
      return base * factor;
    });
  },
};

export const ALGORITHMS = [booleanAlgorithm, bm25, fuzzy, prefix, phrase];
export function algorithmByName(name, list = ALGORITHMS) {
  return list.find((a) => a.name === name) ?? null;
}
