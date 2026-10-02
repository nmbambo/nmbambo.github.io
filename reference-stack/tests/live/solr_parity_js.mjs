// Runs the site's in-browser engine (assets/search/*.mjs) from Node over data/corpus.json for a fixed query set and
// prints, for every algorithm, the top-N result ids and scores as JSON. Used by 08_solr_parity.py.
//   node solr_parity_js.mjs [--top 10] > js_results.json
import { readFileSync } from 'node:fs';
import { SearchIndex } from '../../../assets/search/index.mjs';
import { ALGORITHMS } from '../../../assets/search/algorithms.mjs';

const here = new URL('.', import.meta.url);
const corpus = JSON.parse(readFileSync(new URL('../../../data/corpus.json', here), 'utf8'));
const queries = JSON.parse(readFileSync(new URL('solr_queries.json', here), 'utf8'));
const top = Number(process.argv[process.argv.indexOf('--top') + 1]) || 10;
const index = new SearchIndex(corpus);

const out = { docs: corpus.length, indexSize: index.size, top, queries: [...queries.fixture, ...queries.own], results: {} };
for (const algo of ALGORITHMS) {
  out.results[algo.name] = {};
  for (const q of out.queries) {
    try {
      const ranked = algo.run(q, index);
      out.results[algo.name][q] = { total: ranked.length, top: ranked.slice(0, top).map((r) => ({ id: r.id, score: r.score })) };
    } catch (e) {
      out.results[algo.name][q] = { error: e.message, total: 0, top: [] };
    }
  }
}
console.log(JSON.stringify(out));
