import { rpc, context, isEmail, esc, StoreError } from './api.mjs';
import { CONSENT_VERSION } from './config.mjs';
import { STAGES, CRITERIA, LEVELS, EVIDENCE, collectAnswers, stateFromAnswers } from './dmsc.mjs';

const DRAFT = 'ingqiqo.dmsc.draft';
const KEYSTORE = 'ingqiqo.dmsc.key';
const ROMAN = ['III', 'IV', 'V', 'VI', 'VII'];
const $ = (id) => document.getElementById(id);

const store = {
  get(k) { try { return JSON.parse(localStorage.getItem(k) || 'null'); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private window */ } },
  drop(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } },
};

let state = {};
let rerunKey = null;

function renderStages() {
  $('as-stages').innerHTML = STAGES.map(([s, name, lede], i) => `
    <section aria-labelledby="s-${s}">
      <h2 id="s-${s}"><span class="num">${ROMAN[i]}</span> ${esc(name)}</h2>
      <p class="body as-lede">${esc(lede)}</p>
      ${CRITERIA.filter((c) => c[1] === s).map(renderCriterion).join('')}
    </section>`).join('');
}

function renderCriterion([id, , label, question, levels]) {
  return `
    <fieldset class="as-q" id="q-${id}" data-id="${id}">
      <legend><span class="as-id">${id}</span> <span class="as-label">${esc(label)}</span><span class="as-question">${esc(question)}</span></legend>
      <div class="as-levels" role="radiogroup" aria-label="${esc(label)}: what happened">
        ${levels.map((d, n) => `<label class="bk-pick as-level"><input type="radio" name="${id}-level" value="${n}">
          <span><strong>${n} · ${LEVELS[n]}</strong> ${esc(d)}</span></label>`).join('')}
      </div>
      <p class="k as-k">What could you show for it?</p>
      <div class="bk-opts as-evidence" role="radiogroup" aria-label="${esc(label)}: evidence">
        ${EVIDENCE.map(([k, l, cap]) => `<label class="bk-opt"><input type="radio" name="${id}-evidence" value="${k}"><span>${esc(l)} <em>${cap}</em></span></label>`).join('')}
      </div>
      <label class="bk-none as-unknown"><input type="checkbox" name="${id}-unknown"> <span>I don't know</span></label>
      <p class="bk-err" id="q-${id}-err" hidden></p>
    </fieldset>`;
}

function readState() {
  const s = {};
  for (const [id] of CRITERIA) {
    const level = document.querySelector(`input[name="${id}-level"]:checked`);
    const ev = document.querySelector(`input[name="${id}-evidence"]:checked`);
    const unknown = document.querySelector(`input[name="${id}-unknown"]`).checked;
    s[id] = { level: level ? Number(level.value) : null, evidence: ev ? ev.value : null, unknown };
  }
  return s;
}

function applyState(s) {
  for (const [id] of CRITERIA) {
    const a = s[id] || {};
    document.querySelectorAll(`input[name="${id}-level"]`).forEach((r) => { r.checked = Number(r.value) === a.level; });
    document.querySelectorAll(`input[name="${id}-evidence"]`).forEach((r) => { r.checked = r.value === a.evidence; });
    document.querySelector(`input[name="${id}-unknown"]`).checked = Boolean(a.unknown);
    syncUnknown(id);
  }
}

function syncUnknown(id) {
  const q = $(`q-${id}`);
  const unknown = q.querySelector(`input[name="${id}-unknown"]`).checked;
  q.classList.toggle('is-unknown', unknown);
  q.querySelectorAll('.as-levels input, .as-evidence input').forEach((i) => { i.disabled = unknown; });
}

function saveDraft() {
  store.set(DRAFT, { key: rerunKey, decision: $('as-decision').value, organisation: $('as-org').value, answers: readState() });
}

function showErr(id, msg) {
  const el = $(id);
  if (!el) return;
  el.textContent = msg || '';
  el.hidden = !msg;
}

async function loadRerun() {
  const q = new URLSearchParams(location.search);
  if (q.has('fresh')) { store.drop(DRAFT); offerSeries(); return; }
  rerunKey = q.get('k') || null;
  const draft = store.get(DRAFT);
  if (!rerunKey && draft?.key) rerunKey = draft.key;
  if (rerunKey) {
    try {
      const series = await rpc('get_assessment_series', { p_key: rerunKey });
      if (!series || !series.runs?.length || series.instrument !== 'dmsc') throw new StoreError('That rerun link is not recognised.');
      const last = series.runs[series.runs.length - 1];
      const payload = JSON.parse(last.payload);
      store.set(KEYSTORE, rerunKey);
      const banner = $('as-rerun');
      banner.hidden = false;
      banner.innerHTML = `<p class="k">Rerun</p><p>You are creating <strong>version ${series.runs.length + 1}</strong> of certificate series <strong>${esc(series.series)}</strong> for ${esc(series.organisation)}. Your answers from <a href="../report/?k=${encodeURIComponent(rerunKey)}">version ${last.version}</a> are filled in: change what has changed. Version ${last.version} stays as it was.</p>
        <p><a class="cta-quiet" href="?fresh=1">Start a new, separate self-check instead →</a></p>`;
      const fromDraft = draft?.key === rerunKey ? draft : null;
      $('as-decision').value = fromDraft?.decision ?? payload.subject.decision ?? '';
      $('as-org').value = fromDraft?.organisation ?? payload.subject.organisation ?? '';
      $('as-role').value = payload.subject.role ?? '';
      state = fromDraft?.answers ?? stateFromAnswers(payload.answers);
      return;
    } catch (e) {
      rerunKey = null;
      showErr('as-form-err', e instanceof StoreError ? `${e.message} Starting a new self-check.` : 'That rerun link could not be read. Starting a new self-check.');
    }
  }
  offerSeries();
  if (draft && !draft.key) {
    $('as-decision').value = draft.decision || '';
    $('as-org').value = draft.organisation || '';
    state = draft.answers || {};
  }
}

// A series issued on this device is offered, never assumed: a shared device should not continue someone else's.
function offerSeries() {
  const kept = store.get(KEYSTORE);
  const el = $('as-offer');
  if (!kept || !el) return;
  el.hidden = false;
  el.innerHTML = `<p>This device holds an earlier self-check. <a href="?k=${encodeURIComponent(kept)}">Rerun it as a new version →</a> <a href="../report/?k=${encodeURIComponent(kept)}">or view its report →</a></p>`;
}

function validate() {
  let ok = true;
  const decision = $('as-decision').value.trim();
  showErr('as-decision-err', decision ? '' : 'Name the decision you are assessing.');
  ok = ok && Boolean(decision);
  const { answers, missing } = collectAnswers(readState());
  for (const [id] of CRITERIA) {
    const miss = missing.includes(id);
    showErr(`q-${id}-err`, miss ? 'Choose what happened and what you could show, or tick "I don\'t know".' : '');
    $(`q-${id}`).toggleAttribute('aria-invalid', miss);
  }
  ok = ok && missing.length === 0;
  const name = $('as-name').value.trim();
  const email = $('as-email').value.trim();
  const org = $('as-org').value.trim();
  showErr('as-name-err', name ? '' : 'Your name is needed for the report.');
  showErr('as-email-err', isEmail(email) ? '' : 'A valid email address, please.');
  showErr('as-org-err', org ? '' : 'Your organisation appears on the certificate.');
  const consent = $('as-consent-processing').checked;
  showErr('as-consent-err', consent ? '' : 'The report cannot be issued without this consent.');
  ok = ok && name && isEmail(email) && org && consent;
  const first = missing[0] ? $(`q-${missing[0]}`) : null;
  return { ok, answers, decision, name, email, org, first };
}

async function submit(ev) {
  ev.preventDefault();
  showErr('as-form-err', '');
  if ($('as-website').value) return;
  const v = validate();
  if (!v.ok) {
    showErr('as-form-err', 'Some answers are missing. They are marked above.');
    (v.first || document.querySelector('.bk-err:not([hidden])'))?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    return;
  }
  const btn = $('as-submit');
  btn.setAttribute('aria-disabled', 'true');
  btn.disabled = true;
  $('as-status').textContent = 'Scoring and signing your report…';
  try {
    const res = await rpc('submit_self_check', { p: {
      key: rerunKey,
      contact: { name: v.name, email: v.email, organisation: v.org, role: $('as-role').value.trim() || null },
      consent: { processing: true, contact: $('as-consent-contact').checked, text_version: CONSENT_VERSION },
      decision: v.decision,
      answers: v.answers,
      context: context(),
      hp: '',
    } });
    const key = res.key || rerunKey;
    store.set(KEYSTORE, key);
    store.drop(DRAFT);
    location.href = `../report/?k=${encodeURIComponent(key)}&v=${res.version}`;
  } catch (e) {
    showErr('as-form-err', e instanceof StoreError ? e.message : 'Something went wrong. Your answers are kept on this device; please try again.');
    $('as-status').textContent = '';
    btn.disabled = false;
    btn.removeAttribute('aria-disabled');
  }
}

async function init() {
  renderStages();
  await loadRerun();
  applyState(state);
  const form = $('as-form');
  form.addEventListener('change', (e) => {
    const m = e.target.name?.match(/^([A-Z]\d)-unknown$/);
    if (m) syncUnknown(m[1]);
    saveDraft();
  });
  form.addEventListener('input', saveDraft);
  form.addEventListener('submit', submit);
}

init();
