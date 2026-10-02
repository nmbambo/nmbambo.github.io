import { test } from 'node:test';
import assert from 'node:assert/strict';
import { makeEvent } from '../../assets/es/events.mjs';
import { ConcurrencyError } from '../../assets/es/store.mjs';
import mitt from '../../assets/vendor/mitt.mjs';
import { LocalBroker, KafkaRestBroker, SolaceRestBroker, GatewayStore, fromConfig } from '../../assets/es/adapters.mjs';
import { readFileSync } from 'node:fs';

const ev = makeEvent({ type: 'SearchSubmitted', stream: 'search-1', data: { query: 'x' } });
const fakeFetch = (responses = []) => {
  const calls = [];
  const f = async (url, init) => {
    calls.push({ url, init });
    const r = responses.shift() ?? { status: 200, body: {} };
    return { ok: r.status < 400, status: r.status, json: async () => r.body, text: async () => JSON.stringify(r.body) };
  };
  f.calls = calls;
  return f;
};

test('LocalBroker publishes and subscribes on the bus', async () => {
  const bus = mitt(); const b = new LocalBroker(bus); const got = [];
  const off = b.subscribe('t', (e) => got.push(e));
  await b.publish('t', ev); off(); await b.publish('t', ev);
  assert.deepEqual(got, [ev]);
});

test('KafkaRestBroker URL and body', async () => {
  const f = fakeFetch();
  const k = new KafkaRestBroker({ url: 'http://k:8082/', clusterId: 'c1', fetch: f });
  await k.publish('events', ev);
  const { url, init } = f.calls[0];
  assert.equal(url, 'http://k:8082/v3/clusters/c1/topics/ingqiqo.events/records');
  assert.equal(init.method, 'POST');
  assert.equal(init.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(init.body), { key: { type: 'STRING', data: 'search-1' }, value: { type: 'JSON', data: ev } });
});

test('SolaceRestBroker URL, headers and body', async () => {
  const f = fakeFetch();
  const s = new SolaceRestBroker({ url: 'http://s:9000', vpn: 'dev', fetch: f });
  await s.publish('ingqiqo/events/x', ev);
  const { url, init } = f.calls[0];
  assert.equal(url, 'http://s:9000/TOPIC/ingqiqo/events/x');
  assert.equal(init.headers['Content-Type'], 'application/json');
  assert.equal(init.headers['Solace-Message-VPN'], undefined); // VPN is selected by port, not header
  assert.deepEqual(JSON.parse(init.body), ev);
});

test('GatewayStore URLs and bodies', async () => {
  const f = fakeFetch([{ status: 200, body: { appended: 1, positions: [0] } }, { status: 200, body: [ev] }, { status: 200, body: { position: 7 } }, { status: 404, body: {} }]);
  const g = new GatewayStore({ url: 'http://g/', fetch: f });
  assert.deepEqual(await g.append('search-1', [ev], { expectedVersion: -1 }), { appended: 1, positions: [0] });
  assert.equal(f.calls[0].url, 'http://g/streams/search-1');
  assert.equal(f.calls[0].init.method, 'POST');
  assert.deepEqual(JSON.parse(f.calls[0].init.body), { events: [ev], expectedVersion: -1 });
  assert.deepEqual(await g.read({ fromPosition: 3, limit: 10 }), [ev]);
  assert.equal(f.calls[1].url, 'http://g/events?from=3&limit=10');
  assert.equal(f.calls[1].init.method, 'GET');
  assert.equal(await g.lastPosition(), 7);
  assert.equal(await g.has('abc'), false);
});

test('errors surface as rejected promises with clear messages', async () => {
  const g = new GatewayStore({ url: 'http://g', fetch: fakeFetch([{ status: 409, body: 'conflict' }, { status: 500, body: 'boom' }]) });
  await assert.rejects(g.append('s', [ev], { expectedVersion: 3 }), ConcurrencyError);
  await assert.rejects(g.read(), /GatewayStore: GET http:\/\/g\/events\?from=0 failed with HTTP 500/);
  const down = async () => { throw new Error('offline'); };
  await assert.rejects(new KafkaRestBroker({ url: 'http://k', clusterId: 'c', fetch: down }).publish('t', ev), /network error.*offline/);
  assert.throws(() => new KafkaRestBroker({ url: 'http://k' }), /clusterId/);
  assert.throws(() => new KafkaRestBroker({ url: 'http://k', clusterId: 'c' }).subscribe('t', () => {}), /not supported/);
});

test('fromConfig: default config yields local broker and no remote store', () => {
  const cfg = JSON.parse(readFileSync(new URL('../../assets/es/config.json', import.meta.url)));
  assert.deepEqual(cfg, { broker: 'local', remoteStore: null });
  const r = fromConfig(cfg, { bus: mitt() });
  assert.ok(r.broker instanceof LocalBroker);
  assert.equal(r.remoteStore, null);
  const r2 = fromConfig({ broker: { type: 'kafka-rest', url: 'http://k', clusterId: 'c' }, remoteStore: { url: 'http://g' } });
  assert.ok(r2.broker instanceof KafkaRestBroker);
  assert.ok(r2.remoteStore instanceof GatewayStore);
  assert.throws(() => fromConfig({ broker: { type: 'nope' } }), /unknown broker/);
});
