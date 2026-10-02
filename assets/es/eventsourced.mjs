// Glue: append to the store, then publish newly appended events on the bus.
import { bus as defaultBus } from './bus.mjs';

export function createEventSourced({ store, bus = defaultBus } = {}) {
  if (!store) throw new TypeError('createEventSourced: store is required');
  return {
    store,
    bus,
    async append(stream, events, opts) {
      const fresh = new Set();
      for (const ev of events) if (!fresh.has(ev.id) && !(await store.has(ev.id))) fresh.add(ev.id);
      const result = await store.append(stream, events, opts);
      if (result.appended > 0) {
        const stored = await store.read({ fromPosition: Math.min(...result.positions) });
        for (const ev of stored) {
          if (fresh.has(ev.id) && result.positions.includes(ev.position)) {
            // mitt delivers every emit(type, ev) to '*' handlers as (type, ev); an explicit
            // emit('*') would call wildcard handlers 3x per event, so we emit the type only.
            bus.emit(ev.type, ev);
          }
        }
      }
      return result;
    },
    async replayInto(projectors) {
      for (const p of projectors) {
        await p.load();
        await p.catchUp(store);
      }
    },
  };
}
