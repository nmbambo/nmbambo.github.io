// Tokenizer and light English stemmer for the site search layer.
// Pure module: no DOM, no I/O, safe to import anywhere.

export const STOPWORDS = new Set((
  'a an and are as at be but by for from has have he her his i if in into is it its me my no not of on or our she so ' +
  'than that the their them then there these they this to up us was we were what when which who will with you your'
).split(/\s+/));

const MARKS = /\p{M}/gu;
const ASCII = /^[\x00-\x7f]*$/;
const WORD_UNI = /[\p{L}\p{N}]+(?:-[\p{L}\p{N}]+)*/gu;
const WORD_ASCII = /[a-z0-9]+(?:-[a-z0-9]+)*/g;

/** NFKD, strip diacritics, lowercase. */
export function fold(text) {
  return String(text ?? '').normalize('NFKD').replace(MARKS, '').toLowerCase();
}

// Folds while remembering, for each folded UTF-16 unit, the offset in the original string.
function foldWithMap(s) {
  if (ASCII.test(s)) return { norm: s.toLowerCase(), map: null };
  let norm = '';
  const map = [];
  for (let i = 0; i < s.length; i++) {
    const code = s.charCodeAt(i);
    if (code < 128) { norm += s[i].toLowerCase(); map.push(i); continue; }
    const f = s[i].normalize('NFKD').replace(MARKS, '').toLowerCase();
    for (let k = 0; k < f.length; k++) { norm += f[k]; map.push(i); }
  }
  return { norm, map };
}

/**
 * Scan text into word items with positions and offsets into the ORIGINAL string.
 * A hyphenated word yields each part (consecutive positions) plus the whole compound,
 * which shares the position of its first part so phrase matching is unaffected.
 * With withOffsets=false, start/end are only approximate (folded-string offsets); used by the indexer.
 * @returns {{term:string,pos:number,start:number,end:number,compound:boolean}[]}
 */
export function scan(text, withOffsets = true) {
  const s = String(text ?? '');
  // Offsets are only needed for highlighting; indexing skips the per-character offset map.
  const { norm, map } = withOffsets ? foldWithMap(s) : ASCII.test(s) ? { norm: s.toLowerCase(), map: null } : { norm: fold(s), map: null };
  const uni = withOffsets ? !!map : !ASCII.test(norm);
  const re = uni ? WORD_UNI : WORD_ASCII;
  re.lastIndex = 0;
  const out = [];
  let pos = 0;
  let m;
  while ((m = re.exec(norm)) !== null) {
    const word = m[0];
    const start = m.index;
    const hyphen = word.indexOf('-') !== -1;
    if (!hyphen) {
      out.push({ term: word, pos, start: map ? map[start] : start, end: map ? map[start + word.length - 1] + 1 : start + word.length, compound: false });
      pos++;
      continue;
    }
    const parts = word.split('-');
    const first = pos;
    let off = start;
    for (const p of parts) {
      out.push({ term: p, pos, start: map ? map[off] : off, end: map ? map[off + p.length - 1] + 1 : off + p.length, compound: false });
      pos++;
      off += p.length + 1;
    }
    out.push({ term: word, pos: first, start: map ? map[start] : start, end: map ? map[start + word.length - 1] + 1 : start + word.length, compound: true });
  }
  return out;
}

/**
 * Allocation-light variant of scan() for the indexer: calls fn(term, pos, compound) per word, no offsets.
 * Returns the number of positions used (non-compound words).
 */
export function eachTerm(text, fn) {
  const s = String(text ?? '');
  const norm = ASCII.test(s) ? s.toLowerCase() : fold(s);
  const re = ASCII.test(norm) ? WORD_ASCII : WORD_UNI;
  re.lastIndex = 0;
  let pos = 0;
  let m;
  while ((m = re.exec(norm)) !== null) {
    const word = m[0];
    if (word.indexOf('-') === -1) { fn(word, pos, false); pos++; continue; }
    const first = pos;
    for (const p of word.split('-')) { fn(p, pos, false); pos++; }
    fn(word, first, true);
  }
  return pos;
}

/** Tokens including hyphenated compounds (e.g. "human-in-the-loop" -> human, in, the, loop, human-in-the-loop). */
export function tokenize(text) {
  return scan(text).map((t) => t.term);
}

/** Tokens without the hyphenated compound forms (used for phrases and bare query terms). */
export function tokenizeParts(text) {
  return scan(text).filter((t) => !t.compound).map((t) => t.term);
}

const VOWEL = /[aeiouy]/;
const memo = new Map();

function undouble(w) {
  const n = w.length;
  if (n > 3 && w[n - 1] === w[n - 2] && !/[aeioulsz]/.test(w[n - 1])) return w.slice(0, -1);
  return w;
}

/** Light English suffix stripping: ies->y, es, s, ing, ed, ly. Never returns fewer than 3 characters. */
export function stem(word) {
  const cached = memo.get(word);
  if (cached !== undefined) return cached;
  const w0 = String(word ?? '').toLowerCase();
  if (w0.length < 4 || w0.includes('-') || /\d/.test(w0)) return w0;
  const hit = memo.get(w0);
  if (hit !== undefined) return hit;
  let w = w0;
  // plurals
  if (w.length > 4 && w.endsWith('ies')) w = w.slice(0, -3) + 'y';
  else if (w.endsWith('sses')) w = w.slice(0, -2);
  else if (/(ches|shes|xes)$/.test(w) && w.length > 4) w = w.slice(0, -2);
  else if (w.endsWith('s') && !/(ss|us|is)$/.test(w)) w = w.slice(0, -1);
  // verb endings
  if (w.length > 5 && w.endsWith('ing')) {
    const b = w.slice(0, -3);
    if (b.length >= 3 && VOWEL.test(b)) w = undouble(b);
  } else if (w.length > 4 && w.endsWith('ied')) {
    w = w.slice(0, -3) + 'y';
  } else if (w.length > 4 && w.endsWith('ed')) {
    const b = w.slice(0, -2);
    if (b.length >= 3 && VOWEL.test(b)) w = undouble(b);
  }
  // adverbs
  if (w.length > 5 && w.endsWith('ily')) w = w.slice(0, -3) + 'y';
  else if (w.length > 5 && w.endsWith('ly')) w = w.slice(0, -2);
  if (w.length < 3) w = w0;
  if (memo.size > 50000) memo.clear();
  memo.set(w0, w);
  if (w0 !== word) memo.set(word, w);
  return w;
}

/** Tokenize then stem; positions follow scan(). */
export function analyze(text) {
  return scan(text).map((t) => ({ ...t, term: stem(t.term) }));
}
