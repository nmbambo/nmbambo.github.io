// "Register interest" on every instrument page. Any element with data-capture="<instrument name>"
// becomes a short form that stores the contact, with POPIA consent, through capture_lead().
import { rpc, context, isEmail, esc, StoreError } from './api.mjs';
import { CONSENT_VERSION } from './config.mjs';

let n = 0;

export function formHtml(instrument, id) {
  const f = (k) => `cap${id}-${k}`;
  return `
  <form class="as-capture" novalidate data-instrument="${esc(instrument)}">
    <p class="k">Register interest · ${esc(instrument)}</p>
    <div class="bk-grid">
      <div class="bk-field"><label class="k" for="${f('name')}">Name</label><input id="${f('name')}" name="name" type="text" autocomplete="name" maxlength="120" required></div>
      <div class="bk-field"><label class="k" for="${f('email')}">Work email</label><input id="${f('email')}" name="email" type="email" autocomplete="email" maxlength="160" required></div>
      <div class="bk-field wide"><label class="k" for="${f('org')}">Organisation</label><input id="${f('org')}" name="organisation" type="text" autocomplete="organization" maxlength="160" required></div>
      <div class="bk-field wide"><label class="k" for="${f('msg')}">The decision or question behind your interest (optional)</label><textarea id="${f('msg')}" name="message" rows="3" maxlength="800"></textarea></div>
      <div class="as-hp" aria-hidden="true"><label for="${f('hp')}">Leave this empty</label><input id="${f('hp')}" name="website" type="text" tabindex="-1" autocomplete="off"></div>
    </div>
    <label class="bk-pick"><input type="checkbox" name="consent" required>
      <span><strong>Contact me about this (required)</strong> Ingqiqo Executables may store these details to respond to my interest in ${esc(instrument)}.</span></label>
    <label class="bk-pick"><input type="checkbox" name="updates">
      <span><strong>Occasional notes (optional)</strong> Also send me occasional notes on decision assurance. I can withdraw at any time.</span></label>
    <p class="bk-hint">Responsible party: Ingqiqo Executables (Pty) Ltd. Held with Supabase in the EU. To see, correct or erase your details, write to nmbambo@ingqiqo-executables.run.</p>
    <p class="cta-row"><button type="submit" class="cta-primary bk-btn">Register interest</button><span class="status" aria-live="polite"></span></p>
    <p class="bk-err" role="alert" hidden></p>
  </form>`;
}

export function leadArgs(instrument, fields, ctx) {
  return {
    p_email: fields.email.trim().toLowerCase(),
    p_name: fields.name.trim(),
    p_org: fields.organisation.trim(),
    p_consent: true,
    p_country: null,
    p_kind: 'interest',
    p_interest: instrument,
    p_message: [fields.message?.trim(), `Consent ${CONSENT_VERSION}: contact about this request; occasional notes: ${fields.updates ? 'yes' : 'no'}`].filter(Boolean).join('\n\n'),
    p_page: ctx.page, p_referrer: ctx.referrer, p_utm_source: ctx.utm_source, p_utm_medium: ctx.utm_medium,
    p_utm_campaign: ctx.utm_campaign, p_ua: ctx.ua,
  };
}

function mount(el) {
  const instrument = el.dataset.capture;
  el.innerHTML = formHtml(instrument, ++n);
  const form = el.querySelector('form');
  const err = form.querySelector('.bk-err');
  const status = form.querySelector('.status');
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    err.hidden = true;
    const x = form.elements;
    if (x.website.value) return;
    const fields = { name: x.name.value, email: x.email.value, organisation: x.organisation.value,
      message: x.message.value, updates: x.updates.checked };
    const problems = [];
    if (!fields.name.trim()) problems.push('your name');
    if (!isEmail(fields.email)) problems.push('a valid email address');
    if (!fields.organisation.trim()) problems.push('your organisation');
    if (!x.consent.checked) problems.push('your consent to be contacted');
    if (problems.length) { err.textContent = `Please add ${problems.join(', ')}.`; err.hidden = false; return; }
    const btn = form.querySelector('button');
    btn.disabled = true;
    status.textContent = 'Sending…';
    try {
      await rpc('capture_lead', leadArgs(instrument, fields, context()));
      el.innerHTML = `<div class="as-capture done"><p class="k">Register interest · ${esc(instrument)}</p><p class="body">Thank you, ${esc(fields.name.trim().split(' ')[0])}. Your interest is recorded and I will be in touch at ${esc(fields.email.trim())}.</p></div>`;
    } catch (ex) {
      err.textContent = ex instanceof StoreError ? ex.message : 'Something went wrong; please try again or write to nmbambo@ingqiqo-executables.run.';
      err.hidden = false;
      status.textContent = '';
      btn.disabled = false;
    }
  });
}

if (typeof document !== 'undefined') document.querySelectorAll('[data-capture]').forEach(mount);
