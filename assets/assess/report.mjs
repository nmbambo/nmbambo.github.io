import { rpc, exportDocument, esc, StoreError } from './api.mjs';
import { renderReport, fmtDate } from './render.mjs';

const $ = (id) => document.getElementById(id);
const KEYSTORE = 'ingqiqo.dmsc.key';

function fail(msg) {
  $('as-report').innerHTML = `<section class="hero project-hero"><p class="label">Certified report</p><h1>Report not found</h1>
    <p class="role">${esc(msg)}</p><p class="cta-row"><a class="cta-primary" href="../self-check/">Take the self-check</a><a class="cta-quiet" href="../../verify/">Verify a certificate →</a></p></section>`;
}

async function init() {
  const q = new URLSearchParams(location.search);
  let key = q.get('k');
  if (!key) { try { key = JSON.parse(localStorage.getItem(KEYSTORE) || 'null'); } catch { key = null; } }
  if (!key) return fail('Open the private link you were given when your report was issued.');
  let series;
  try { series = await rpc('get_assessment_series', { p_key: key }); } catch (e) {
    return fail(e instanceof StoreError ? e.message : 'The report could not be loaded.');
  }
  if (!series || !series.runs?.length) return fail('This link is not recognised, or the report was erased at its holder\'s request.');

  const want = Number(q.get('v')) || series.runs[series.runs.length - 1].version;
  const run = series.runs.find((r) => r.version === want) || series.runs[series.runs.length - 1];
  const payload = JSON.parse(run.payload);
  const verifyUrl = `../../verify/?id=${encodeURIComponent(run.cert_id)}`;

  document.title = `${run.cert_id} — Certified report — Ingqiqo Executables`;
  $('as-report').innerHTML = renderReport(payload, run, { verifyUrl });
  $('as-actions').hidden = false;
  // Every printed page carries the certificate ID (CSS page-margin box; the ID is a safe [A-Z0-9-] string).
  const foot = document.createElement('style');
  foot.textContent = `@media print { @page { @bottom-left { content: "Ingqiqo Executables \\00B7  ${run.cert_id.replace(/[^A-Z0-9-]/g, '')}  \\00B7  verify at nmbambo.github.io/verify"; font: 8pt Georgia, serif; color: #666; } } }`;
  document.head.append(foot);

  $('as-print').addEventListener('click', () => window.print());
  $('as-download').addEventListener('click', () => {
    const blob = new Blob([JSON.stringify(exportDocument(run), null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${run.cert_id}.ingqiqo.json`;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });
  if (series.instrument === 'dmsc') {
    $('as-rerun-link').href = `../self-check/?k=${encodeURIComponent(key)}`;
    try { localStorage.setItem(KEYSTORE, JSON.stringify(key)); } catch { /* private window */ }
  } else {
    $('as-rerun-link').hidden = true;
  }

  const items = series.runs.slice().reverse().map((r) => {
    const p = JSON.parse(r.payload);
    const here = r.version === run.version;
    const changed = p.diff ? ` · ${p.diff.changed} of 15 criteria changed` : ' · first version';
    return `<li>${here ? '<strong>' : `<a href="?k=${encodeURIComponent(key)}&v=${r.version}">`}Version ${r.version}${here ? '</strong>' : '</a>'}
      <span class="as-mono">${esc(r.cert_id)}</span> · ${esc(fmtDate(r.issued_at))} · ${esc(p.scores.profile || '')}${esc(changed)}</li>`;
  }).join('');
  $('as-versions').innerHTML = `<p class="k as-k">All versions of series ${esc(series.series)}</p><ul class="as-versions">${items}</ul>`;
}

init();
