import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { SolrSearch, solrFromConfig, SOLR_ALGORITHMS } from '../../assets/search/remote.mjs';

const fakeFetch = (responses) => {
  const calls = [];
  const f = async (url, init) => {
    calls.push({ url, init });
    const r = responses.shift();
    if (r instanceof Error) throw r;
    return { ok: r.status < 400, status: r.status, json: async () => r.body };
  };
  f.calls = calls;
  return f;
};

test('off by default: no config, disabled or url-less config give null, and nothing is fetched', () => {
  const f = fakeFetch([]);
  assert.equal(solrFromConfig(undefined, { fetch: f }), null);
  assert.equal(solrFromConfig({}, { fetch: f }), null);
  assert.equal(solrFromConfig({ solr: { url: 'http://x' } }, { fetch: f }), null);
  assert.equal(solrFromConfig({ solr: { enabled: false, url: 'http://x' } }, { fetch: f }), null);
  assert.equal(solrFromConfig({ solr: { enabled: true } }, { fetch: f }), null);
  assert.ok(solrFromConfig({ solr: { enabled: true, url: 'http://x' } }, { fetch: f }) instanceof SolrSearch);
  assert.equal(f.calls.length, 0);
});

test('the shipped es/config.json and the page do not switch Solr on or import the adapter', () => {
  const cfg = JSON.parse(readFileSync(new URL('../../assets/es/config.json', import.meta.url), 'utf8'));
  assert.equal(solrFromConfig(cfg), null);
  for (const f of ['../../assets/search/app.mjs', '../../assets/search/search.mjs', '../../assets/search/algorithms.mjs']) {
    assert.ok(!/remote\.mjs|SolrSearch/.test(readFileSync(new URL(f, import.meta.url), 'utf8')), f);
  }
});

test('constructing makes no request; search calls GET /search with q, algorithm, rows, start only', async () => {
  const f = fakeFetch([{ status: 200, body: { algorithm: 'boolean', total: 1, qtimeMs: 4, solrQuery: '(+a +b)', results: [{ id: 'x#1', score: 2 }] } }]);
  const s = new SolrSearch({ url: 'http://localhost:8088/', fetch: f });
  assert.equal(f.calls.length, 0);
  const out = await s.search('a AND b & "c d"', { algorithm: 'boolean', limit: 7, start: 3 });
  const u = new URL(f.calls[0].url);
  assert.equal(u.origin + u.pathname, 'http://localhost:8088/search');
  assert.deepEqual([...u.searchParams.keys()].sort(), ['algorithm', 'q', 'rows', 'start']);
  assert.equal(u.searchParams.get('q'), 'a AND b & "c d"');
  assert.equal(u.searchParams.get('rows'), '7');
  assert.equal(u.searchParams.get('start'), '3');
  assert.equal(f.calls[0].init.method, 'GET');
  assert.deepEqual(Object.keys(f.calls[0].init.headers), ['Accept']);   // no credentials, no custom headers
  assert.deepEqual(out, { results: [{ id: 'x#1', score: 2 }], total: 1, algorithm: 'boolean', qtimeMs: 4, solrQuery: '(+a +b)', error: null });
});

test('defaults and clamping', async () => {
  const f = fakeFetch([{ status: 200, body: {} }, { status: 200, body: {} }, { status: 200, body: {} }]);
  const s = new SolrSearch({ url: 'http://g', fetch: f });
  await s.search('x');
  await s.search('x', { limit: 5000, start: -4 });
  await s.search('x', { limit: 0 });
  const p = (i) => new URL(f.calls[i].url).searchParams;
  assert.deepEqual([p(0).get('algorithm'), p(0).get('rows'), p(0).get('start')], ['bm25', '10', '0']);
  assert.deepEqual([p(1).get('rows'), p(1).get('start')], ['100', '0']);
  assert.equal(p(2).get('rows'), '10');
});

test('a 400 becomes a friendly error with no results; other failures throw', async () => {
  const s = new SolrSearch({ url: 'http://g', fetch: fakeFetch([
    { status: 400, body: { error: 'a AND needs a word after it', position: 2 } },
    { status: 503, body: { error: 'search backend unavailable' } },
    new Error('connection refused'),
  ]) });
  assert.deepEqual(await s.search('a AND', { algorithm: 'boolean' }), { results: [], total: 0, algorithm: 'boolean', error: 'a AND needs a word after it', position: 2 });
  await assert.rejects(() => s.search('x'), /HTTP 503: search backend unavailable/);
  await assert.rejects(() => s.search('x'), /network error.*connection refused/);
});

test('rejects unknown algorithms before any request and requires a url', async () => {
  const f = fakeFetch([]);
  await assert.rejects(() => new SolrSearch({ url: 'http://g', fetch: f }).search('x', { algorithm: 'semantic' }), /unknown algorithm/);
  assert.equal(f.calls.length, 0);
  assert.throws(() => new SolrSearch({ fetch: f }), /url is required/);
  assert.deepEqual(SOLR_ALGORITHMS, ['boolean', 'bm25', 'fuzzy', 'prefix', 'phrase']);
});

test('RemoteSearch: off by default, adds substring, passes the query service ftsQuery through', async () => {
  const { RemoteSearch, searchFromConfig, REMOTE_ALGORITHMS } = await import('../../assets/search/remote.mjs');
  const cfg = JSON.parse(readFileSync(new URL('../../assets/es/config.json', import.meta.url), 'utf8'));
  assert.equal(searchFromConfig(cfg), null);
  assert.equal(searchFromConfig({ search: { url: 'http://x' } }), null);
  assert.deepEqual(REMOTE_ALGORITHMS, ['boolean', 'bm25', 'fuzzy', 'prefix', 'phrase', 'substring']);
  const f = fakeFetch([{ status: 200, body: { algorithm: 'substring', total: 1, qtimeMs: 1, ftsQuery: null, results: [{ id: 'a', score: 1 }] } }]);
  const r = searchFromConfig({ search: { enabled: true, url: 'http://localhost:8091' } }, { fetch: f });
  assert.ok(r instanceof RemoteSearch);
  const out = await r.search('herenc', { algorithm: 'substring' });
  assert.equal(new URL(f.calls[0].url).pathname, '/search');
  assert.equal(out.total, 1);
  assert.ok('ftsQuery' in out);
  await assert.rejects(() => new SolrSearch({ url: 'http://x', fetch: f }).search('a', { algorithm: 'substring' }), /SolrSearch: unknown algorithm/);
});
