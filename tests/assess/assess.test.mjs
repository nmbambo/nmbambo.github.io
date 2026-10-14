// Fixtures were produced by the real database functions (supabase/migrations/0002_assessments.sql)
// on a local Postgres, so these tests read the shapes the browser will actually receive.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { rpc, sha256Hex, exportDocument, checkExport, StoreError, isEmail, esc } from '../../assets/assess/api.mjs';
import { CRITERIA, STAGES, collectAnswers, stateFromAnswers } from '../../assets/assess/dmsc.mjs';
import { renderReport } from '../../assets/assess/render.mjs';
import { verdictHtml } from '../../assets/assess/verify.mjs';
import { leadArgs, formHtml } from '../../assets/assess/capture.mjs';

const fx = JSON.parse(readFileSync(new URL('./fixtures.json', import.meta.url)));
const dma1 = JSON.parse(readFileSync(new URL('./dma-v1.json', import.meta.url)));
const dma2 = JSON.parse(readFileSync(new URL('./dma-v2.json', import.meta.url)));

test('fifteen criteria, three per stage, matching the rubric ids', () => {
  assert.equal(CRITERIA.length, 15);
  for (const [s] of STAGES) assert.equal(CRITERIA.filter((c) => c[1] === s).length, 3);
  for (const c of CRITERIA) assert.equal(c[4].length, 4, c[0]);
  assert.deepEqual(CRITERIA.map((c) => c[0]), ['F1', 'F2', 'F3', 'E1', 'E2', 'E3', 'J1', 'J2', 'J3', 'X1', 'X2', 'X3', 'R1', 'R2', 'R3']);
});

test('collectAnswers reports what is missing and keeps unknowns as unknown', () => {
  const state = { F1: { level: 2, evidence: 'policy' }, F2: { unknown: true }, F3: { level: 1 } };
  const { answers, missing } = collectAnswers(state);
  assert.deepEqual(answers.F1, { level: 2, evidence: 'policy', unknown: false });
  assert.deepEqual(answers.F2, { unknown: true });
  assert.ok(missing.includes('F3'));
  assert.equal(missing.length, 13);
  assert.ok(collectAnswers({ F1: { level: 2, evidence: 'made-up' } }).missing.includes('F1'));
});

test('a stored version round-trips into the form for a rerun', () => {
  const payload = JSON.parse(fx.v1.payload);
  const state = stateFromAnswers(payload.answers);
  const { answers, missing } = collectAnswers(state);
  assert.equal(missing.length, 0);
  for (const [id] of CRITERIA) {
    const stored = payload.answers[id];
    if (stored.unknown) assert.deepEqual(answers[id], { unknown: true }, id);
    else assert.deepEqual(answers[id], stored, id);
  }
});

test('the fingerprint is SHA-256 over the exact payload text', async () => {
  assert.equal(await sha256Hex(fx.v2.payload), fx.v2.sha256);
  assert.equal(await sha256Hex(dma2.payload), dma2.sha256);
});

test('an exported file checks out, and any edit is caught', async () => {
  const doc = exportDocument(fx.v2);
  const ok = await checkExport(JSON.parse(JSON.stringify(doc)));
  assert.equal(ok.ok, true);
  assert.equal(ok.certId, fx.v2.cert_id);

  const tampered = { ...doc, payload: doc.payload.replace('"score": 3', '"score": 2') };
  assert.notEqual(tampered.payload, doc.payload);
  const bad = await checkExport(tampered);
  assert.equal(bad.ok, false);
  assert.match(bad.reason, /altered/);

  const relabelled = { ...doc, cert_id: 'IQX-DMSC-AAAAAAAA-V1' };
  assert.match((await checkExport(relabelled)).reason, /does not match/);
  assert.equal((await checkExport({ format: 'other' })).ok, false);
});

test('self-check payload carries certificate, statement, scores and, from v2, the diff', () => {
  const p1 = JSON.parse(fx.v1.payload);
  const p2 = JSON.parse(fx.v2.payload);
  assert.equal(p1.certificate.basis, 'self-reported');
  assert.match(p1.certificate.statement, /not a certification of the organisation/);
  assert.equal(p1.diff, null);
  assert.equal(p2.certificate.version, 2);
  assert.equal(p2.certificate.supersedes, fx.v1.cert_id);
  assert.equal(p2.scores.stages.E.status, 'scored');
  assert.equal(p1.scores.stages.E.status, 'insufficient');
  assert.ok(p2.diff.criteria.some((c) => c.id === 'E1' && c.change === 'up'));
  assert.ok(p2.diff.summary.some((s) => /self-reported/.test(s)));
  // the certificate never carries the respondent's name or email
  assert.doesNotMatch(fx.v2.payload, /t@example\.org|Test <b>Person/);
});

test('the report escapes everything it is given', () => {
  const html = renderReport(JSON.parse(fx.v2.payload), fx.v2, {});
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /Acme &amp; Sons &lt;script&gt;/);
  assert.match(html, /Credit &quot;limit&quot; change/);
  assert.match(html, /What changed since version 1/);
  assert.match(html, new RegExp(fx.v2.sha256));
  assert.match(html, /Self-reported, not verified/);
});

test('the report renders an assessor-scored engagement and its diff', () => {
  const html = renderReport(JSON.parse(dma2.payload), dma2, {});
  assert.match(html, /Decision Maturity Assessment/);
  assert.match(html, /Scored by an Ingqiqo assessor/);
  assert.match(html, /Median/);
  assert.match(html, /What changed since version 1/);
  const first = renderReport(JSON.parse(dma1.payload), dma1, {});
  assert.doesNotMatch(first, /What changed/);
});

test('verify verdicts', () => {
  const reg = { status: 'valid', signature_valid: true, cert_id: 'IQX-DMSC-X-V1', sha256: 'abc', version: 1, latest_cert_id: 'IQX-DMSC-X-V2',
    instrument: { name: 'Decision Maturity Self-Check', version: '1.0' }, organisation: 'A<b>', basis: 'self-reported', issued_at: '2026-10-14T10:00:00Z', profile: 'F1' };
  const h = verdictHtml(reg, { ok: true, hash: 'abc' });
  assert.match(h, /Genuine/);
  assert.match(h, /matches the register exactly/);
  assert.match(h, /A later version exists: IQX-DMSC-X-V2/);
  assert.match(h, /A&lt;b&gt;/);
  assert.match(verdictHtml(reg, { ok: true, hash: 'zzz' }), /does not match the register/);
  assert.match(verdictHtml({ ...reg, signature_valid: false }), /does not check/);
  assert.match(verdictHtml({ status: 'withdrawn', cert_id: 'X', withdrawn_at: '2026-10-14T10:00:00Z', reason: 'Erased' }), /Withdrawn/);
  assert.match(verdictHtml({ status: 'unknown', cert_id: 'X' }), /Not found/);
});

test('interest capture sends consent and the instrument, and escapes its label', () => {
  const args = leadArgs('Organisational Debt Diagnostic', { name: ' Zodwa ', email: ' Z@Example.org ', organisation: 'Org', message: 'Why', updates: false },
    { page: '/work/x/', referrer: null, utm_source: 'linkedin', utm_medium: null, utm_campaign: null, ua: 'ua' });
  assert.equal(args.p_email, 'z@example.org');
  assert.equal(args.p_consent, true);
  assert.equal(args.p_kind, 'interest');
  assert.equal(args.p_interest, 'Organisational Debt Diagnostic');
  assert.match(args.p_message, /occasional notes: no/);
  assert.equal(Object.keys(args).length, 14);
  assert.doesNotMatch(formHtml('<img src=x>', 1), /<img src=x>/);
});

test('rpc turns database errors into readable messages', async () => {
  const fail = async () => ({ ok: false, status: 400, text: async () => JSON.stringify({ message: 'a valid email address is required' }) });
  await assert.rejects(rpc('submit_self_check', {}, { fetchImpl: fail }), (e) => e instanceof StoreError && e.message === 'A valid email address is required.');
  const down = async () => { throw new TypeError('network'); };
  await assert.rejects(rpc('x', {}, { fetchImpl: down }), /could not be reached/);
  const ok = async (url, init) => ({ ok: true, status: 200, text: async () => JSON.stringify({ url, apikey: init.headers.apikey }) });
  const res = await rpc('verify_certificate', { p_cert_id: 'x' }, { fetchImpl: ok, store: { url: 'https://s', key: 'k' } });
  assert.deepEqual(res, { url: 'https://s/rest/v1/rpc/verify_certificate', apikey: 'k' });
});

test('helpers', () => {
  assert.ok(isEmail('a@b.co'));
  assert.ok(!isEmail('a@b'));
  assert.equal(esc(`<a href="x">'&`), '&lt;a href=&quot;x&quot;&gt;&#39;&amp;');
});
