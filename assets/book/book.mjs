// Visitor page: choose a time, describe the decision, prepare a request.
// Nothing leaves the browser except through the mailto link or the clipboard text the visitor triggers.
import { computeSlots, groupByDay } from './slots.mjs';
import { formatDay, formatTime, tzLabel, sameWallClock, ymd, isValidTimeZone } from './tz.mjs';
import { validate, isEmail, PayloadError, REASON_MIN, REASON_MAX } from './payload.mjs';
import { requestMessage, describeSlot, PLATFORM_NAME, SAST } from './messages.mjs';
import { requestHold } from './ics.mjs';
import { h, $, download, copyText, loadJson, uuid } from './ui.mjs';
import { readSource } from '../source.mjs';

const state = {
  availability: null, busy: [], duration: 30, role: 'preferred',
  picks: { preferred: null, alternative: null }, slots: [], none: false,
  viewerTz: 'UTC', request: null, message: null,
};

function viewerZone() {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return isValidTimeZone(tz) ? tz : 'UTC';
  } catch { return 'UTC'; }
}

const slotByMs = (ms) => state.slots.find((s) => s.start.getTime() === ms) || null;
const pickedSlot = (role) => (state.picks[role] === null ? null : slotByMs(state.picks[role]));
const asPayloadSlot = (s) => (s ? { start: s.start.toISOString(), end: s.end.toISOString() } : null);

function slotLabel(s) {
  const tz = state.viewerTz;
  if (sameWallClock(s.start, SAST, tz)) return { main: formatTime(s.start, SAST), sub: 'SAST' };
  const sastDay = ymd(s.start, SAST) !== ymd(s.start, tz) ? `${formatDay(s.start, SAST, { weekday: 'short', year: false })} ` : '';
  return { main: formatTime(s.start, tz), sub: `${sastDay}${formatTime(s.start, SAST)} SAST` };
}

function renderDurations() {
  const host = $('#bk-durations');
  host.replaceChildren(...state.availability.durations.map((d) => h('label', { class: 'bk-opt' },
    h('input', { type: 'radio', name: 'duration', value: d, checked: d === state.duration }),
    h('span', {}, `${d} minutes`))));
  host.addEventListener('change', (e) => {
    if (e.target.name !== 'duration') return;
    state.duration = Number(e.target.value);
    renderDays();
  });
}

function renderTz() {
  const t = $('#bk-tz');
  const now = new Date();
  t.textContent = sameWallClock(now, SAST, state.viewerTz)
    ? 'All times are SAST, South African Standard Time.'
    : `Times show in your zone (${state.viewerTz}) and in SAST, South African Standard Time, where I keep my diary.`;
}

function renderDays() {
  const { availability, busy, duration } = state;
  state.slots = computeSlots({ availability, busy, now: new Date(), duration });
  // A pick survives a change of length only if that start time is still on offer.
  for (const r of ['preferred', 'alternative']) if (state.picks[r] !== null && !slotByMs(state.picks[r])) state.picks[r] = null;
  const host = $('#bk-days');
  const days = groupByDay(state.slots, state.viewerTz);
  if (!days.length) {
    host.replaceChildren(h('p', { class: 'status' }, 'No times are open in the next three weeks. Tick “None of these suit me” and tell me what would.'));
    paint();
    return;
  }
  host.replaceChildren(...days.map((day, i) => h('details', { class: 'bk-day', open: i === 0, 'data-date': day.date },
    h('summary', {}, h('span', { class: 'bk-dname' }, formatDay(day.slots[0].start, state.viewerTz, { year: false })),
      h('span', { class: 'bk-dcount' }, `${day.slots.length} times`), h('span', { class: 'bk-dmark' })),
    h('div', { class: 'bk-times' }, day.slots.map((s) => {
      const l = slotLabel(s);
      return h('button', { type: 'button', class: 'bk-slot', 'aria-pressed': 'false', 'data-start': s.start.toISOString() },
        h('span', { class: 'bk-t' }, l.main), h('span', { class: 'bk-s' }, l.sub));
    })))));
  paint();
}

function paint() {
  for (const b of document.querySelectorAll('.bk-slot')) {
    const ms = Date.parse(b.dataset.start);
    const role = state.picks.preferred === ms ? 'preferred' : state.picks.alternative === ms ? 'alternative' : '';
    b.setAttribute('aria-pressed', role ? 'true' : 'false');
    b.dataset.role = role;
  }
  for (const d of document.querySelectorAll('.bk-day')) {
    const marks = [...d.querySelectorAll('.bk-slot[data-role]')].map((b) => b.dataset.role).filter(Boolean);
    d.querySelector('.bk-dmark').textContent = marks.map((m) => (m === 'preferred' ? 'Preferred' : 'Alternative')).join(' · ');
  }
  const p = pickedSlot('preferred'); const a = pickedSlot('alternative');
  const parts = [];
  if (p) parts.push(`Preferred: ${describeSlot(asPayloadSlot(p), state.viewerTz)}.`);
  if (a) parts.push(`Alternative: ${describeSlot(asPayloadSlot(a), state.viewerTz)}.`);
  $('#bk-picks').textContent = state.none ? 'You will suggest a time in your own words.'
    : parts.length ? parts.join(' ') : 'Nothing chosen yet.';
}

function pick(ms) {
  const other = state.role === 'preferred' ? 'alternative' : 'preferred';
  if (state.picks[state.role] === ms) state.picks[state.role] = null;
  else {
    state.picks[state.role] = ms;
    if (state.picks[other] === ms) state.picks[other] = null;
  }
  if (state.picks.preferred === null && state.picks.alternative !== null) {
    state.picks.preferred = state.picks.alternative; state.picks.alternative = null;
  }
  paint();
}

function fieldError(id, message) {
  const err = $(`#${id}-err`);
  const field = $(`#${id}`);
  if (err) { err.textContent = message || ''; err.hidden = !message; }
  if (field) { if (message) field.setAttribute('aria-invalid', 'true'); else field.removeAttribute('aria-invalid'); }
  return message;
}

function updateCount() {
  const n = $('#bk-reason').value.length;
  const c = $('#bk-count');
  c.textContent = `${n} of ${REASON_MAX} characters, ${REASON_MIN} at least`;
  c.classList.toggle('short', n > 0 && n < REASON_MIN);
}

function collect() {
  const v = (id) => $(id).value.trim();
  return {
    name: v('#bk-name'), email: v('#bk-email'), org: v('#bk-org'), reason: v('#bk-reason'),
    suggestion: state.none ? v('#bk-suggestion') : '',
    platform: document.querySelector('input[name=platform]:checked').value,
  };
}

function check(f) {
  const errors = [];
  const add = (id, msg) => { if (fieldError(id, msg)) errors.push(id); };
  add('bk-name', f.name ? '' : 'Please add your name.');
  add('bk-email', !f.email ? 'Please add your email.' : isEmail(f.email) ? '' : 'That email does not look right.');
  add('bk-reason', f.reason.length < REASON_MIN ? `A little more, please: at least ${REASON_MIN} characters.`
    : f.reason.length > REASON_MAX ? `Please keep it to ${REASON_MAX} characters.` : '');
  add('bk-suggestion', state.none && f.suggestion.length < 3 ? 'Say what time would suit you.' : '');
  const timeErr = $('#bk-time-err');
  const noTime = !state.none && !pickedSlot('preferred');
  timeErr.textContent = noTime ? 'Choose a preferred time, or tick “None of these suit me”.' : '';
  timeErr.hidden = !noTime;
  if (noTime) errors.push('bk-time');
  return errors;
}

function buildRequest(f) {
  return {
    v: 1, id: uuid(), createdAt: new Date().toISOString(),
    name: f.name, email: f.email, org: f.org, reason: f.reason,
    duration: state.duration, platform: f.platform,
    preferred: state.none ? null : asPayloadSlot(pickedSlot('preferred')),
    alternative: state.none ? null : asPayloadSlot(pickedSlot('alternative')),
    suggestion: f.suggestion, viewerTz: state.viewerTz,
    ...(readSource() ? { source: readSource() } : {}),
  };
}

// A local event, with no personal data, through the site's event-sourcing modules. Never blocks the request.
async function recordLocally(req) {
  try {
    const [{ openStore }, { createEventSourced }, { makeEvent }] = await Promise.all([
      import('../es/store.mjs'), import('../es/eventsourced.mjs'), import('../es/events.mjs'),
    ]);
    const es = createEventSourced({ store: await openStore() });
    const stream = `booking-${req.id}`;
    await es.append(stream, [makeEvent({
      type: 'BookingRequested', stream, meta: { source: 'book' },
      data: { duration: req.duration, platform: req.platform, hasAlternative: Boolean(req.alternative), hasSuggestion: Boolean(req.suggestion) },
    })]);
  } catch { /* the request does not depend on it */ }
}

function showDone(req) {
  const msg = requestMessage(req);
  state.request = req; state.message = msg;
  $('#bk-summary').textContent = `${req.duration} minutes on ${PLATFORM_NAME[req.platform]}. ${
    req.preferred ? `Preferred: ${describeSlot(req.preferred, req.viewerTz)}.` : `Suggested: ${req.suggestion}`}`;
  $('#bk-mailto').setAttribute('href', msg.href);
  $('#bk-long').hidden = !msg.shortened;
  $('#bk-copy-text').value = msg.text;
  $('#bk-copy-status').textContent = '';
  $('#bk-hold-li').hidden = !req.preferred;
  $('#bk-form').hidden = true;
  const done = $('#bk-done');
  done.hidden = false;
  $('#b-done').setAttribute('tabindex', '-1');
  $('#b-done').focus({ preventScroll: true });
  done.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function wire() {
  $('#bk-days').addEventListener('click', (e) => {
    const b = e.target.closest('.bk-slot');
    if (b && !state.none) pick(Date.parse(b.dataset.start));
  });
  $('#bk-role-set').addEventListener('change', (e) => { if (e.target.name === 'role') state.role = e.target.value; });
  $('#bk-none').addEventListener('change', (e) => {
    state.none = e.target.checked;
    $('#bk-suggest').hidden = !state.none;
    $('#bk-days').toggleAttribute('inert', state.none);
    $('#bk-days').classList.toggle('off', state.none);
    if (state.none) { state.picks.preferred = null; state.picks.alternative = null; $('#bk-time-err').hidden = true; $('#bk-suggestion').focus(); }
    paint();
  });
  $('#bk-reason').addEventListener('input', updateCount);
  $('#bk-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const f = collect();
    const errors = check(f);
    const status = $('#bk-form-status');
    if (errors.length) {
      status.textContent = 'A few things need your attention.';
      const first = errors[0] === 'bk-time' ? ($('#bk-days summary') || $('#bk-none')) : $(`#${errors[0]}`);
      first.focus();
      return;
    }
    status.textContent = '';
    let req;
    try { req = validate(buildRequest(f)); } catch (err) {
      status.textContent = err instanceof PayloadError ? `Please check your details: ${err.message}.` : 'Something went wrong preparing the request.';
      return;
    }
    showDone(req);
    recordLocally(req);
  });
  $('#bk-copy').addEventListener('click', async () => {
    const ok = await copyText(state.message.text);
    $('#bk-copy-status').textContent = ok ? 'Copied.' : 'Could not copy. The text is below, ready to select.';
    if (!ok) document.querySelector('.bk-text').open = true;
  });
  $('#bk-hold').addEventListener('click', () => download('request-hold.ics', requestHold(state.request)));
  $('#bk-edit').addEventListener('click', () => {
    $('#bk-done').hidden = true; $('#bk-form').hidden = false;
    $('#b-time').scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}

async function main() {
  state.viewerTz = viewerZone();
  try {
    const here = (p) => new URL(p, import.meta.url);
    state.availability = await loadJson(here('./availability.json'));
    const busy = await loadJson(here('../../data/busy.json'), { optional: true });
    state.busy = Array.isArray(busy) ? busy : [];
  } catch {
    $('#bk-days').replaceChildren(h('p', { class: 'status' }, 'The diary did not load. Tick “None of these suit me” and tell me what would, or write to me from the contact section.'));
    state.availability = null;
  }
  if (state.availability) {
    state.duration = state.availability.defaultDuration;
    renderDurations(); renderTz(); renderDays();
  }
  wire(); updateCount();
  document.documentElement.dataset.bookReady = '1';
}

main();
