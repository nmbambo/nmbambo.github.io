// search() orchestrator and snippet() highlighter. The parser can never crash search().
import { scan, stem } from './tokenize.mjs';
import { ALGORITHMS, algorithmByName, bm25 } from './algorithms.mjs';
import { parse, ParseError } from './boolean.mjs';
import { SearchAgent } from './agent.mjs';

const DEFAULT_LIMIT = 100;

/**
 * Short excerpt of doc.text centred on the first match, with matches wrapped as {{term}}.
 * `matched` may hold stems (as results provide) or plain words.
 */
export function snippet(doc, matched, maxLen = 220) {
  const max = Math.max(20, Math.floor(Number(maxLen)) || 220);
  const text = String(doc?.text ?? '').replace(/\{\{|\}\}/g, ' ').replace(/\s+/g, ' ').trim();
  if (!text) return '';
  const set = new Set((matched || []).map((m) => String(m).toLowerCase()));
  const isHit = (it) => set.size > 0 && (set.has(it.term) || (!it.compound && set.has(stem(it.term))));
  const first = set.size ? firstHit(text, isHit) : null;
  let start = 0;
  if (first) start = Math.max(0, first.start - Math.floor((max - (first.end - first.start)) / 2));
  let end = Math.min(text.length, start + max);
  if (end - start < max) start = Math.max(0, end - max);
  if (start > 0) { const sp = text.indexOf(' ', start); if (sp !== -1 && sp < end) start = sp + 1; }
  if (end < text.length) { const sp = text.lastIndexOf(' ', end); if (sp > start) end = sp; }
  const ranges = [];
  if (first) {
    for (const h of scan(text.slice(start, end))) {
      if (!isHit(h)) continue;
      const s0 = h.start + start, e0 = h.end + start;
      const last = ranges[ranges.length - 1];
      if (last && s0 <= last[1]) last[1] = Math.max(last[1], e0); else ranges.push([s0, e0]);
    }
  }
  let out = '';
  let cur = start;
  for (const [s, e] of ranges) { out += text.slice(cur, s) + '{{' + text.slice(s, e) + '}}'; cur = e; }
  out += text.slice(cur, end);
  return (start > 0 ? '…' : '') + out + (end < text.length ? '…' : '');
}

// Find the first matching word without scanning the whole text: blocks are cut at spaces so no word is split.
function firstHit(text, isHit, block = 300) {
  for (let from = 0; from < text.length; block *= 2) {
    let to = Math.min(text.length, from + block);
    if (to < text.length) { const sp = text.indexOf(' ', to); to = sp === -1 ? text.length : sp; }
    for (const it of scan(text.slice(from, to))) if (isHit(it)) return { start: it.start + from, end: it.end + from };
    from = to;
  }
  return null;
}

const SYNTAX = /(^|[\s(])(AND|OR|NOT)(?=$|[\s(])|["()|&*]|(^|\s)[-+!]\S|\b(?:title|section|text|tags?|type|source):/;

function decorate(index, ranked, limit) {
  const take = Number.isFinite(limit) ? ranked.slice(0, Math.max(0, limit)) : ranked;
  return take.map((r) => {
    const doc = index.get(r.id);
    return {
      id: r.id, score: r.score, matched: r.matched,
      title: doc?.title ?? '', url: doc?.url ?? null, section: doc?.section ?? '', type: doc?.type ?? '', source: doc?.source ?? '',
      snippet: snippet(doc, r.matched), doc,
    };
  });
}

/**
 * search(query, index, agent, { algorithm, limit }) ->
 *   { results, total, algorithm, proposed, explanation, error, key, features }
 * `algorithm` is the one actually used; `proposed` is what the agent suggested. A user-supplied
 * `algorithm` (override) wins. Malformed boolean syntax yields a friendly `error` and a bm25 fallback.
 */
export function search(query, index, agent, opts = {}) {
  const q = typeof query === 'string' ? query : '';
  const limit = opts.limit === undefined ? DEFAULT_LIMIT : opts.limit;
  const ag = agent || new SearchAgent();
  const list = ag.algorithms?.length ? ag.algorithms : ALGORITHMS;
  const base = { results: [], total: 0, algorithm: 'bm25', proposed: 'bm25', explanation: '', error: null, key: 'plain', features: null };

  if (!q.trim()) return { ...base, explanation: 'Type a word or two to search.' };

  let choice;
  try {
    choice = ag.choose(q, index);
  } catch (e) {
    choice = { algorithm: 'bm25', explanation: 'This is a plain keyword search, so I used keyword ranking (BM25). You can switch.', features: null };
  }
  const key = choice.features?.key ?? 'plain';
  const proposed = choice.algorithm;
  const asked = opts.algorithm;
  const override = typeof asked === 'string' && asked ? algorithmByName(asked, list) || algorithmByName(asked) : null;

  let used = override || algorithmByName(proposed, list) || bm25;
  let error = null;
  let explanation;
  if (asked && !override) {
    error = `I don't know a search mode called "${asked}", so I used the proposed one instead.`;
  }
  explanation = override
    ? `Using ${override.label} as you chose. I had proposed ${algorithmByName(proposed, list)?.label ?? proposed}. ${choice.explanation}`
    : choice.explanation;

  const fallback = (message) => {
    error = message;
    used = bm25;
    explanation = `${message} I've searched your words with plain keyword ranking (BM25) instead.`;
  };

  // Lint boolean syntax first so malformed input always gets a friendly message, whatever was proposed.
  if (used.name === 'boolean' || (!override && SYNTAX.test(q))) {
    try { parse(q); } catch (e) {
      fallback(e instanceof ParseError ? e.message : "I couldn't read that search.");
    }
  }

  let ranked;
  try {
    ranked = used.run(q, index);
  } catch (e) {
    if (used.name !== 'bm25') fallback(e instanceof ParseError ? e.message : "I couldn't read that search.");
    try { ranked = bm25.run(q, index); } catch { ranked = []; error = error || 'Something went wrong while searching.'; }
  }
  return {
    results: decorate(index, ranked, limit),
    total: ranked.length,
    algorithm: used.name,
    proposed,
    explanation,
    error,
    key,
    features: choice.features ?? null,
  };
}
