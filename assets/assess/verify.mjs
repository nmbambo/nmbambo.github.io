import { rpc, checkExport, esc, StoreError } from './api.mjs';

const $ = (id) => document.getElementById(id);
const fmt = (iso) => new Date(iso).toLocaleString('en-ZA', { day: 'numeric', month: 'long', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'Africa/Johannesburg' }) + ' SAST';

export function verdictHtml(reg, file) {
  const rows = [];
  let head;
  if (reg.status === 'valid' && reg.signature_valid) {
    head = `<p class="as-verdict ok">Genuine. ${esc(reg.cert_id)} is in Ingqiqo's register and its signature checks.</p>`;
  } else if (reg.status === 'valid') {
    head = `<p class="as-verdict bad">The register holds ${esc(reg.cert_id)}, but its signature does not check. Do not rely on it; write to Ingqiqo.</p>`;
  } else if (reg.status === 'withdrawn') {
    head = `<p class="as-verdict bad">Withdrawn on ${esc(fmt(reg.withdrawn_at))}. ${esc(reg.reason)}.</p>`;
  } else {
    head = `<p class="as-verdict bad">Not found. No certificate ${esc(reg.cert_id)} is in Ingqiqo's register. Check the ID for typing errors.</p>`;
  }
  if (reg.status === 'valid') {
    rows.push(['Instrument', `${reg.instrument?.name ?? ''} · ${reg.instrument?.version ?? ''}`]);
    rows.push(['Organisation', reg.organisation]);
    if (reg.decision) rows.push(['Decision assessed', reg.decision]);
    if (reg.engagement) rows.push(['Engagement', reg.engagement]);
    rows.push(['Basis', reg.basis === 'self-reported' ? 'Self-reported, not verified by Ingqiqo' : 'Scored by an Ingqiqo assessor from evidence']);
    rows.push(['Version', reg.latest_cert_id && reg.latest_cert_id !== reg.cert_id
      ? `${reg.version}. A later version exists: ${reg.latest_cert_id}` : `${reg.version}, the latest`]);
    rows.push(['Issued', fmt(reg.issued_at)]);
    rows.push(['Stage profile', reg.profile]);
    rows.push(['Fingerprint', reg.sha256]);
  }
  let fileLine = '';
  if (file) {
    if (!file.ok) fileLine = `<p class="as-verdict bad">The file: ${esc(file.reason)}</p>`;
    else if (reg.status !== 'valid') fileLine = '<p class="as-verdict bad">The file is internally consistent, but the register does not vouch for it.</p>';
    else if (file.hash === reg.sha256) fileLine = '<p class="as-verdict ok">The file matches the register exactly. Nothing in it has been changed.</p>';
    else fileLine = '<p class="as-verdict bad">The file does not match the register. Its content differs from what was certified.</p>';
  }
  return `${head}${fileLine}${rows.length ? `<dl class="as-dl">${rows.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd${k === 'Fingerprint' ? ' class="as-mono as-break"' : ''}>${esc(v)}</dd></div>`).join('')}</dl>` : ''}`;
}

async function run(ev) {
  ev?.preventDefault();
  $('v-result').innerHTML = '';
  let file = null;
  const f = $('v-file').files?.[0];
  if (f) {
    try { file = await checkExport(JSON.parse(await f.text())); } catch { file = { ok: false, reason: 'This is not an Ingqiqo certified report file.' }; }
    if (file.certId && !$('v-id').value.trim()) $('v-id').value = file.certId;
  }
  const id = $('v-id').value.trim().toUpperCase();
  if (!id) { $('v-status').textContent = 'Enter a certificate ID, or choose the certified file.'; return; }
  $('v-status').textContent = 'Checking the register…';
  try {
    const reg = await rpc('verify_certificate', { p_cert_id: id });
    $('v-result').innerHTML = verdictHtml(reg, file);
    $('v-status').textContent = '';
    history.replaceState(null, '', `?id=${encodeURIComponent(id)}`);
  } catch (e) {
    $('v-status').textContent = e instanceof StoreError ? e.message : 'The register could not be checked.';
  }
}

if (typeof document !== 'undefined') {
  $('v-form').addEventListener('submit', run);
  const id = new URLSearchParams(location.search).get('id');
  if (id) { $('v-id').value = id; run(); }
}
