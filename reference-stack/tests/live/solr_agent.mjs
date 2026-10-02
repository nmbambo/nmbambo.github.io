// The JS SearchAgent drives Solr: for each query the agent proposes an algorithm (Thompson sampling over the same
// vocabulary features the page uses), then the gateway's GET /search is called with that algorithm. Reports what the
// agent proposed and why, how many results Solr returned, and the round-trip latency.
//   node solr_agent.mjs [--gateway http://localhost:8088] [--seed 1] [--json out.json]
import { readFileSync, writeFileSync } from 'node:fs';
import { SearchIndex } from '../../../assets/search/index.mjs';
import { SearchAgent, seededRng } from '../../../assets/search/agent.mjs';
import { ALGORITHMS } from '../../../assets/search/algorithms.mjs';
import { search as jsSearch } from '../../../assets/search/search.mjs';
import { SolrSearch } from '../../../assets/search/remote.mjs';

const arg = (name, dflt) => { const i = process.argv.indexOf(name); return i > 0 ? process.argv[i + 1] : dflt; };
const gateway = arg('--gateway', 'http://localhost:8088');
const seed = Number(arg('--seed', '1'));
const here = new URL('.', import.meta.url);
const corpus = JSON.parse(readFileSync(new URL('../../../data/corpus.json', here), 'utf8'));
const queries = JSON.parse(readFileSync(new URL('solr_queries.json', here), 'utf8'));
const all = [...queries.fixture, ...queries.own, 'a AND', '"unclosed', '{!dismax qf=text}x', ''];

const index = new SearchIndex(corpus);
const agent = new SearchAgent({ algorithms: ALGORITHMS, rng: seededRng(seed) });
const solr = new SolrSearch({ url: gateway });

const rows = [];
for (const q of all) {
  const choice = agent.choose(q, index);
  const t0 = performance.now();
  let out, failure = null;
  try { out = await solr.search(q, { algorithm: choice.algorithm, limit: 10 }); } catch (e) { failure = e.message; }
  const ms = performance.now() - t0;
  const js = jsSearch(q, index, new SearchAgent({ algorithms: ALGORITHMS, rng: seededRng(seed) }), { limit: 10 });
  rows.push({
    query: q, proposed: choice.algorithm, key: choice.features?.key ?? null, explanation: choice.explanation,
    solr: failure ? { failure } : { total: out.total, error: out.error, top3: out.results.slice(0, 3).map((r) => r.title), solrQuery: out.solrQuery, qtimeMs: out.qtimeMs },
    roundTripMs: Math.round(ms * 10) / 10,
    jsTotal: js.total, jsTop3: js.results.slice(0, 3).map((r) => r.title),
  });
}
for (const r of rows) {
  const s = r.solr.failure ? `FAILED ${r.solr.failure}` : r.solr.error ? `refused: ${r.solr.error}` : `${r.solr.total} results, top: ${r.solr.top3[0] ?? '-'}`;
  console.log(`${JSON.stringify(r.query).padEnd(44)} -> ${r.proposed.padEnd(8)} ${String(r.roundTripMs).padStart(6)} ms  solr: ${s}  | js total ${r.jsTotal}`);
}
const lat = rows.map((r) => r.roundTripMs).sort((a, b) => a - b);
console.log(`round trips: n=${lat.length} median ${lat[Math.floor(lat.length / 2)]} ms, max ${lat[lat.length - 1]} ms`);
const jf = arg('--json', null);
if (jf) writeFileSync(jf, JSON.stringify({ gateway, seed, rows }, null, 2));
