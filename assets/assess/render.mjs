// Turns a certified payload into the report a holder reads, prints and shares.
// Pure: payload in, HTML string out (every value escaped), so it can be tested without a browser.
import { esc } from './api.mjs';
import { STAGES, CRITERIA, LEVELS, EVIDENCE_LABEL, criterion } from './dmsc.mjs';

const fmtDate = (iso) => new Date(iso).toLocaleDateString('en-ZA', { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'Africa/Johannesburg' });
const fmtTime = (iso) => new Date(iso).toLocaleString('en-ZA', { day: 'numeric', month: 'long', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'Africa/Johannesburg' }) + ' SAST';
const stageScore = (st) => (!st ? '·' : st.status === 'insufficient' ? 'Insufficient evidence' : String(st.score));
const CHANGE = { up: 'Rose', down: 'Fell', answered: 'Now answered', unknown: 'Now unknown', claimed: 'Claim changed', evidence: 'Evidence changed' };

function stageTable(payload) {
  const engagement = payload.certificate.instrument.id === 'dma';
  const rows = STAGES.map(([s, name]) => {
    const st = payload.scores.stages?.[s];
    const detail = !st ? '' : engagement
      ? (st.status === 'insufficient' ? 'More than a third of its criteria lacked evidence' : `Range ${esc(st.range ?? '')} across decisions`)
      : (st.status === 'insufficient' ? `${st.el} of 3 criteria unknown` : `Median ${esc(st.median)}${st.el ? `, ${st.el} unknown` : ''}`);
    const weak = (payload.scores.weakest || []).includes(s);
    return `<tr${weak ? ' class="weak"' : ''}><td>${esc(name)}${weak ? ' <span class="tag">weakest</span>' : ''}</td><td class="num">${esc(stageScore(st))}</td><td>${detail}</td></tr>`;
  }).join('');
  return `<table class="spec as-table"><thead><tr><th>Stage</th><th>Score</th><th>${engagement ? 'Spread' : 'Detail'}</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function criteriaTable(payload) {
  const engagement = payload.certificate.instrument.id === 'dma';
  const rows = CRITERIA.map(([id, , label]) => {
    const c = payload.scores.criteria?.[id];
    if (!c) return '';
    if (engagement) {
      return `<tr><td><span class="as-id">${id}</span> ${esc(label)}</td><td class="num">${esc(c.score)}</td><td>${esc(c.min)} to ${esc(c.max)}</td><td>${c.el_count ? `${esc(c.el_count)} evidence-limited` : ''}</td></tr>`;
    }
    const claim = c.el ? 'Unknown' : `${c.level} · ${LEVELS[c.level]}`;
    const ev = c.el ? '' : esc(EVIDENCE_LABEL[c.evidence] || c.evidence);
    const note = c.el ? 'Limitation, not failure' : c.capped ? `Capped by evidence at ${c.score}` : '';
    return `<tr><td><span class="as-id">${id}</span> ${esc(label)}</td><td>${esc(claim)}</td><td>${ev}</td><td class="num">${c.el ? '·' : esc(c.score)}</td><td>${esc(note)}</td></tr>`;
  }).join('');
  const head = engagement
    ? '<th>Criterion</th><th>Median</th><th>Range</th><th>Evidence</th>'
    : '<th>Criterion</th><th>What happened</th><th>What could be shown</th><th>Score</th><th>Note</th>';
  return `<div class="tw as-tw"><table class="spec as-table as-criteria"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div>`;
}

function diffBlock(payload) {
  const d = payload.diff;
  if (!d) return '';
  const summary = (d.summary || []).map((s) => `<li>${esc(s)}</li>`).join('');
  const stages = (d.stages || []).map((s) => `<li>${esc(s.implication)}</li>`).join('');
  const crit = (d.criteria || []).map((c) => {
    const b = c.before, a = c.after;
    const show = (x) => (x.el ? 'unknown' : x.score !== undefined && x.level !== undefined
      ? `${x.level} on ${EVIDENCE_LABEL[x.evidence] ? EVIDENCE_LABEL[x.evidence].toLowerCase() : x.evidence}, scores ${x.score}`
      : `median ${x.score}`);
    return `<tr><td><span class="as-id">${esc(c.id)}</span> ${esc(c.label)}</td><td>${esc(CHANGE[c.change] || c.change)}</td><td>${esc(show(b))}</td><td>${esc(show(a))}</td><td>${esc(c.implication)}</td></tr>`;
  }).join('');
  return `
  <section aria-labelledby="r-diff" class="as-diff">
    <h2 id="r-diff"><span class="num">III</span> What changed since version ${esc(d.from_version)}</h2>
    <ul class="as-summary">${summary}</ul>
    ${stages ? `<p class="k as-k">Stages</p><ul class="as-summary">${stages}</ul>` : ''}
    ${crit ? `<p class="k as-k">Criteria</p><div class="tw as-tw"><table class="spec as-table as-criteria"><thead><tr><th>Criterion</th><th>Change</th><th>Before</th><th>Now</th><th>What it implies</th></tr></thead><tbody>${crit}</tbody></table></div>` : ''}
  </section>`;
}

export function renderReport(payload, run, { verifyUrl } = {}) {
  const c = payload.certificate;
  const s = payload.subject || {};
  const engagement = c.instrument.id === 'dma';
  const subjectRows = engagement
    ? [['Organisation', s.organisation], ['Engagement', s.engagement], ['Period', s.period], ['Decisions sampled', s.decisions_sampled], ['Assessor', s.assessor]]
    : [['Organisation', s.organisation], ['Decision assessed', s.decision], ['Respondent role', s.role]];
  return `
  <section class="hero project-hero as-cert">
    <p class="label">Ingqiqo Executables · Certified report</p>
    <h1>${esc(c.instrument.name)}</h1>
    <p class="role">${esc(s.organisation)}${s.decision ? ` · ${esc(s.decision)}` : ''}${s.engagement ? ` · ${esc(s.engagement)}` : ''}</p>
    <dl class="facts">
      <div><dt>Certificate</dt><dd class="as-mono">${esc(c.id)}</dd></div>
      <div><dt>Version</dt><dd>${esc(c.version)}${c.supersedes ? ` · supersedes ${esc(c.supersedes)}` : ''}</dd></div>
      <div><dt>Issued</dt><dd>${esc(fmtTime(c.issued_at))}</dd></div>
      <div><dt>Basis</dt><dd>${c.basis === 'self-reported' ? 'Self-reported, not verified' : 'Scored by an Ingqiqo assessor from evidence'}</dd></div>
    </dl>
  </section>

  <section aria-labelledby="r-profile">
    <h2 id="r-profile"><span class="num">I</span> Stage profile</h2>
    <p class="body">Each stage scores as its weakest criterion, because a stage is only as strong as its weakest required record. There is no single overall score.${engagement ? '' : ' Scores run from 0, absent, to 3, demonstrated by a record from the decision itself.'}</p>
    ${stageTable(payload)}
  </section>

  <section aria-labelledby="r-criteria">
    <h2 id="r-criteria"><span class="num">II</span> Criteria</h2>
    ${criteriaTable(payload)}
  </section>

  ${diffBlock(payload)}

  <section aria-labelledby="r-cert" class="as-certblock">
    <h2 id="r-cert"><span class="num">${payload.diff ? 'IV' : 'III'}</span> Certificate</h2>
    <p class="body">${esc(c.statement)}</p>
    <dl class="as-dl">
      <div><dt>Issuer</dt><dd>${esc(c.issuer)}</dd></div>
      <div><dt>Instrument</dt><dd>${esc(c.instrument.name)} · ${esc(c.instrument.version)}</dd></div>
      <div><dt>Certificate ID</dt><dd class="as-mono">${esc(c.id)}</dd></div>
      <div><dt>Fingerprint (SHA-256)</dt><dd class="as-mono as-break">${esc(run.sha256)}</dd></div>
      <div><dt>Signature (HMAC-SHA-256)</dt><dd class="as-mono as-break">${esc(run.signature)}</dd></div>
      <div><dt>Verify at</dt><dd><a href="${esc(verifyUrl || c.verify)}">${esc(c.verify)}</a></dd></div>
    </dl>
    <p class="bk-hint">The fingerprint is computed over the exact certified content in the downloadable file. Anyone can recompute it; only Ingqiqo's register can confirm the signature.</p>
  </section>`;
}

export { fmtDate, criterion };
