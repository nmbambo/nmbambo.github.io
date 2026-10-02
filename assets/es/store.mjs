// Event stores. See CONTRACT.md section 2. Append-only; appends are idempotent on event id.

export class ConcurrencyError extends Error {
  constructor(message, { stream, expected, actual } = {}) {
    super(message);
    this.name = 'ConcurrencyError';
    this.stream = stream;
    this.expected = expected;
    this.actual = actual;
  }
}

const clone = (v) => (typeof structuredClone === 'function' ? structuredClone(v) : JSON.parse(JSON.stringify(v)));

export class MemoryStore {
  constructor() {
    this._events = [];          // global log, index === position
    this._ids = new Set();
    this._versions = new Map(); // stream -> next version
  }

  async append(stream, events, { expectedVersion } = {}) {
    if (typeof stream !== 'string' || !stream) throw new TypeError('append: stream must be a non-empty string');
    const current = (this._versions.get(stream) ?? 0) - 1;
    if (expectedVersion !== undefined && expectedVersion !== current) {
      throw new ConcurrencyError(
        `Stream "${stream}" is at version ${current}, expected ${expectedVersion}`,
        { stream, expected: expectedVersion, actual: current });
    }
    const positions = [];
    for (const ev of events) {
      if (this._ids.has(ev.id)) continue;
      const position = this._events.length;
      const version = this._versions.get(stream) ?? 0;
      this._events.push(clone({ ...ev, stream, position, version }));
      this._ids.add(ev.id);
      this._versions.set(stream, version + 1);
      positions.push(position);
    }
    return { appended: positions.length, positions };
  }

  async read({ fromPosition = 0, limit } = {}) {
    const end = limit === undefined ? undefined : fromPosition + limit;
    return this._events.slice(Math.max(0, fromPosition), end).map(clone);
  }

  async readStream(stream) {
    return this._events.filter((e) => e.stream === stream).map(clone);
  }

  async lastPosition() { return this._events.length - 1; }
  async has(id) { return this._ids.has(id); }
}

const DB_NAME = 'ingqiqo-es';
const STORE = 'events';

const req = (r) => new Promise((res, rej) => {
  r.onsuccess = () => res(r.result);
  r.onerror = () => rej(r.error);
});

export class IndexedDBStore {
  // idbFactory is injectable (tests); defaults to globalThis.indexedDB.
  constructor({ idbFactory, idbKeyRange, dbName = DB_NAME } = {}) {
    this._factory = idbFactory ?? globalThis.indexedDB;
    this._range = idbKeyRange ?? globalThis.IDBKeyRange;
    if (!this._factory) throw new Error('IndexedDBStore: indexedDB is not available');
    this._dbName = dbName;
    this._dbp = null;
  }

  _db() {
    if (!this._dbp) {
      this._dbp = new Promise((resolve, reject) => {
        const open = this._factory.open(this._dbName, 1);
        open.onupgradeneeded = () => {
          const db = open.result;
          const os = db.createObjectStore(STORE, { keyPath: 'position' });
          os.createIndex('id', 'id', { unique: true });
          os.createIndex('stream', 'stream', { unique: false });
        };
        open.onsuccess = () => resolve(open.result);
        open.onerror = () => reject(open.error);
      });
      this._dbp.catch(() => { this._dbp = null; });
    }
    return this._dbp;
  }

  async append(stream, events, { expectedVersion } = {}) {
    if (typeof stream !== 'string' || !stream) throw new TypeError('append: stream must be a non-empty string');
    const db = await this._db();
    const tx = db.transaction(STORE, 'readwrite');
    const os = tx.objectStore(STORE);
    const done = new Promise((res, rej) => {
      tx.oncomplete = () => res();
      tx.onabort = () => rej(tx.error || new Error('transaction aborted'));
      tx.onerror = () => rej(tx.error);
    });
    done.catch(() => {});
    try {
      let version = await req(os.index('stream').count(this._range.only(stream)));
      if (expectedVersion !== undefined && expectedVersion !== version - 1) {
        throw new ConcurrencyError(
          `Stream "${stream}" is at version ${version - 1}, expected ${expectedVersion}`,
          { stream, expected: expectedVersion, actual: version - 1 });
      }
      const cur = await req(os.openCursor(null, 'prev'));
      let next = cur ? cur.key + 1 : 0;
      const positions = [];
      for (const ev of events) {
        const existing = await req(os.index('id').getKey(ev.id));
        if (existing !== undefined) continue;
        os.add(clone({ ...ev, stream, position: next, version }));
        positions.push(next);
        next += 1;
        version += 1;
      }
      await done;
      return { appended: positions.length, positions };
    } catch (err) {
      try { tx.abort(); } catch { /* already finished */ }
      throw err;
    }
  }

  async read({ fromPosition = 0, limit } = {}) {
    const db = await this._db();
    const os = db.transaction(STORE, 'readonly').objectStore(STORE);
    const range = this._range.lowerBound(Math.max(0, fromPosition));
    const rows = limit === undefined ? await req(os.getAll(range)) : await req(os.getAll(range, limit));
    return rows;
  }

  async readStream(stream) {
    const db = await this._db();
    const os = db.transaction(STORE, 'readonly').objectStore(STORE);
    const rows = await req(os.index('stream').getAll(this._range.only(stream)));
    return rows.sort((a, b) => a.position - b.position);
  }

  async lastPosition() {
    const db = await this._db();
    const os = db.transaction(STORE, 'readonly').objectStore(STORE);
    const cur = await req(os.openCursor(null, 'prev'));
    return cur ? cur.key : -1;
  }

  async has(id) {
    const db = await this._db();
    const os = db.transaction(STORE, 'readonly').objectStore(STORE);
    return (await req(os.index('id').getKey(id))) !== undefined;
  }

  close() {
    if (this._dbp) this._dbp.then((db) => db.close()).catch(() => {});
    this._dbp = null;
  }
}

export async function openStore() {
  if (globalThis.indexedDB) {
    try {
      const s = new IndexedDBStore();
      await s.lastPosition(); // forces open; fails in some private modes
      return s;
    } catch { /* fall through to memory */ }
  }
  return new MemoryStore();
}
