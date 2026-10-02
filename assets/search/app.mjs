// Search page: CQRS in the browser.
// Commands (submit, open, override) become events in an append-only store (IndexedDB),
// the mitt bus publishes them, idempotent projectors fold them into read models
// (the search index and the agent's learned statistics), and queries read only from those models.
// Content arrives the same way: build-time CDC appends Content* events to data/events.ndjson,
// which are replayed into the store idempotently on every visit.

import { makeEvent } from '../es/events.mjs';
import { openStore } from '../es/store.mjs';
import { bus } from '../es/bus.mjs';
import { createEventSourced } from '../es/eventsourced.mjs';
import { Projector, memoryCheckpoint } from '../es/projector.mjs';
import { fromConfig } from '../es/adapters.mjs';
import { SearchIndex } from './index.mjs';
import { SearchAgent, applyReward, rewardsFromEvent, PRIORS } from './agent.mjs';
import { ALGORITHMS } from './algorithms.mjs';
import { search } from './search.mjs';

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const marked = (s) => esc(s).replace(/\{\{(.+?)\}\}/g, '<mark>$1</mark>');
const SESSION = 'search-' + (globalThis.crypto?.randomUUID?.() ?? String(Math.random()).slice(2));
const LABEL = Object.fromEntries(ALGORITHMS.map((a) => [a.name, a.label]));

// ---------- read models ----------
const content = new Projector({
  name: 'content', checkpoint: memoryCheckpoint, initial: { docs: {} },
  handlers: {
    ContentAdded: (s, e) => ({ docs: { ...s.docs, [e.data.id]: stripHash(e.data) } }),
    ContentChanged: (s, e) => ({ docs: { ...s.docs, [e.data.id]: stripHash(e.data) } }),
    ContentRemoved: (s, e) => { const d = { ...s.docs }; delete d[e.data.id]; return { docs: d }; },
  },
});
function stripHash(d) { const { hash, ...doc } = d; return doc; }

const rewardHandler = (s, e) => {
  let st = s;
  for (const x of rewardsFromEvent(e)) st = applyReward(st, x.key, x.algorithm, x.r);
  return st;
};
const stats = new Projector({
  name: 'agentStats', checkpoint: memoryCheckpoint, initial: {},
  handlers: { SearchSubmitted: rewardHandler, ResultOpened: rewardHandler, AlgorithmOverridden: rewardHandler },
});
const recent = new Projector({
  name: 'recentQueries', checkpoint: memoryCheckpoint, initial: [],
  handlers: { SearchSubmitted: (s, e) => [e.data.query, ...s.filter((q) => q !== e.data.query)].slice(0, 8) },
});
const projectors = [content, stats, recent];

let store, es, index, agent, broker;
let last = { query: '', result: null, mode: 'auto' };

// ---------- boot ----------
async function boot() {
  setStatus('Opening your local event log…');
  store = await openStore();
  es = createEventSourced({ store, bus });
  try {
    const cfg = await fetch('../assets/es/config.json').then((r) => (r.ok ? r.json() : {}));
    broker = fromConfig(cfg, { bus }).broker;
  } catch { broker = null; }

  setStatus('Replaying captured content changes…');
  const text = await fetch('../data/events.ndjson', { cache: 'no-cache' }).then((r) => r.text());
  const incoming = text.split('\n').filter(Boolean).map((l) => JSON.parse(l));
  let added = 0;
  for (const ev of incoming) added += (await es.append(ev.stream, [ev])).appended; // idempotent by id

  for (const p of projectors) { await p.load(); await p.catchUp(store); }
  index = new SearchIndex(Object.values(content.state.docs));
  agent = new SearchAgent({ algorithms: ALGORITHMS, state: stats.state });

  // live: every new event flows through the bus into the projectors
  bus.on('*', (type, ev) => {
    for (const p of projectors) p.apply(ev);
    if (type === 'ContentAdded' || type === 'ContentChanged') index.add(stripHash(ev.data));
    if (type === 'ContentRemoved') index.remove(ev.data.id);
    agent.state = stats.state;
    if (broker && broker.constructor.name !== 'LocalBroker') broker.publish('site-events', ev).catch(() => {});
    logEvent(ev);
    renderStats();
  });

  const total = await store.lastPosition() + 1;
  setStatus(`${index.docs().length} sections indexed from ${incoming.length} content events${added ? ` (${added} new to this browser)` : ''}. Your log holds ${total} events.`);
  renderStats();
  renderRecent();
  (await store.read({ fromPosition: Math.max(0, total - 6) })).forEach(logEvent);

  const q = new URLSearchParams(location.search).get('q');
  if (q) { $('#q').value = q; run(q, { record: true }); }
  $('#q').disabled = false;
  $('#q').focus();
}

// ---------- commands ----------
async function record(type, data) {
  const ev = makeEvent({ type, stream: SESSION, data, meta: { source: 'site-search' } });
  await es.append(SESSION, [ev]);
}

function run(query, { record: rec = false } = {}) {
  const mode = $('#mode').value;
  const res = search(query, index, agent, mode === 'auto' ? {} : { algorithm: mode });
  last = { query, result: res, mode };
  render(res);
  if (rec && query.trim()) {
    record('SearchSubmitted', { query, algorithm: res.algorithm, proposed: res.proposed, resultCount: res.total, key: res.key });
    if (mode !== 'auto' && mode !== res.proposed) {
      record('AlgorithmOverridden', { query, proposed: res.proposed, chosen: mode, key: res.key });
    }
  }
}

// ---------- rendering ----------
function render(res) {
  const out = $('#results');
  const why = $('#why');
  if (!last.query.trim()) { out.innerHTML = ''; why.innerHTML = ''; $('#count').textContent = ''; return; }
  const proposedLabel = LABEL[res.proposed] ?? res.proposed;
  why.innerHTML = `<span class="k">The agent proposes</span> <strong>${esc(proposedLabel)}</strong>${res.algorithm !== res.proposed ? ` · <span class="k">${last.mode === 'auto' ? 'fell back to' : 'you chose'}</span> <strong>${esc(LABEL[res.algorithm] ?? res.algorithm)}</strong>` : ''}<br>${esc(res.explanation)}${res.error ? `<br><em>${esc(res.error)}</em>` : ''}`;
  $('#count').textContent = `${res.total} ${res.total === 1 ? 'match' : 'matches'}`;
  out.innerHTML = res.results.slice(0, 30).map((r, i) => {
    const d = r.doc || {};
    const where = d.url ? `<a class="hit-title" href="..${esc(d.url)}" data-id="${esc(r.id)}" data-rank="${i + 1}">${esc(r.title || d.title)}</a>`
                        : `<span class="hit-title" data-id="${esc(r.id)}" data-rank="${i + 1}">${esc(r.title || d.title)}</span>`;
    return `<li class="hit">
      <p class="hit-meta"><span class="tag">${esc(d.type)}</span>${d.section && d.section !== d.title ? ` · ${esc(d.section)}` : ''}${!d.url && d.source ? ` · ${esc(d.source)}` : ''}</p>
      ${where}
      <p class="hit-snip">${marked(r.snippet || '')}</p>
    </li>`;
  }).join('') || '<li class="hit none">Nothing matched. Try fewer words, switch to fuzzy, or loosen an AND into an OR.</li>';
}

function renderStats() {
  const rows = Object.keys(PRIORS).map((key) => {
    const cells = ALGORITHMS.map((a) => {
      const p = PRIORS[key]?.[a.name] ?? { a: 1, b: 1 };
      const s = stats.state?.[key]?.[a.name];
      const ab = s ?? p;
      const mean = ab.a / (ab.a + ab.b);
      return `<td class="num${s ? ' learnt' : ''}" title="α ${ab.a.toFixed(1)} · β ${ab.b.toFixed(1)}">${mean.toFixed(2)}</td>`;
    }).join('');
    return `<tr><td>${esc(key)}</td>${cells}</tr>`;
  }).join('');
  $('#stats').innerHTML = `<thead><tr><th scope="col">Query shape</th>${ALGORITHMS.map((a) => `<th scope="col" class="num">${esc(a.label)}</th>`).join('')}</tr></thead><tbody>${rows}</tbody>`;
  $('#positions').textContent = projectors.map((p) => `${p.name} @ ${p.position}`).join(' · ');
  renderRecent();
}

function renderRecent() {
  const qs = recent.state || [];
  $('#recent').innerHTML = qs.length ? qs.map((q) => `<button type="button" class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join('') : '<span class="muted">Your searches stay in this browser only.</span>';
}

function logEvent(ev) {
  const li = document.createElement('li');
  li.innerHTML = `<span class="pos">#${ev.position}</span> <strong>${esc(ev.type)}</strong> <span class="muted">${esc(ev.stream.length > 38 ? ev.stream.slice(0, 38) + '…' : ev.stream)}</span>`;
  const log = $('#log');
  log.prepend(li);
  while (log.children.length > 12) log.lastChild.remove();
}

function setStatus(s) { $('#status').textContent = s; }

// ---------- wiring ----------
let t;
$('#q').addEventListener('input', (e) => { clearTimeout(t); t = setTimeout(() => run(e.target.value), 140); });
$('#form').addEventListener('submit', (e) => { e.preventDefault(); run($('#q').value, { record: true }); });
$('#mode').addEventListener('change', () => run($('#q').value, { record: !!$('#q').value.trim() }));
$('#results').addEventListener('click', (e) => {
  const a = e.target.closest('[data-id]');
  if (!a || !last.result) return;
  record('ResultOpened', { query: last.query, docId: a.dataset.id, rank: Number(a.dataset.rank), algorithm: last.result.algorithm, key: last.result.key });
});
$('#recent').addEventListener('click', (e) => {
  const b = e.target.closest('[data-q]');
  if (b) { $('#q').value = b.dataset.q; run(b.dataset.q, { record: true }); }
});
document.querySelectorAll('[data-try]').forEach((b) => b.addEventListener('click', () => { $('#q').value = b.dataset.try; run(b.dataset.try, { record: true }); }));
$('#forget').addEventListener('click', async () => {
  if (!confirm('Delete your local search log from this browser? Site content will be re-captured on next visit.')) return;
  try { store.close?.(); } catch {}
  try { indexedDB.deleteDatabase('ingqiqo-es'); } catch {}
  location.reload();
});

boot().catch((err) => {
  console.error(err);
  setStatus('Search could not start in this browser. ' + (err?.message || ''));
});
