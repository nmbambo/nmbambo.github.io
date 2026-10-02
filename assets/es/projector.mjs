// Idempotent projector with checkpoints. See CONTRACT.md section 4.
// Handlers must be pure: no I/O, no Date.now, no randomness.

const clone = (v) => (v === undefined ? v : typeof structuredClone === 'function' ? structuredClone(v) : JSON.parse(JSON.stringify(v)));

export class Projector {
  constructor({ name, initial, handlers = {}, checkpoint } = {}) {
    if (!name) throw new TypeError('Projector: name is required');
    this.name = name;
    this._initial = initial;
    this.handlers = handlers;
    this.checkpoint = checkpoint ?? memoryCheckpoint;
    this.state = typeof initial === 'function' ? initial() : clone(initial);
    this.position = -1;
  }

  async load() {
    const saved = await this.checkpoint.load(this.name);
    if (saved && typeof saved.position === 'number') {
      this.position = saved.position;
      this.state = clone(saved.state);
    }
    return this;
  }

  // Returns true if the event was applied (position advanced), false if ignored.
  apply(event) {
    if (typeof event.position !== 'number') throw new TypeError('Projector.apply: event has no position');
    if (event.position <= this.position) return false;
    const h = this.handlers[event.type];
    if (h) this.state = h(this.state, event);
    this.position = event.position;
    return true;
  }

  async catchUp(store) {
    const events = await store.read({ fromPosition: this.position + 1 });
    for (const ev of events) this.apply(ev);
    await this.checkpoint.save(this.name, { position: this.position, state: this.state });
    return events.length;
  }
}

const KEY = (name) => `ingqiqo-es:checkpoint:${name}`;

export const localCheckpoint = {
  async load(name) {
    try {
      const raw = globalThis.localStorage?.getItem(KEY(name));
      return raw ? JSON.parse(raw) : null;
    } catch { return null; }
  },
  async save(name, value) {
    try { globalThis.localStorage?.setItem(KEY(name), JSON.stringify(value)); } catch { /* unavailable or full */ }
  },
};

const mem = new Map();
export const memoryCheckpoint = {
  async load(name) { return mem.has(name) ? clone(mem.get(name)) : null; },
  async save(name, value) { mem.set(name, clone(value)); },
};
