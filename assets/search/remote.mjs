// SolrSearch: an OFF-BY-DEFAULT adapter for the reference stack's Solr read model, reached only through the gateway's
// GET /search (the browser never talks to Solr). Nothing in the page imports this file, the in-browser engine is
// unchanged, and constructing the adapter makes no request; fetch happens only when search() is called.
//
//   const solr = solrFromConfig({ solr: { enabled: true, url: 'http://localhost:8088' } });   // null unless enabled
//   const out = await solr?.search('title:spine', { algorithm: 'boolean', limit: 10 });
//
// Result shape matches search(): { results, total, algorithm, error }. A 400 from the gateway (malformed boolean
// syntax, a refused query) comes back as { error } with no results, like the in-browser engine's friendly errors;
// anything else (gateway or Solr down) throws.
export const SOLR_ALGORITHMS = ['boolean', 'bm25', 'fuzzy', 'prefix', 'phrase'];
const trimSlash = (u) => String(u).replace(/\/+$/, '');
const defaultFetch = (...a) => globalThis.fetch(...a);

export class SolrSearch {
  constructor({ url, fetch: f = defaultFetch } = {}) {
    if (!url) throw new Error('SolrSearch: the gateway url is required');
    this.url = trimSlash(url);
    this.fetch = f;
  }

  async search(query, { algorithm = 'bm25', limit = 10, start = 0 } = {}) {
    if (!SOLR_ALGORITHMS.includes(algorithm)) throw new Error(`SolrSearch: unknown algorithm "${algorithm}"`);
    const rows = Math.min(100, Math.max(1, Math.floor(Number(limit)) || 10));
    const params = new URLSearchParams({ q: String(query ?? ''), algorithm, rows: String(rows), start: String(Math.max(0, Math.floor(Number(start)) || 0)) });
    const url = `${this.url}/search?${params}`;
    let res;
    try {
      res = await this.fetch(url, { method: 'GET', headers: { Accept: 'application/json' } });
    } catch (err) {
      throw new Error(`SolrSearch: network error calling GET ${url}: ${err?.message ?? err}`);
    }
    let body = null;
    try { body = await res.json(); } catch { /* not JSON */ }
    if (res.status === 400) return { results: [], total: 0, algorithm, error: body?.error ?? 'The search was refused.', position: body?.position ?? null };
    if (!res.ok) throw new Error(`SolrSearch: GET ${url} failed with HTTP ${res.status}${body?.error ? ': ' + body.error : ''}`);
    return { results: body?.results ?? [], total: body?.total ?? 0, algorithm: body?.algorithm ?? algorithm, qtimeMs: body?.qtimeMs ?? null, solrQuery: body?.solrQuery ?? null, error: null };
  }
}

/** null unless the config switches Solr on: { solr: { enabled: true, url } }. */
export function solrFromConfig(config, opts = {}) {
  const c = config?.solr;
  return c && c.enabled === true && c.url ? new SolrSearch({ url: c.url, ...opts }) : null;
}
