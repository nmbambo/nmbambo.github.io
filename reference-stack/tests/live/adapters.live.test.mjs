// Check 5 (Node half): the browser's own adapter classes, unmodified, over real HTTP (Node's global fetch)
// against the running gateway and Confluent REST proxy.   Run via 05_adapters.py, or directly:
//   GATEWAY_URL=http://localhost:8088 REST_URL=http://localhost:8082 node --test reference-stack/tests/live/adapters.live.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { GatewayStore, KafkaRestBroker, fromConfig } from '../../../assets/es/adapters.mjs';
import { makeEvent, deterministicId } from '../../../assets/es/events.mjs';
import { ConcurrencyError } from '../../../assets/es/store.mjs';
import { Projector } from '../../../assets/es/projector.mjs';

const GATEWAY_URL = process.env.GATEWAY_URL ?? 'http://localhost:8088';
const REST_URL = process.env.REST_URL ?? 'http://localhost:8082';
const rid = Math.random().toString(16).slice(2, 10);
const here = dirname(fileURLToPath(import.meta.url));
const published = { rid, topicTest: null, projectorDoc: null };

const health = await (await fetch(`${GATEWAY_URL}/health`)).json();
const clusterId = health.kafkaClusterId;
const store = new GatewayStore({ url: GATEWAY_URL });

test('gateway is up and reports a Kafka cluster id', () => {
  assert.equal(health.status, 'ok');
  assert.ok(clusterId, 'kafkaClusterId missing from /health');
});

test('GatewayStore.append: new stream, idempotent re-append, ConcurrencyError on 409', async () => {
  const stream = `content-liveadapters${rid}`;
  const evs = [0, 1, 2].map((i) => makeEvent({ type: 'ContentAdded', stream, data: { id: `live-adapters-${rid}#${i}`, title: `t${i}` } }));
  const r1 = await store.append(stream, evs, { expectedVersion: -1 });
  assert.equal(r1.appended, 3);
  assert.equal(r1.positions.length, 3);
  const r2 = await store.append(stream, evs, { expectedVersion: -1 });
  assert.equal(r2.appended, 0, 're-post must be a no-op');
  const fresh = makeEvent({ type: 'ContentChanged', stream, data: { id: `live-adapters-${rid}#0`, title: 'changed' } });
  await assert.rejects(store.append(stream, [fresh], { expectedVersion: -1 }), (e) => e instanceof ConcurrencyError);
  await assert.rejects(store.append(stream, [fresh], { expectedVersion: 7 }), (e) => e instanceof ConcurrencyError);
  const r3 = await store.append(stream, [fresh], { expectedVersion: 2 });
  assert.equal(r3.appended, 1);
  const ids = [...evs, fresh].map((e) => e.id);
  const back = await store.readStream(stream);
  assert.deepEqual(back.map((e) => e.id), ids);
  assert.deepEqual(back.map((e) => e.version), [0, 1, 2, 3]);
});

test('GatewayStore.append with a deterministic sha256 id, then has()', async () => {
  const stream = `content-liveadapters${rid}sha`;
  const id = await deterministicId('ContentAdded', `live-adapters-${rid}#sha`, rid);
  const ev = makeEvent({ type: 'ContentAdded', stream, data: { id: `live-adapters-${rid}#sha` }, id });
  assert.equal((await store.append(stream, [ev], { expectedVersion: -1 })).appended, 1);
  assert.equal((await store.append(stream, [ev])).appended, 0);
  assert.equal(await store.has(id), true);
  assert.equal(await store.has('00000000-0000-4000-8000-000000000000'), false);
});

test('GatewayStore.read / lastPosition: in order, inclusive from, and Projector.catchUp resumes at position+1', async () => {
  const stream = `content-liveadapters${rid}rd`;
  const mk = (i) => makeEvent({ type: 'ContentAdded', stream, data: { id: `live-adapters-${rid}#rd${i}`, title: `r${i}` } });
  const first = [mk(0), mk(1)];
  const { positions } = await store.append(stream, first);
  const got = await store.read({ fromPosition: positions[0] });
  assert.equal(got[0].id, first[0].id, 'fromPosition is inclusive');
  assert.deepEqual(got.filter((e) => e.stream === stream).map((e) => e.id), first.map((e) => e.id));
  assert.ok((await store.lastPosition()) >= positions[1]);
  // Projector against the real store: positions are sparse on KurrentDB, so position+1 is not a record boundary.
  const p = new Projector({ name: `live-${rid}`, initial: () => ({ n: 0 }), handlers: { ContentAdded: (s) => ({ n: s.n + 1 }) } });
  p.position = positions[0] - 1;
  await p.catchUp(store);
  assert.equal(p.position, (await store.lastPosition()));
  const before = p.state.n;
  assert.equal(await p.catchUp(store), 0, 'second catch-up at the end reads nothing');
  await store.append(stream, [mk(2)]);
  assert.equal(await p.catchUp(store), 1, 'resumes from position+1 and sees only the new event');
  assert.equal(p.state.n, before + 1);
});

test('GatewayStore: non-2xx other than 409 surfaces as an error with the HTTP status', async () => {
  await assert.rejects(store.append('content-x', [{ id: 'nope', type: 'X', stream: 'content-x', data: {}, meta: {} }]),
    (e) => e.status === 400);
});

test('KafkaRestBroker.publish: record lands on a Kafka topic (REST proxy v3, STRING key / JSON value)', async () => {
  const broker = new KafkaRestBroker({ url: REST_URL, clusterId, topicPrefix: 'ingqiqo.' });
  const stream = `content-liveadapters${rid}k`;
  const ev = makeEvent({ type: 'ContentAdded', stream, data: { id: `live-adapters-${rid}#k`, title: 'via broker' }, meta: { source: 'browser-live-test' } });
  const res = await broker.publish('live-adapter-test', ev);
  assert.ok(res && res.error_code === 200, `unexpected produce response ${JSON.stringify(res)}`);
  published.topicTest = { topic: 'ingqiqo.live-adapter-test', id: ev.id, stream };
});

test('KafkaRestBroker.publish to the projector topic (topic "events"): browser-originated event reaches the read model', async () => {
  const broker = new KafkaRestBroker({ url: REST_URL, clusterId });
  const doc = `live-adapters-${rid}#proj`;
  const ev = makeEvent({ type: 'ContentAdded', stream: `content-liveadapters${rid}p`, data: { id: doc, title: 'from the browser', section: 's', type: 'page', url: '/x', hash: 'hh' }, meta: { source: 'browser-live-test' } });
  const res = await broker.publish('events', ev);
  assert.equal(res.error_code, 200);
  published.projectorDoc = { doc, id: ev.id };
});

test('KafkaRestBroker surfaces the REST proxy\'s HTTP-200-with-error_code failures (invalid topic name -> 40002)', async () => {
  const broker = new KafkaRestBroker({ url: REST_URL, clusterId });
  await assert.rejects(broker.publish('bad topic!', makeEvent({ type: 'X', stream: 'a-b', data: {} })), (e) => e.status === 40002);
});

test('observed: the REST proxy ignores the cluster id in the URL (single-cluster v3), so a wrong id still produces', async () => {
  const broker = new KafkaRestBroker({ url: REST_URL, clusterId: 'no-such-cluster' });
  const res = await broker.publish('live-adapter-test', makeEvent({ type: 'X', stream: 'a-b', data: { note: 'wrong cluster id accepted' } }));
  assert.equal(res.error_code, 200);
});

test('fromConfig builds the same adapters from a config document', async () => {
  const { broker, remoteStore } = fromConfig({ broker: { type: 'kafka-rest', url: REST_URL, clusterId }, remoteStore: { url: GATEWAY_URL } });
  assert.ok(broker instanceof KafkaRestBroker);
  assert.ok(remoteStore instanceof GatewayStore);
  assert.ok((await remoteStore.lastPosition()) >= 0);
});

test.after(() => {
  const out = join(here, 'out');
  mkdirSync(out, { recursive: true });
  writeFileSync(join(out, 'adapters-published.json'), JSON.stringify(published, null, 2));
});
