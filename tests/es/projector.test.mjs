import { test } from 'node:test';
import assert from 'node:assert/strict';
import { makeEvent } from '../../assets/es/events.mjs';
import { MemoryStore } from '../../assets/es/store.mjs';
import { Projector, memoryCheckpoint, localCheckpoint } from '../../assets/es/projector.mjs';
import { createEventSourced } from '../../assets/es/eventsourced.mjs';
import mitt from '../../assets/vendor/mitt.mjs';

const mk = (name, checkpoint = memoryCheckpoint) => new Projector({
  name, checkpoint, initial: { count: 0, sum: 0 },
  handlers: { Added: (s, e) => ({ count: s.count + 1, sum: s.sum + e.data.n }) },
});
const add = (n) => makeEvent({ type: 'Added', stream: 's', data: { n } });

test('replay twice gives the same state; duplicates not re-applied', async () => {
  const store = new MemoryStore();
  const e1 = add(1);
  await store.append('s', [e1, add(2), makeEvent({ type: 'Unknown', stream: 's' })]);
  await store.append('s', [e1]); // duplicate id: no-op
  const p = mk('r1');
  await p.catchUp(store);
  const first = structuredClone(p.state);
  assert.deepEqual(first, { count: 2, sum: 3 });
  assert.equal(p.position, 2); // unknown type advanced position
  await p.catchUp(store);
  assert.deepEqual(p.state, first);
  // applying an already-seen event object directly is ignored
  const all = await store.read();
  assert.equal(p.apply(all[0]), false);
  assert.deepEqual(p.state, first);
  // fresh projector replaying gives same state
  const q = mk('r1b'); await q.catchUp(store); await q.catchUp(store);
  assert.deepEqual(q.state, first);
});

test('checkpoint save/load resumes with only new events', async () => {
  const store = new MemoryStore();
  await store.append('s', [add(1), add(2)]);
  const p = mk('cp'); await p.catchUp(store);
  await store.append('s', [add(10)]);
  let applied = 0;
  const p2 = new Projector({
    name: 'cp', checkpoint: memoryCheckpoint, initial: { count: 0, sum: 0 },
    handlers: { Added: (s, e) => { applied++; return { count: s.count + 1, sum: s.sum + e.data.n }; } },
  });
  await p2.load();
  assert.equal(p2.position, 1);
  assert.deepEqual(p2.state, { count: 2, sum: 3 });
  await p2.catchUp(store);
  assert.equal(applied, 1);
  assert.deepEqual(p2.state, { count: 3, sum: 13 });
  assert.equal(p2.position, 2);
});

test('localCheckpoint is a safe no-op without localStorage, and works with it', async () => {
  assert.equal(await localCheckpoint.load('x'), null);
  await localCheckpoint.save('x', { position: 1, state: {} });
  const data = new Map();
  globalThis.localStorage = { getItem: (k) => data.get(k) ?? null, setItem: (k, v) => data.set(k, v) };
  try {
    await localCheckpoint.save('x', { position: 4, state: { a: 1 } });
    assert.deepEqual(await localCheckpoint.load('x'), { position: 4, state: { a: 1 } });
  } finally { delete globalThis.localStorage; }
});

test('EventSourced emits only newly appended events on type and *', async () => {
  const store = new MemoryStore(); const bus = mitt();
  const es = createEventSourced({ store, bus });
  const typed = [], wild = [];
  bus.on('Added', (e) => typed.push(e));
  bus.on('*', (t, e) => wild.push([t, e]));
  const e1 = add(1);
  await es.append('s', [e1, add(2)]);
  await es.append('s', [e1]); // duplicate: no emit
  assert.equal(typed.length, 2);
  assert.equal(wild.length, 2);
  assert.equal(wild[0][0], 'Added');
  assert.equal(typed[1].position, 1);
  assert.equal(typed[1].version, 1);
  const p = mk('es-replay');
  await es.replayInto([p]);
  assert.deepEqual(p.state, { count: 2, sum: 3 });
});
