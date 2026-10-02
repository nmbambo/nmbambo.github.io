// Incremental, field-aware inverted index. Terms are stems (see tokenize.stem).
import { eachTerm, stem } from './tokenize.mjs';

export const SCORE_FIELDS = ['title', 'section', 'text', 'tags'];
export const ALL_FIELDS = [...SCORE_FIELDS, 'type', 'source'];
export const FIELD_WEIGHTS = { title: 3, section: 2, text: 1, tags: 2 };
/** Offset added per field to positions returned by postings(term), so positions never adjoin across fields. */
export const FIELD_STRIDE = 1_000_000;
const OFFSETS = { title: 0, section: FIELD_STRIDE, text: 2 * FIELD_STRIDE, tags: 3 * FIELD_STRIDE };
const PIECE_GAP = 10;
const EMPTY = new Map();

const FIELD_ALIASES = { tag: 'tags', tags: 'tags', title: 'title', section: 'section', text: 'text', type: 'type', source: 'source' };
/** Map a user-facing field name (tag, title, ...) to an index field, or null. */
export function resolveField(name) {
  return FIELD_ALIASES[String(name ?? '').toLowerCase()] ?? null;
}

function pieces(doc, field) {
  const v = field === 'tags' ? doc.tags : doc[field];
  if (v == null) return [];
  if (Array.isArray(v)) return v.map((x) => String(x ?? ''));
  return [String(v)];
}

export class SearchIndex {
  #docs = new Map();            // id -> { doc, terms:{field:string[]}, len:{field:number} }
  #post = {};                   // field -> Map<term, Map<id, number[]>>
  #df = new Map();              // term -> number of docs containing it in a scoring field
  #total = {};                  // field -> total token count
  #vocab = null;
  #merged = new Map();
  version = 0;

  constructor(docs) {
    for (const f of ALL_FIELDS) { this.#post[f] = new Map(); this.#total[f] = 0; }
    if (docs) for (const d of docs) this.add(d);
  }

  /** Add (or replace) a document. Returns false for an unusable document. */
  add(doc) {
    if (!doc || typeof doc.id !== 'string' || !doc.id) return false;
    if (this.#docs.has(doc.id)) this.remove(doc.id);
    const terms = {};
    const len = {};
    const scoringTerms = new Set();
    for (const f of ALL_FIELDS) {
      const local = new Map();
      let base = 0;
      let count = 0;
      for (const piece of pieces(doc, f)) {
        const off = base;
        const used = eachTerm(piece, (word, pos, compound) => {
          const t = stem(word);
          let arr = local.get(t);
          if (!arr) { arr = []; local.set(t, arr); }
          arr.push(off + pos);
          if (!compound) count++;
        });
        const maxPos = used - 1;
        base += maxPos + 1 + PIECE_GAP;
      }
      const pf = this.#post[f];
      const scoring = SCORE_FIELDS.includes(f);
      for (const [t, arr] of local) {
        let m = pf.get(t);
        if (!m) { m = new Map(); pf.set(t, m); }
        m.set(doc.id, arr);
        if (scoring) scoringTerms.add(t);
      }
      terms[f] = [...local.keys()];
      len[f] = count;
      this.#total[f] += count;
    }
    for (const t of scoringTerms) this.#df.set(t, (this.#df.get(t) || 0) + 1);
    this.#docs.set(doc.id, { doc, terms, len });
    this.#touch();
    return true;
  }

  remove(id) {
    const rec = this.#docs.get(id);
    if (!rec) return false;
    const scoringTerms = new Set();
    for (const f of ALL_FIELDS) {
      const pf = this.#post[f];
      for (const t of rec.terms[f]) {
        const m = pf.get(t);
        if (m) { m.delete(id); if (m.size === 0) pf.delete(t); }
        if (SCORE_FIELDS.includes(f)) scoringTerms.add(t);
      }
      this.#total[f] -= rec.len[f];
    }
    for (const t of scoringTerms) {
      const n = (this.#df.get(t) || 0) - 1;
      if (n <= 0) this.#df.delete(t); else this.#df.set(t, n);
    }
    this.#docs.delete(id);
    this.#touch();
    return true;
  }

  #touch() { this.#vocab = null; this.#merged.clear(); this.version++; }

  get(id) { return this.#docs.get(id)?.doc; }
  has(id) { return this.#docs.has(id); }
  get size() { return this.#docs.size; }
  docs() { return [...this.#docs.values()].map((r) => r.doc); }
  ids() { return [...this.#docs.keys()]; }

  /** Postings for the four scoring fields merged: Map<docId, positions[]>. Positions carry a per-field offset (FIELD_STRIDE). */
  postings(term) {
    let m = this.#merged.get(term);
    if (m) return m;
    m = new Map();
    for (const f of SCORE_FIELDS) {
      const fm = this.#post[f].get(term);
      if (!fm) continue;
      const off = OFFSETS[f];
      for (const [id, arr] of fm) {
        const cur = m.get(id);
        const shifted = off ? arr.map((p) => p + off) : arr;
        m.set(id, cur ? cur.concat(shifted) : shifted);
      }
    }
    this.#merged.set(term, m);
    return m;
  }

  /** Raw postings for one field (title, section, text, tags, type, source): Map<docId, positions[]>. */
  fieldPostings(term, field) {
    return this.#post[field]?.get(term) ?? EMPTY;
  }

  /** Sorted array of every stem found in a scoring field. */
  vocabulary() {
    if (!this.#vocab) this.#vocab = [...this.#df.keys()].sort();
    return this.#vocab;
  }
  hasTerm(term) { return this.#df.has(term); }
  docFreq(term) { return this.#df.get(term) || 0; }

  /** Vocabulary terms starting with prefix. With a field, only terms present in that field. */
  prefixTerms(prefix, field = null) {
    const out = [];
    if (field === 'type' || field === 'source') {
      for (const t of this.#post[field].keys()) if (t.startsWith(prefix)) out.push(t);
      return out.sort();
    }
    const v = this.vocabulary();
    let lo = 0, hi = v.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (v[mid] < prefix) lo = mid + 1; else hi = mid; }
    for (let i = lo; i < v.length && v[i].startsWith(prefix); i++) {
      if (!field || this.#post[field].get(v[i])) out.push(v[i]);
    }
    return out;
  }

  /** Doc ids containing term in any scoring field (or in the given field). */
  docsWith(term, field = null) {
    const out = new Set();
    for (const f of field ? [field] : SCORE_FIELDS) for (const id of this.fieldPostings(term, f).keys()) out.add(id);
    return out;
  }

  /** Exact consecutive-phrase matches of already-stemmed terms: Map<docId, occurrences>. */
  phraseMatches(terms, field = null) {
    const out = new Map();
    if (!terms.length) return out;
    const maps = terms.map((t) => (field ? this.fieldPostings(t, field) : this.postings(t)));
    if (maps.some((m) => m.size === 0)) return out;
    const small = maps.reduce((a, b) => (b.size < a.size ? b : a));
    for (const id of small.keys()) {
      const lists = maps.map((m) => m.get(id));
      if (lists.some((l) => !l)) continue;
      const sets = lists.slice(1).map((l) => new Set(l));
      let c = 0;
      for (const p of lists[0]) {
        let ok = true;
        for (let i = 0; i < sets.length; i++) if (!sets[i].has(p + i + 1)) { ok = false; break; }
        if (ok) c++;
      }
      if (c) out.set(id, c);
    }
    return out;
  }

  fieldLength(id, field) { return this.#docs.get(id)?.len[field] ?? 0; }
  avgFieldLength(field) { return this.#docs.size ? (this.#total[field] || 0) / this.#docs.size : 0; }

  stats() {
    const avg = {};
    for (const f of ALL_FIELDS) avg[f] = this.avgFieldLength(f);
    return { docs: this.#docs.size, terms: this.#df.size, tokens: { ...this.#total }, avgLength: avg, version: this.version };
  }
}
