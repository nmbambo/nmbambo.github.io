import test from 'node:test';
import assert from 'node:assert/strict';
import { parse, evaluate, ParseError, collectTerms } from '../../assets/search/boolean.mjs';
import { SearchIndex } from '../../assets/search/index.mjs';
import { FIXTURE_DOCS } from './fixtures.mjs';

const ix = new SearchIndex(FIXTURE_DOCS);
const ids = (q) => [...evaluate(parse(q), ix)].sort();

test('decision AND (warranty OR canvas)', () => {
  assert.deepEqual(ids('decision AND (warranty OR canvas)'), ['architecture#e10', 'work#canvas']);
  assert.deepEqual(ids('decision & (warranty | canvas)'), ['architecture#e10', 'work#canvas']);
  assert.deepEqual(ids('decision && (warranty || canvas)'), ['architecture#e10', 'work#canvas']);
});

test('quoted phrase is positional', () => {
  assert.deepEqual(ids('"human in the loop"'), ['engage#e3', 'method#e5']);
  assert.deepEqual(ids('"loop human"'), []);
  assert.deepEqual(ids('"the loop"'), ['engage#e3', 'method#e5']);
});

test('field queries', () => {
  assert.deepEqual(ids('title:spine'), ['architecture#e1', 'architecture#e2']);
  assert.deepEqual(ids('TITLE:spine'), ['architecture#e1', 'architecture#e2']);
  assert.deepEqual(ids('title:"disciplined decision"'), ['method#e3']);
  assert.deepEqual(ids('section:instrument'), ['work#canvas', 'work#coverage']);
  assert.deepEqual(ids('type:glossary'), ['method#e8']);
  assert.deepEqual(ids('tag:warranty'), ['architecture#e10', 'work#coverage']);
  assert.deepEqual(ids('tags:warranty'), ['architecture#e10', 'work#coverage']);
  assert.deepEqual(ids('source:site').length, FIXTURE_DOCS.length);
  assert.deepEqual(ids('tag:decision-ir'), ['architecture#e7']);
});

test('field restricts where the term may match', () => {
  assert.deepEqual(ids('title:warranty'), ['architecture#e10']);
  assert.ok(ids('warranty').length > 1);
  assert.deepEqual(ids('tag:spine title:planes'), ['architecture#e2']);
});

test('NOT, minus and bang', () => {
  assert.deepEqual(ids('solver -quantum'), ['architecture#e7']);
  assert.deepEqual(ids('solver NOT quantum'), ['architecture#e7']);
  assert.deepEqual(ids('solver !quantum'), ['architecture#e7']);
  const notPricing = ids('NOT pricing');
  assert.equal(notPricing.length, FIXTURE_DOCS.length - 1);
  assert.ok(!notPricing.includes('engage#e5'));
  assert.deepEqual(ids('NOT NOT pricing'), ['engage#e5']);
  assert.deepEqual(ids('-pricing decision'), ids('decision NOT pricing'));
});

test('+term is required; unmarked terms only rank', () => {
  assert.deepEqual(ids('+moloto framework'), ['method#e3']);
  assert.deepEqual(ids('+pricing +writing'), ['engage#e5']);
  const withPlus = ids('+decision warranty');
  assert.ok(withPlus.length > ids('decision warranty').length);
  assert.ok(withPlus.includes('home#e2'), 'doc has decision but not warranty');
  const terms = collectTerms(parse('+moloto framework'), ix).map((t) => t.term);
  assert.deepEqual(terms.sort(), ['framework', 'moloto']);
});

test('wildcards', () => {
  assert.deepEqual(ids('warrant*'), ['architecture#e10', 'work#coverage']);
  assert.deepEqual(ids('tag:decision*'), ids('tag:decision OR tag:decision-ir'));
  assert.deepEqual(ids('zzzz*'), []);
});

test('tag:instrument OR type:note', () => {
  assert.deepEqual(ids('tag:instrument OR type:note'), ['notes#hiring', 'notes#time', 'work#canvas', 'work#coverage']);
});

test('precedence: OR < AND < NOT', () => {
  assert.deepEqual(ids('canvas OR warranty AND decision'), ids('canvas OR (warranty AND decision)'));
  assert.deepEqual(ids('NOT warranty OR canvas'), ids('(NOT warranty) OR canvas'));
  assert.notDeepEqual(ids('NOT warranty OR canvas'), ids('NOT (warranty OR canvas)'));
});

test('nested parentheses', () => {
  const q = '((decision AND (warranty OR (canvas AND instrument))) OR (tag:quantum AND (solver OR "hash chain")))';
  assert.deepEqual(ids(q), ['architecture#e10', 'architecture#e12', 'work#canvas']);
  assert.deepEqual(ids('((((warranty))))'), ids('warranty'));
});

test('lowercase and/or/not are ordinary words', () => {
  const ast = parse('human and loop');
  assert.equal(ast.type, 'and');
  assert.deepEqual(ast.children.map((c) => c.term), ['human', 'and', 'loop']);
  const o = parse('canvas or warranty');
  assert.equal(o.type, 'and');
  assert.equal(parse('not').type, 'term');
});

test('empty and punctuation-only queries parse to an empty AST', () => {
  assert.equal(parse('').type, 'empty');
  assert.equal(parse('   ').type, 'empty');
  assert.equal(parse('???').type, 'empty');
  assert.equal(evaluate(parse(''), ix).size, 0);
});

test('hyphenated word becomes a phrase of its parts', () => {
  const ast = parse('human-in-the-loop');
  assert.equal(ast.type, 'phrase');
  assert.deepEqual(ast.terms, ['human', 'in', 'the', 'loop']);
});

test('ParseError carries position for malformed input', () => {
  const cases = [
    ['"human in the loop', 0, /never closed/],
    ['decision "abc', 9, /quote/],
    ['(decision OR warranty', 0, /never closed/],
    ['decision OR warranty)', 20, /no matching opening/],
    ['a AND (b OR (c AND d)', 6, /never closed/],
    ['decision AND', 9, /AND needs/],
    ['OR decision', 0, /OR needs/],
    ['decision OR OR canvas', 9, /OR needs/],
    ['NOT', 0, /NOT needs/],
    ['title:', 0, /needs a word/],
    ['title: spine', 0, /needs a word/],
    ['()', 0, /nothing inside/],
    ['""', 0, /nothing to search/],
    ['*', 0, /wildcard/],
    [')', 0, /no matching/],
  ];
  for (const [q, pos, re] of cases) {
    assert.throws(() => parse(q), (e) => {
      assert.ok(e instanceof ParseError, q);
      assert.equal(e.name, 'ParseError');
      assert.equal(e.position, pos, `${q} position`);
      assert.match(e.message, re, q);
      return true;
    }, q);
  }
});

test('absurd input is rejected politely, never by stack overflow', () => {
  assert.throws(() => parse('('.repeat(500) + 'a' + ')'.repeat(500)), ParseError);
  assert.throws(() => parse('a '.repeat(2000)), ParseError);
});

test('unknown field prefixes are plain words', () => {
  const ast = parse('http:thing');
  assert.notEqual(ast.type, 'empty');
});
