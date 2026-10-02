// Organiser page (choose, create the link, reply) and attendee view (the confirmed meeting).
// The request is read from the URL fragment, which no server ever sees. Everything from the payload
// goes into the page as text, never as HTML.
import { parseFragment, PayloadError, isHttpsUrl } from './payload.mjs';
import { formatDay, formatTime, tzLabel, sameWallClock, toLocalInput, fromLocalInput, isValidTimeZone } from './tz.mjs';
import { invite, publish, deterministicUuid } from './ics.mjs';
import {
  ORGANISER_EMAIL, SIGNAL_NUMBER, SAST, PLATFORM_NAME, describeSlot, platformInstructions,
  confirmationMessage, proposeMessage, declineMessage,
} from './messages.mjs';
import { h, $, download, loadJson } from './ui.mjs';

let req = null;
let availability = null;

const show = (id, on) => { $(id).hidden = !on; };
const fact = (dt, ...dd) => h('div', {}, h('dt', {}, dt), h('dd', {}, ...dd));

function browserZone() {
  try { const tz = Intl.DateTimeFormat().resolvedOptions().timeZone; return isValidTimeZone(tz) ? tz : null; } catch { return null; }
}

function slotText(slot) { return describeSlot(slot, req.viewerTz); }

/* ---------- organiser ---------- */

function renderRequest() {
  $('#cf-label').textContent = 'Book · Organiser';
  $('#cf-title').textContent = `A request from ${req.name}`;
  $('#cf-role').textContent = 'Nothing is booked until you confirm. Choose the time and the platform, then reply.';
  $('#cf-facts').replaceChildren(
    fact('Who', req.name),
    fact('Email', h('a', { class: 'inline', href: `mailto:${req.email}` }, req.email)),
    fact('Organisation', req.org || 'Not given'),
    fact('Length asked', `${req.duration} minutes`),
    fact('Platform asked', PLATFORM_NAME[req.platform]),
    fact('Their zone', req.viewerTz),
  );
  $('#cf-reason').textContent = req.reason;
  const asked = [];
  if (req.preferred) asked.push(['Preferred', slotText(req.preferred)]);
  if (req.alternative) asked.push(['Alternative', slotText(req.alternative)]);
  if (req.suggestion) asked.push(['Their suggestion', req.suggestion]);
  $('#cf-asked').replaceChildren(...asked.map(([k, v]) => h('li', {}, h('strong', {}, `${k}: `), v)));
}

function renderChoices() {
  const choices = [];
  if (req.preferred) choices.push(['preferred', 'Preferred', slotText(req.preferred)]);
  if (req.alternative) choices.push(['alternative', 'Alternative', slotText(req.alternative)]);
  choices.push(['other', 'Another time', 'Set it below, in SAST.']);
  $('#cf-choices').replaceChildren(...choices.map(([value, name, text], i) => h('label', { class: 'bk-pick' },
    h('input', { type: 'radio', name: 'cftime', value, checked: i === 0 }), h('span', {}, h('strong', {}, name), ` ${text}`))));
  const other = $('#cf-other');
  other.min = toLocalInput(new Date(), SAST);
  other.addEventListener('focus', () => { document.querySelector('input[name=cftime][value=other]').checked = true; refresh(); });
  const durations = [...new Set([...(availability ? availability.durations : [30, 45, 60]), req.duration])].sort((a, b) => a - b);
  $('#cf-durations').replaceChildren(...durations.map((d) => h('label', { class: 'bk-opt' },
    h('input', { type: 'radio', name: 'cfdur', value: d, checked: d === req.duration }), h('span', {}, `${d} minutes`))));
  document.querySelector(`input[name=cfplat][value=${req.platform}]`).checked = true;
}

const selected = (name) => (document.querySelector(`input[name=${name}]:checked`) || {}).value;

function chosenStart() {
  const which = selected('cftime');
  if (which === 'preferred') return new Date(req.preferred.start);
  if (which === 'alternative') return new Date(req.alternative.start);
  return fromLocalInput($('#cf-other').value, SAST);
}

/** Read the form into a confirmed meeting, or a list of what is missing. */
function readChoice() {
  const problems = [];
  const start = chosenStart();
  if (!start) problems.push('Choose a time.');
  const duration = Number(selected('cfdur'));
  const platform = selected('cfplat');
  let link = ''; let signalNumber = false;
  if (platform === 'proton-meet') {
    link = $('#cf-proton-link').value.trim();
    if (!isHttpsUrl(link)) problems.push('Paste the Proton Meet link (it starts with https://).');
  } else {
    signalNumber = $('#cf-signal-number').checked;
    link = signalNumber ? '' : $('#cf-signal-link').value.trim();
    if (!signalNumber && !isHttpsUrl(link)) problems.push('Paste a Signal call link, or choose to call at your number.');
  }
  if (problems.length) return { problems };
  const end = new Date(start.getTime() + duration * 60000);
  return {
    problems,
    confirmed: { start: start.toISOString(), end: end.toISOString(), platform, link, signalNumber, uid: deterministicUuid(`${req.id}:meeting`) },
  };
}

function meetingFor(confirmed) {
  const instructions = platformInstructions(confirmed.platform, confirmed);
  return {
    uid: confirmed.uid, start: confirmed.start, end: confirmed.end,
    summary: `Conversation: Nkosinathi Mbambo and ${req.name}`,
    reason: req.reason, platformInstructions: instructions,
    link: confirmed.link,
    location: confirmed.link || `Signal call to ${SIGNAL_NUMBER}`,
    organizer: { name: 'Nkosinathi Mbambo', email: ORGANISER_EMAIL },
    attendee: { name: req.name, email: req.email },
  };
}

function setLink(a, href, ok) {
  a.setAttribute('href', ok ? href : '#');
  a.setAttribute('aria-disabled', ok ? 'false' : 'true');
}

function refresh() {
  const choice = readChoice();
  const when = chosenStart();
  const pending = choice.problems.length > 0;
  $('#cf-signal').hidden = selected('cfplat') !== 'signal';
  $('#cf-proton').hidden = selected('cfplat') !== 'proton-meet';
  $('#cf-signal-link').disabled = $('#cf-signal-number').checked;
  const dur = Number(selected('cfdur'));
  $('#cf-when').textContent = when
    ? `Chosen: ${slotText({ start: when.toISOString(), end: new Date(when.getTime() + dur * 60000).toISOString() })}.`
    : 'No time chosen yet.';
  $('#cf-invite').setAttribute('aria-disabled', pending ? 'true' : 'false');
  setLink($('#cf-confirm'), pending ? '#' : confirmationMessage(req, choice.confirmed).href, !pending);
  const other = fromLocalInput($('#cf-other').value, SAST);
  const proposeOk = Boolean(other);
  setLink($('#cf-propose'), proposeOk ? proposeMessage(req, { start: other.toISOString(), end: new Date(other.getTime() + dur * 60000).toISOString() }).href : '#', proposeOk);
  setLink($('#cf-decline'), declineMessage(req).href, true);
  return choice;
}

function organiserHint(message) { $('#cf-hint').textContent = message; }

function wireOrganiser() {
  const root = $('#cf-organiser');
  root.addEventListener('input', refresh);
  root.addEventListener('change', refresh);
  $('#cf-invite').addEventListener('click', () => {
    const choice = refresh();
    if (choice.problems.length) { organiserHint(choice.problems.join(' ')); return; }
    organiserHint('');
    download('invite.ics', invite(meetingFor(choice.confirmed)));
  });
  $('#cf-confirm').addEventListener('click', (e) => {
    const choice = refresh();
    if (choice.problems.length) { e.preventDefault(); organiserHint(choice.problems.join(' ')); } else organiserHint('');
  });
  $('#cf-propose').addEventListener('click', (e) => {
    if ($('#cf-propose').getAttribute('aria-disabled') === 'true') { e.preventDefault(); organiserHint('Set “Another time” first, then propose it.'); } else organiserHint('');
  });
  refresh();
}

/* ---------- attendee ---------- */

function renderAttendee() {
  const c = req.confirmed;
  const slot = { start: c.start, end: c.end };
  $('#cf-label').textContent = 'Book · Confirmed';
  $('#cf-title').textContent = 'Your conversation is confirmed';
  $('#cf-role').textContent = `Nkosinathi Mbambo and ${req.name}.`;
  const s = new Date(c.start); const e = new Date(c.end);
  const facts = [
    fact('When (SAST)', `${formatDay(s, SAST)}, ${formatTime(s, SAST)} to ${formatTime(e, SAST)}`),
  ];
  const here = browserZone() || req.viewerTz;
  if (!sameWallClock(s, SAST, here)) {
    facts.push(fact(`Your time (${here})`, `${formatDay(s, here)}, ${formatTime(s, here)} to ${formatTime(e, here)} ${tzLabel(s, here)}`));
  }
  facts.push(fact('Length', `${Math.round((e - s) / 60000)} minutes`), fact('Platform', PLATFORM_NAME[c.platform]));
  if (c.link) facts.push(fact('Link', h('a', { class: 'inline', href: c.link, target: '_blank', rel: 'noopener' }, c.link)));
  $('#at-facts').replaceChildren(...facts);
  $('#at-instructions').textContent = platformInstructions(c.platform, c);
  const join = $('#at-join');
  if (c.link) { join.href = c.link; join.hidden = false; }
  $('#at-add').addEventListener('click', () => download('invite.ics', publish(meetingFor(c))));
}

/* ---------- start ---------- */

function fail(error) {
  $('#cf-title').textContent = 'Confirm a conversation';
  $('#cf-role').textContent = 'Open the link from a request to see it here.';
  $('#cf-err-msg').textContent = error instanceof PayloadError
    ? `The request in the link is missing or damaged (${error.message}).`
    : 'The request in the link could not be read.';
  show('#cf-error', true);
}

async function main() {
  try {
    req = parseFragment(location.hash);
  } catch (error) { fail(error); return; }
  if (req.view === 'attendee') {
    renderAttendee();
    show('#cf-attendee', true);
  } else {
    try { availability = await loadJson(new URL('./availability.json', import.meta.url), { optional: true }); } catch { availability = null; }
    renderRequest(); renderChoices(); wireOrganiser();
    show('#cf-organiser', true);
  }
  document.documentElement.dataset.bookReady = '1';
}

addEventListener('hashchange', () => location.reload());
main();
