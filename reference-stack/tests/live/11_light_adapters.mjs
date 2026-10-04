// Live: the site's own adapters against the light stack, with Node's real fetch. GatewayStore -> command (8090),
// RemoteSearch -> query (8091). Run after `docker compose up -d` and 10_light.py.
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { fromConfig } from '../../../assets/es/adapters.mjs';
import { searchFromConfig } from '../../../assets/search/remote.mjs';

const cfg = { broker: 'local', remoteStore: { url: 'http://127.0.0.1:8090' }, search: { enabled: true, url: 'http://127.0.0.1:8091' } };
const { remoteStore } = fromConfig(cfg);
const remote = searchFromConfig(cfg);
const id = randomUUID();
const stream = `content-live/${id.slice(0, 8)}`;
const ev = { id, type: 'ContentAdded', stream, data: { id: `live#${id.slice(0, 8)}`, title: 'Aardwolf adapter probe', text: 'light stack probe' }, meta: { ts: new Date().toISOString(), schema: 1, source: 'live-test' } };
const r1 = await remoteStore.append(stream, [ev], { expectedVersion: -1 });
const r2 = await remoteStore.append(stream, [ev]);               // same id again: de-duplicated
console.log('append', JSON.stringify(r1), 'again', JSON.stringify(r2));
let hit;
for (let i = 0; i < 40 && !hit?.total; i++) { hit = await remote.search('aardwolf', { algorithm: 'bm25' }); if (!hit.total) await new Promise((r) => setTimeout(r, 250)); }
assert.equal(hit.total, 1); assert.equal(hit.results[0].id, ev.data.id);
const bad = await remote.search('(a AND', { algorithm: 'boolean' });
assert.ok(bad.error && bad.results.length === 0);
const sub = await remote.search('rdwol', { algorithm: 'substring' });
assert.equal(sub.total, 1);
console.log('PASS GatewayStore -> command -> JetStream -> query -> RemoteSearch round trip; 400 -> friendly error; substring');
