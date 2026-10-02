// Event envelope. See CONTRACT.md section 1. No personal data in events.

export function makeEvent({ type, stream, data, meta = {}, id } = {}) {
  if (typeof type !== 'string' || !type) throw new TypeError('makeEvent: type must be a non-empty string');
  if (typeof stream !== 'string' || !stream) throw new TypeError('makeEvent: stream must be a non-empty string');
  if (data === undefined) data = {};
  if (data === null || typeof data !== 'object') throw new TypeError('makeEvent: data must be a JSON object');
  const eventId = id ?? globalThis.crypto.randomUUID();
  return {
    id: eventId,
    type,
    stream,
    data,
    meta: { ts: new Date().toISOString(), schema: 1, source: 'browser', ...meta },
  };
}

export async function deterministicId(...parts) {
  const bytes = new TextEncoder().encode(parts.join('␟'));
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
}
