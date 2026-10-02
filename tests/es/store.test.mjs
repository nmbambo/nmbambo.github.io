import { test } from 'node:test';
import assert from 'node:assert/strict';
import { makeEvent, deterministicId } from '../../assets/es/events.mjs';
import { MemoryStore, IndexedDBStore, ConcurrencyError, openStore } from '../../assets/es/store.mjs';

let IDB = null;
try { IDB = await import('/tmp/esdeps/node_modules/fake-indexeddb/build/esm/index.js'); } catch { /* optional */ }

const ev = (stream, n, id) => makeEvent({ type: 'Thing', stream, data: { n }, id });

const factories = [['MemoryStore', () => new MemoryStore()]];
if (IDB) factories.push(['IndexedDBStore', () => new IndexedDBStore({ idbFactory: new IDB.IDBFactory(), idbKeyRange: IDB.IDBKeyRange })]);
else test('IndexedDBStore (skipped: fake-indexeddb not installed in /tmp/esdeps)', { skip: true }, () => {});

for (const [name, make] of factories) {
  test(`${name}: idempotent append`, async () => {
    const s = make(); const e = ev('a', 1);
    assert.equal((await s.append('a', [e])).appended, 1);
    assert.equal((await s.append('a', [e])).appended, 0);
    assert.equal((await s.read()).length, 1);
    assert.equal(await s.has(e.id), true);
    assert.equal(await s.has('nope'), false);
    // duplicate inside a single batch
    const e2 = ev('a', 2);
    assert.equal((await s.append('a', [e2, e2])).appended, 1);
  });

  test(`${name}: expectedVersion mismatch throws ConcurrencyError`, async () => {
    const s = make();
    await s.append('a', [ev('a', 1)], { expectedVersion: -1 });
    await assert.rejects(s.append('a', [ev('a', 2)], { expectedVersion: -1 }), ConcurrencyError);
    await assert.rejects(s.append('a', [ev('a', 2)], { expectedVersion: 5 }), ConcurrencyError);
    assert.equal((await s.append('a', [ev('a', 2)], { expectedVersion: 0 })).appended, 1);
    assert.equal((await s.read()).length, 2);
  });

  test(`${name}: positions and versions are monotonic`, async () => {
    const s = make();
    assert.equal(await s.lastPosition(), -1);
    const r1 = await s.append('a', [ev('a', 1), ev('a', 2)]);
    const r2 = await s.append('b', [ev('b', 1)]);
    const r3 = await s.append('a', [ev('a', 3)]);
    assert.deepEqual(r1.positions, [0, 1]);
    assert.deepEqual(r2.positions, [2]);
    assert.deepEqual(r3.positions, [3]);
    const all = await s.read();
    assert.deepEqual(all.map((e) => e.position), [0, 1, 2, 3]);
    assert.deepEqual((await s.readStream('a')).map((e) => e.version), [0, 1, 2]);
    assert.deepEqual((await s.readStream('b')).map((e) => e.version), [0]);
    assert.deepEqual((await s.read({ fromPosition: 2 })).map((e) => e.position), [2, 3]);
    assert.deepEqual((await s.read({ fromPosition: 1, limit: 2 })).map((e) => e.position), [1, 2]);
    assert.equal(await s.lastPosition(), 3);
  });
}

test('deterministicId is stable and 64 hex chars', async () => {
  const a = await deterministicId('ContentAdded', 'doc1', 'h1');
  const b = await deterministicId('ContentAdded', 'doc1', 'h1');
  assert.equal(a, b);
  assert.match(a, /^[0-9a-f]{64}$/);
  assert.notEqual(a, await deterministicId('ContentAdded', 'doc1', 'h2'));
  assert.equal(await deterministicId('a', 'b'), await deterministicId('a␟b'));
});

test('makeEvent builds an envelope', () => {
  const e = makeEvent({ type: 'X', stream: 's', data: { a: 1 } });
  assert.match(e.id, /^[0-9a-f-]{36}$/);
  assert.equal(e.meta.schema, 1);
  assert.ok(!Number.isNaN(Date.parse(e.meta.ts)));
  assert.throws(() => makeEvent({ stream: 's' }));
});

test('openStore falls back to MemoryStore without indexedDB', async () => {
  assert.ok((await openStore()) instanceof MemoryStore);
});
