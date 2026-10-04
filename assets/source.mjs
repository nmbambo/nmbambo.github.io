// Which post brought this visit: the utm_source / utm_campaign / utm_content tags on the link the visitor followed.
// First touch only, kept in sessionStorage (this tab, this visit). No cookie, no server, nothing about the person.
// The booking page reads it so a request can say "linkedin / signal / 2026-10-06-cost-no-one-counts".
export const KEY = 'ingqiqo.source';
const PART = /^[A-Za-z0-9._-]{1,40}$/;
const WHOLE = /^[A-Za-z0-9._-]{1,40}( \/ [A-Za-z0-9._-]{1,40}){0,2}$/;

/** sourceFrom('?utm_source=linkedin&utm_campaign=signal') -> 'linkedin / signal'; '' when absent or unsafe. */
export function sourceFrom(search) {
  let p;
  try { p = new URLSearchParams(search || ''); } catch { return ''; }
  const src = p.get('utm_source');
  if (!src || !PART.test(src)) return '';
  return [src, p.get('utm_campaign'), p.get('utm_content')].filter((v) => v && PART.test(v)).join(' / ');
}

export const isSource = (s) => typeof s === 'string' && WHOLE.test(s);

function session() {
  try { return globalThis.sessionStorage ?? null; } catch { return null; }
}

/** Keep the first tagged arrival of this visit. Never throws. */
export function capture(search = globalThis.location?.search, store = session()) {
  try {
    const s = sourceFrom(search);
    if (s && store && !store.getItem(KEY)) store.setItem(KEY, s);
  } catch { /* storage blocked: the visit simply has no source */ }
}

/** The stored source, or ''. Never throws. */
export function readSource(store = session()) {
  try {
    const s = store?.getItem(KEY) || '';
    return isSource(s) ? s : '';
  } catch { return ''; }
}

if (typeof window !== 'undefined') capture();
