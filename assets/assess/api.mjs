import { STORE } from './config.mjs';

export class StoreError extends Error {}

// Call a database function. Errors raised by the function come back as readable messages.
export async function rpc(name, args, { store = STORE, fetchImpl = globalThis.fetch } = {}) {
  let res;
  try {
    res = await fetchImpl(`${store.url}/rest/v1/rpc/${name}`, {
      method: 'POST',
      headers: { apikey: store.key, 'content-type': 'application/json' },
      body: JSON.stringify(args),
    });
  } catch {
    throw new StoreError('The Ingqiqo store could not be reached. Check your connection and try again; your answers are kept on this device.');
  }
  const text = await res.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!res.ok) {
    const msg = body && typeof body === 'object' && body.message ? body.message : `The store refused the request (${res.status}).`;
    throw new StoreError(msg.charAt(0).toUpperCase() + msg.slice(1).replace(/\.?$/, '.'));
  }
  return body;
}

export async function sha256Hex(text) {
  const bytes = new TextEncoder().encode(text);
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

// Where this visit came from. Falls back to the first tagged arrival that assets/source.mjs kept for the visit.
export function context(loc = globalThis.location, doc = globalThis.document, nav = globalThis.navigator) {
  const q = new URLSearchParams(loc?.search || '');
  let kept = [];
  try { kept = (globalThis.sessionStorage?.getItem('ingqiqo.source') || '').split(' / '); } catch { /* storage blocked */ }
  return {
    page: loc ? loc.pathname : null,
    referrer: doc?.referrer || null,
    utm_source: q.get('utm_source') || kept[0] || null,
    utm_medium: q.get('utm_medium') || null,
    utm_campaign: q.get('utm_campaign') || kept[1] || null,
    ua: nav?.userAgent ? nav.userAgent.slice(0, 300) : null,
  };
}

export const isEmail = (s) => typeof s === 'string' && /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(s.trim()) && s.trim().length <= 160;

// The file a holder downloads. payload is the exact text that was hashed and signed.
export const EXPORT_FORMAT = 'ingqiqo-certified-report/1';
export function exportDocument(run) {
  return { format: EXPORT_FORMAT, cert_id: run.cert_id, sha256: run.sha256, signature: run.signature, payload: run.payload };
}

// Check a downloaded file on its own: is the payload intact, and which certificate does it claim to be?
export async function checkExport(doc) {
  if (!doc || doc.format !== EXPORT_FORMAT || typeof doc.payload !== 'string') {
    return { ok: false, reason: 'This is not an Ingqiqo certified report file.' };
  }
  let payload;
  try { payload = JSON.parse(doc.payload); } catch { return { ok: false, reason: 'The report inside the file is damaged.' }; }
  const hash = await sha256Hex(doc.payload);
  const claimed = payload?.certificate?.id;
  if (hash !== doc.sha256) return { ok: false, hash, certId: claimed, reason: 'The report has been altered: its content no longer matches its fingerprint.' };
  if (claimed !== doc.cert_id) return { ok: false, hash, certId: claimed, reason: 'The certificate ID on the file does not match the report inside it.' };
  return { ok: true, hash, certId: claimed, payload };
}

export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
