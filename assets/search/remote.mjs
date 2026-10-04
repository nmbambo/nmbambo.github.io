// RemoteSearch: an OFF-BY-DEFAULT adapter for a server-side read model behind one contract, GET /search?q&algorithm&rows&start.
// Two read models answer it: the light stack's query service (SQLite FTS5, the default) and, in the heavy profile, Solr
// through the gateway. The browser never talks to a database. Nothing in the page imports this file, the in-browser engine
// is unchanged, and constructing the adapter makes no request; fetch happens only when search() is called.
//
//   const remote = searchFromConfig({ search: { enabled: true, url: 'http://localhost:8091' } });   // null unless enabled
//   const out = await remote?.search('title:spine', { algorithm: 'boolean', limit: 10 });
//
// SolrSearch / solrFromConfig / SOLR_ALGORITHMS remain as the names the Solr profile was written against.
//
// Result shape matches search(): { results, total, algorithm, error }. A 400 from the gateway (malformed boolean
// syntax, a refused query) comes back as { error } with no results, like the in-browser engine's friendly errors;
// anything else (gateway or Solr down) throws.
export const SOLR_ALGORITHMS = ['boolean', 'bm25', 'fuzzy', 'prefix', 'phrase'];
/** The query service adds `substring` (FTS5 trigram index). */
export const REMOTE_ALGORITHMS = [...SOLR_ALGORITHMS, 'substring'];
const trimSlash = (u) => String(u).replace(/\/+$/, '');
const defaultFetch = (...a) => globalThis.fetch(...a);

export class RemoteSearch {
  constructor({ url, fetch: f = defaultFetch, algorithms = REMOTE_ALGORITHMS, name = 'RemoteSearch' } = {}) {
    if (!url) throw new Error(`${name}: the gateway url is required`);
    this.url = trimSlash(url);
    this.fetch = f;
    this.algorithms = algorithms;
    this.name = name;
  }

  async search(query, { algorithm = 'bm25', limit = 10, start = 0 } = {}) {
    if (!this.algorithms.includes(algorithm)) throw new Error(`${this.name}: unknown algorithm "${algorithm}"`);
    const rows = Math.min(100, Math.max(1, Math.floor(Number(limit)) || 10));
    const params = new URLSearchParams({ q: String(query ?? ''), algorithm, rows: String(rows), start: String(Math.max(0, Math.floor(Number(start)) || 0)) });
    const url = `${this.url}/search?${params}`;
    let res;
    try {
      res = await this.fetch(url, { method: 'GET', headers: { Accept: 'application/json' } });
    } catch (err) {
      throw new Error(`${this.name}: network error calling GET ${url}: ${err?.message ?? err}`);
    }
    let body = null;
    try { body = await res.json(); } catch { /* not JSON */ }
    if (res.status === 400) return { results: [], total: 0, algorithm, error: body?.error ?? 'The search was refused.', position: body?.position ?? null };
    if (!res.ok) throw new Error(`${this.name}: GET ${url} failed with HTTP ${res.status}${body?.error ? ': ' + body.error : ''}`);
    return { results: body?.results ?? [], total: body?.total ?? 0, algorithm: body?.algorithm ?? algorithm, qtimeMs: body?.qtimeMs ?? null, solrQuery: body?.solrQuery ?? null, ...(body?.ftsQuery !== undefined ? { ftsQuery: body.ftsQuery } : {}), error: null };
  }
}

/** The Solr read model (heavy profile): same contract, five methods. */
export class SolrSearch extends RemoteSearch {
  constructor(opts = {}) { super({ algorithms: SOLR_ALGORITHMS, name: 'SolrSearch', ...opts }); }
}

/** null unless the config switches remote search on: { search: { enabled: true, url } } (query service). */
export function searchFromConfig(config, opts = {}) {
  const c = config?.search;
  return c && c.enabled === true && c.url ? new RemoteSearch({ url: c.url, ...opts }) : null;
}

/** null unless the config switches Solr on: { solr: { enabled: true, url } }. */
export function solrFromConfig(config, opts = {}) {
  const c = config?.solr;
  return c && c.enabled === true && c.url ? new SolrSearch({ url: c.url, ...opts }) : null;
}
