// Boolean search-string parser and evaluator.
//
// Grammar (lowest to highest precedence):
//   expr   := and ( OR and )*                 OR also | ||
//   and    := unary ( [AND] unary )*          AND also & && ; adjacency is an implicit AND
//   unary  := (NOT | ! | -) unary | + unary | primary
//   primary:= ( expr ) | "phrase" | field:term | field:"phrase" | term | term*
// Operators are recognised only when UPPERCASE (AND, OR, NOT); lowercase "and" is an ordinary word.
// +term makes a term required; when any +term is present in an AND group, the unmarked terms only affect ranking.
import { tokenizeParts, fold, stem } from './tokenize.mjs';
import { resolveField, SCORE_FIELDS } from './index.mjs';

export const MAX_QUERY_LENGTH = 1000;
export const MAX_DEPTH = 40;
const FIELD_NAMES = new Set(['title', 'section', 'text', 'tag', 'tags', 'type', 'source']);

export class ParseError extends Error {
  constructor(message, position = 0) {
    super(message);
    this.name = 'ParseError';
    this.position = position;
  }
}

function lex(q) {
  const toks = [];
  const n = q.length;
  let i = 0;
  const readPhrase = (open) => {
    const close = q.indexOf('"', open + 1);
    if (close === -1) throw new ParseError(`The quote that opens at character ${open + 1} is never closed. Add a closing " or remove it.`, open);
    const value = q.slice(open + 1, close);
    if (!/[\p{L}\p{N}]/u.test(value)) throw new ParseError(`There is nothing to search for between the quotes at character ${open + 1}.`, open);
    return { value, next: close + 1 };
  };
  while (i < n) {
    const c = q[i];
    if (/\s/.test(c)) { i++; continue; }
    if (c === '(') { toks.push({ t: 'LP', pos: i }); i++; continue; }
    if (c === ')') { toks.push({ t: 'RP', pos: i }); i++; continue; }
    if (c === '"') { const r = readPhrase(i); toks.push({ t: 'PHRASE', value: r.value, pos: i }); i = r.next; continue; }
    if (c === '|') { toks.push({ t: 'OR', pos: i, text: 'OR' }); i += q[i + 1] === '|' ? 2 : 1; continue; }
    if (c === '&') { toks.push({ t: 'AND', pos: i, text: 'AND' }); i += q[i + 1] === '&' ? 2 : 1; continue; }
    if (c === '!') { toks.push({ t: 'NOT', pos: i, text: 'NOT' }); i++; continue; }
    if (c === '-' || c === '+') {
      const nx = q[i + 1];
      if (nx !== undefined && !/\s/.test(nx)) toks.push({ t: c === '-' ? 'NOT' : 'PLUS', pos: i, text: c === '-' ? 'NOT' : '+' });
      i++;
      continue;
    }
    let j = i;
    while (j < n && !/[\s()"|&]/.test(q[j])) j++;
    const w = q.slice(i, j);
    if (w === 'AND' || w === 'OR' || w === 'NOT') { toks.push({ t: w, pos: i, text: w }); i = j; continue; }
    const fm = /^([A-Za-z]+):/.exec(w);
    if (fm && FIELD_NAMES.has(fm[1].toLowerCase())) {
      const field = fm[1].toLowerCase() === 'tags' ? 'tag' : fm[1].toLowerCase();
      const rest = w.slice(fm[0].length);
      if (rest === '') {
        if (q[j] === '"') {
          const r = readPhrase(j);
          toks.push({ t: 'PHRASE', value: r.value, field, pos: i });
          i = r.next;
          continue;
        }
        throw new ParseError(`"${fm[1]}:" needs a word or a "quoted phrase" straight after it (character ${i + 1}).`, i);
      }
      toks.push({ t: 'WORD', value: rest, field, pos: i });
    } else {
      toks.push({ t: 'WORD', value: w, field: null, pos: i });
    }
    i = j;
  }
  return toks;
}

const startsOperand = (t) => !!t && (t.t === 'WORD' || t.t === 'PHRASE' || t.t === 'LP' || t.t === 'NOT' || t.t === 'PLUS');

function wordNode(tok) {
  let value = tok.value;
  let wildcard = false;
  if (/\*+$/.test(value)) { wildcard = true; value = value.replace(/\*+$/, ''); }
  if (wildcard) {
    const prefix = fold(value).replace(/[^\p{L}\p{N}-]/gu, '').replace(/^-+|-+$/g, '');
    if (!prefix) throw new ParseError(`A * wildcard needs at least one letter before it (character ${tok.pos + 1}).`, tok.pos);
    return { type: 'prefix', prefix, field: tok.field, pos: tok.pos };
  }
  const parts = tokenizeParts(value);
  if (parts.length === 0) return null;
  if (parts.length === 1) return { type: 'term', term: stem(parts[0]), raw: parts[0], field: tok.field, pos: tok.pos };
  return { type: 'phrase', terms: parts.map(stem), raw: parts.join(' '), field: tok.field, pos: tok.pos };
}

function phraseNode(tok) {
  const parts = tokenizeParts(tok.value);
  if (parts.length === 0) return null;
  if (parts.length === 1) return { type: 'term', term: stem(parts[0]), raw: parts[0], field: tok.field ?? null, pos: tok.pos, quoted: true };
  return { type: 'phrase', terms: parts.map(stem), raw: parts.join(' '), field: tok.field ?? null, pos: tok.pos };
}

function parseTokens(toks, query) {
  let k = 0;
  const peek = () => toks[k];
  const endPos = query.length;

  const unexpected = (t, what) => {
    if (!t) return new ParseError(`The search ends too early: I was expecting ${what}.`, endPos);
    if (t.t === 'RP') return new ParseError(`The closing bracket at character ${t.pos + 1} has no matching opening bracket.`, t.pos);
    if (t.t === 'AND' || t.t === 'OR') return new ParseError(`${t.text} needs a word or phrase before it (character ${t.pos + 1}).`, t.pos);
    return new ParseError(`I didn't expect that at character ${t.pos + 1}.`, t.pos);
  };

  function parseOr(depth) {
    const kids = [parseAnd(depth)];
    while (peek() && peek().t === 'OR') {
      const op = toks[k++];
      if (!startsOperand(peek())) throw new ParseError(`${op.text} needs a word or phrase after it (character ${op.pos + 1}).`, op.pos);
      kids.push(parseAnd(depth));
    }
    const live = kids.filter(Boolean);
    if (live.length === 0) return null;
    return live.length === 1 ? live[0] : { type: 'or', children: live };
  }

  function parseAnd(depth) {
    if (!startsOperand(peek())) throw unexpected(peek(), 'a word or phrase');
    const items = [];
    for (;;) {
      items.push(parseUnary(depth));
      const t = peek();
      if (t && t.t === 'AND') {
        k++;
        if (!startsOperand(peek())) throw new ParseError(`${t.text} needs a word or phrase after it (character ${t.pos + 1}).`, t.pos);
        continue;
      }
      if (startsOperand(t)) continue;
      break;
    }
    const live = items.filter(Boolean);
    if (live.length === 0) return null;
    return live.length === 1 ? live[0] : { type: 'and', children: live };
  }

  function parseUnary(depth) {
    const t = peek();
    if (t.t === 'NOT' || t.t === 'PLUS') {
      k++;
      if (!startsOperand(peek())) throw new ParseError(`${t.text} needs a word or phrase after it (character ${t.pos + 1}).`, t.pos);
      const child = parseUnary(depth);
      return child ? { type: t.t === 'NOT' ? 'not' : 'must', child, pos: t.pos } : null;
    }
    return parsePrimary(depth);
  }

  function parsePrimary(depth) {
    const t = toks[k];
    if (t.t === 'LP') {
      if (depth >= MAX_DEPTH) throw new ParseError(`Brackets are nested too deeply (more than ${MAX_DEPTH} levels) at character ${t.pos + 1}.`, t.pos);
      k++;
      if (peek() && peek().t === 'RP') throw new ParseError(`There is nothing inside the brackets at character ${t.pos + 1}.`, t.pos);
      const inner = parseOr(depth + 1);
      if (!peek() || peek().t !== 'RP') throw new ParseError(`The bracket opened at character ${t.pos + 1} is never closed. Add a ) or remove the (.`, t.pos);
      k++;
      return inner;
    }
    k++;
    return t.t === 'WORD' ? wordNode(t) : phraseNode(t);
  }

  const ast = parseOr(0);
  if (k < toks.length) throw unexpected(toks[k], 'the end of the search');
  return ast;
}

/** Parse a query string into an AST. Throws ParseError (with .position) on malformed input. */
export function parse(query) {
  const q = typeof query === 'string' ? query : String(query ?? '');
  if (q.length > MAX_QUERY_LENGTH) throw new ParseError(`That search is longer than ${MAX_QUERY_LENGTH} characters. Please shorten it.`, MAX_QUERY_LENGTH);
  const toks = lex(q);
  if (toks.length === 0) return { type: 'empty' };
  return parseTokens(toks, q) ?? { type: 'empty' };
}

/** Evaluate an AST against a SearchIndex. Returns the Set of matching doc ids. */
export function evaluate(ast, index) {
  const universe = () => new Set(index.ids());
  const fieldOf = (f) => (f ? resolveField(f) : null);
  function ev(node) {
    switch (node.type) {
      case 'empty': return new Set();
      case 'term': return index.docsWith(node.term, fieldOf(node.field));
      case 'prefix': {
        const f = fieldOf(node.field);
        const out = new Set();
        for (const t of index.prefixTerms(node.prefix, f)) for (const id of index.docsWith(t, f)) out.add(id);
        return out;
      }
      case 'phrase': {
        const f = fieldOf(node.field);
        if (f) return new Set(index.phraseMatches(node.terms, f).keys());
        const out = new Set();
        for (const fld of SCORE_FIELDS) for (const id of index.phraseMatches(node.terms, fld).keys()) out.add(id);
        return out;
      }
      case 'or': {
        const out = new Set();
        for (const c of node.children) for (const id of ev(c)) out.add(id);
        return out;
      }
      case 'not': {
        const out = universe();
        for (const id of ev(node.child)) out.delete(id);
        return out;
      }
      case 'must': return ev(node.child);
      case 'and': {
        const must = [], pos = [], neg = [];
        for (const c of node.children) {
          if (c.type === 'not') neg.push(c.child);
          else if (c.type === 'must') must.push(c.child);
          else pos.push(c);
        }
        const base = must.length ? must : pos;
        let result = null;
        if (base.length === 0) result = universe();
        else {
          const sets = base.map(ev).sort((a, b) => a.size - b.size);
          result = new Set(sets[0]);
          for (let i = 1; i < sets.length && result.size; i++) for (const id of [...result]) if (!sets[i].has(id)) result.delete(id);
        }
        for (const n of neg) { if (!result.size) break; for (const id of ev(n)) result.delete(id); }
        return result;
      }
      default: return new Set();
    }
  }
  if (!ast) return new Set();
  return ev(ast);
}

/** Positive (non-negated) terms of an AST, with prefixes expanded against the index: [{ term, field }]. Used for ranking. */
export function collectTerms(ast, index) {
  const seen = new Map();
  const add = (term, field) => { const k = `${field || ''}␟${term}`; if (!seen.has(k)) seen.set(k, { term, field: field || null }); };
  (function walk(n) {
    if (!n) return;
    switch (n.type) {
      case 'term': add(n.term, n.field); break;
      case 'phrase': for (const t of n.terms) add(t, n.field); break;
      case 'prefix': for (const t of index.prefixTerms(n.prefix, resolveField(n.field))) add(t, n.field); break;
      case 'and': case 'or': n.children.forEach(walk); break;
      case 'must': walk(n.child); break;
      default: break; // not, empty
    }
  })(ast);
  return [...seen.values()];
}
