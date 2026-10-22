#!/usr/bin/env node
// End-to-end check of the self-check, report, rerun, verify and interest capture in Chromium.
//
// Local (default): the site is served by `python3 -m http.server`; calls to the Supabase store are
// answered by the real database functions on a local Postgres (PGHOST/PGPORT/PGDATABASE, default
// /tmp, 5433, t) with supabase/migrations/0002_assessments.sql applied, run as the anon role.
// Live (--live): calls go to the real store. Use only with a test identity you then erase.
//
// Run: NODE_PATH=$(npm root -g) node tests/assess/e2e_assess.mjs [--live] [--shots DIR]
// Not picked up by `node --test` (the file name does not end in .test.mjs).
import { spawn, execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, readFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createRequire } from 'node:module';
import assert from 'node:assert/strict';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
process.env.PLAYWRIGHT_BROWSERS_PATH ||= '/opt/pw-browsers';

const ROOT = resolve(new URL('../..', import.meta.url).pathname);
const args = process.argv.slice(2);
const LIVE = args.includes('--live');
const SHOTS = args.includes('--shots') ? args[args.indexOf('--shots') + 1] : null;
const STORE = 'https://zlwsqhlzwhqwxnekaejn.supabase.co';
const PORT = 8000 + Math.floor(Math.random() * 900);
const BASE = `http://127.0.0.1:${PORT}`;
const EMAIL = LIVE ? 'e2e-selfcheck@ingqiqo-executables.run' : 'e2e@example.org';
if (SHOTS) mkdirSync(SHOTS, { recursive: true });

// ---------- the store, answered by local Postgres ----------
const pg = ['-h', process.env.PGHOST || '/tmp', '-p', process.env.PGPORT || '5433', '-U', 'postgres', '-d', process.env.PGDATABASE || 't', '-At', '-v', 'ON_ERROR_STOP=1'];
const lit = (v, type) => (v === null || v === undefined ? 'null' : `'${String(typeof v === 'object' ? JSON.stringify(v) : v).replace(/'/g, "''")}'::${type}`);
function localRpc(fn, body) {
  const allowed = ['submit_self_check', 'get_assessment_series', 'verify_certificate', 'capture_lead'];
  if (!allowed.includes(fn)) return { status: 404, body: { message: 'not found' } };
  const named = Object.entries(body).map(([k, v]) => `${k} => ${lit(v, k === 'p' ? 'jsonb' : k === 'p_consent' ? 'boolean' : 'text')}`).join(', ');
  try {
    const out = execFileSync('psql', [...pg, '-c', `set role anon; select to_jsonb(public.${fn}(${named}))`], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
    const line = out.trim().split('\n').pop();
    return { status: 200, body: line ? JSON.parse(line) : null };
  } catch (e) {
    const m = String(e.stderr).match(/ERROR:\s+(.*)/);
    return { status: 400, body: { message: m ? m[1] : 'error' } };
  }
}
async function answerLocally(context) {
  await context.route(`${STORE}/rest/v1/rpc/*`, async (route) => {
    const fn = new URL(route.request().url()).pathname.split('/').pop();
    const r = localRpc(fn, JSON.parse(route.request().postData() || '{}'));
    await route.fulfill({ status: r.status, contentType: 'application/json', body: JSON.stringify(r.body), headers: { 'access-control-allow-origin': '*' } });
  });
}
const sql = (q) => execFileSync('psql', [...pg, '-c', q], { encoding: 'utf8' }).trim();

// ---------- helpers ----------
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function shot(page, name) { if (SHOTS) await page.screenshot({ path: join(SHOTS, `${name}.png`), fullPage: true }); }
async function noSideScroll(page, label) {
  const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert.ok(over <= 1, `${label}: page scrolls sideways by ${over}px`);
}
const ANSWERS = {
  F1: [3, 'artefact'], F2: [2, 'policy'], F3: [1, 'testimony'], E1: [3, 'testimony'], E2: 'unknown', E3: 'unknown',
  J1: [2, 'policy'], J2: [2, 'artefact'], J3: [0, 'none'], X1: [1, 'testimony'], X2: [1, 'testimony'], X3: [2, 'policy'],
  R1: [0, 'none'], R2: [1, 'testimony'], R3: [0, 'none'],
};
async function answer(page, id, a) {
  const unknown = page.locator(`input[name="${id}-unknown"]`);
  if (a === 'unknown') { if (!(await unknown.isChecked())) await unknown.check(); return; }
  if (await unknown.isChecked()) await unknown.uncheck();
  await page.locator(`input[name="${id}-level"][value="${a[0]}"]`).check();
  await page.locator(`input[name="${id}-evidence"][value="${a[1]}"]`).check();
}
async function contact(page) {
  await page.fill('#as-name', 'E2E Tester');
  await page.fill('#as-email', EMAIL);
  await page.fill('#as-org', 'E2E Test Organisation');
  await page.check('#as-consent-processing');
}

// ---------- run ----------
const server = spawn('python3', ['-m', 'http.server', String(PORT), '--bind', '127.0.0.1'], { cwd: ROOT, stdio: 'ignore' });
await sleep(800);
const browser = await chromium.launch();
const dl = mkdtempSync(join(tmpdir(), 'iqx-'));
let issued = [];
try {
  const ctx = await browser.newContext({ acceptDownloads: true, viewport: { width: 1280, height: 900 } });
  if (!LIVE) await answerLocally(ctx);
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));

  // 1. Empty submit shows what is missing.
  await page.goto(`${BASE}/assessment/self-check/`);
  await page.waitForSelector('#q-R3');
  assert.equal(await page.locator('.as-q').count(), 15);
  await page.click('#as-submit');
  assert.ok(await page.locator('#as-form-err').isVisible());
  assert.ok(await page.locator('#q-F1-err').isVisible());
  assert.ok(await page.locator('#as-consent-err').isVisible());
  console.log('ok  empty submit is refused, with each gap marked');

  // 2. First version.
  await page.fill('#as-decision', '2026 credit limit policy change');
  for (const [id, a] of Object.entries(ANSWERS)) await answer(page, id, a);
  assert.ok(await page.locator('input[name="E2-level"]').first().isDisabled(), 'unknown disables the level choices');
  await contact(page);
  await page.check('#as-consent-contact');
  await shot(page, 'selfcheck-desktop');
  await Promise.all([page.waitForURL(/\/assessment\/report\/\?k=.+&v=1/), page.click('#as-submit')]);
  await page.waitForSelector('.as-cert h1');
  const reportUrl1 = page.url();
  const cert1 = (await page.locator('.as-cert .as-mono').first().textContent()).trim();
  assert.match(cert1, /^IQX-DMSC-[0-9A-Z]{8}-V1$/);
  issued.push(cert1);
  const report1 = await page.locator('#as-report').textContent();
  assert.match(report1, /Self-reported, not verified/);
  assert.match(report1, /Insufficient evidence/); // Evidence: two of three unknown
  assert.doesNotMatch(report1, /What changed/);
  assert.match(report1, /Capped by evidence at 1/); // E1: 3 claimed on testimony
  console.log(`ok  version 1 issued: ${cert1}`);
  await shot(page, 'report-v1');

  // 3. Download the certified file and verify it.
  const [download] = await Promise.all([page.waitForEvent('download'), page.click('#as-download')]);
  const file1 = join(dl, await download.suggestedFilename());
  await download.saveAs(file1);
  const doc1 = JSON.parse(readFileSync(file1, 'utf8'));
  assert.equal(doc1.cert_id, cert1);
  if (!LIVE) await page.emulateMedia({ media: 'print' });
  if (SHOTS) await page.pdf({ path: join(SHOTS, 'report-v1.pdf'), format: 'A4', printBackground: true });
  await page.emulateMedia({ media: 'screen' });

  await page.goto(`${BASE}/verify/`);
  await page.setInputFiles('#v-file', file1);
  await page.click('#v-submit');
  await page.waitForSelector('.as-verdict');
  let v = await page.locator('#v-result').textContent();
  assert.match(v, /Genuine/);
  assert.match(v, /matches the register exactly/);
  assert.match(v, /1, the latest/);
  console.log('ok  certified file verifies against the register');
  await shot(page, 'verify-ok');

  // 4. A tampered file is caught.
  const tampered = join(dl, 'tampered.json');
  writeFileSync(tampered, JSON.stringify({ ...doc1, payload: doc1.payload.replace('E2E Test Organisation', 'Someone Else') }));
  await page.goto(`${BASE}/verify/`);
  await page.setInputFiles('#v-file', tampered);
  await page.click('#v-submit');
  await page.waitForSelector('.as-verdict.bad');
  assert.match(await page.locator('#v-result').textContent(), /altered/);
  console.log('ok  an altered file is caught');

  // 5. Rerun: answers come back filled in; change some; version 2 carries the diff.
  await page.goto(`${BASE}/assessment/self-check/?fresh=1`); // the device remembers the series and offers it
  await page.waitForSelector('#as-offer:not([hidden])');
  await page.goto(reportUrl1);
  await Promise.all([page.waitForURL(/self-check\/\?k=/), page.click('#as-rerun-link')]);
  await page.waitForSelector('#as-rerun:not([hidden])');
  assert.match(await page.locator('#as-rerun').textContent(), /version 2/);
  assert.ok(await page.locator('input[name="F1-level"][value="3"]').isChecked(), 'previous answers are filled in');
  assert.equal(await page.inputValue('#as-decision'), '2026 credit limit policy change');
  await answer(page, 'E1', [3, 'artefact']);
  await answer(page, 'E2', [2, 'policy']);
  await answer(page, 'J3', [3, 'testimony']);
  await answer(page, 'R1', [2, 'policy']);
  await contact(page);
  await Promise.all([page.waitForURL(/v=2/), page.click('#as-submit')]);
  await page.waitForSelector('.as-diff');
  const report2 = await page.locator('#as-report').textContent();
  const cert2 = cert1.replace(/-V1$/, '-V2');
  assert.match(report2, new RegExp(cert2));
  assert.match(report2, /What changed since version 1/);
  assert.match(report2, /4 of 15 criteria changed/);
  assert.match(report2, /Improvement backed by a record from the decision itself/);
  assert.match(report2, /reported but not recorded/);
  assert.match(report2, /Evidence can now be scored/);
  assert.match(await page.locator('#as-versions').textContent(), /Version 1/);
  issued.push(cert2);
  console.log(`ok  version 2 issued with a diff: ${cert2}`);
  await shot(page, 'report-v2');
  if (SHOTS) { await page.emulateMedia({ media: 'print' }); await page.pdf({ path: join(SHOTS, 'report-v2.pdf'), format: 'A4', printBackground: true }); await page.emulateMedia({ media: 'screen' }); }

  // 6. Version 1 still verifies, and says it has been superseded.
  await page.goto(`${BASE}/verify/?id=${cert1}`);
  await page.waitForSelector('.as-verdict');
  v = await page.locator('#v-result').textContent();
  assert.match(v, /Genuine/);
  assert.match(v, new RegExp(`A later version exists: ${cert2}`));
  await page.goto(`${BASE}/verify/?id=IQX-DMSC-ZZZZZZZZ-V1`);
  await page.waitForSelector('.as-verdict');
  assert.match(await page.locator('#v-result').textContent(), /Not found/);
  console.log('ok  version 1 kept and marked superseded; unknown IDs are not found');

  // 7. Interest capture on an instrument page, and on the Dojo.
  await page.goto(`${BASE}/work/organisational-debt-diagnostic/`);
  const cap = page.locator('form.as-capture');
  await cap.locator('button').click();
  assert.match(await cap.locator('.bk-err').textContent(), /consent/);
  await cap.locator('input[name=name]').fill('E2E Tester');
  await cap.locator('input[name=email]').fill(EMAIL);
  await cap.locator('input[name=organisation]').fill('E2E Test Organisation');
  await cap.locator('textarea').fill('Testing the interest form.');
  await cap.locator('input[name=consent]').check();
  await cap.locator('button').click();
  await page.waitForSelector('.as-capture.done');
  console.log('ok  interest captured on an instrument page');
  await shot(page, 'capture-done');
  await page.goto(`${BASE}/resources/dojo/`);
  await page.waitForSelector('#dojo-interest form.as-capture');
  console.log('ok  the Dojo carries the interest form');

  if (!LIVE) {
    const ev = sql(`select string_agg(kind || ':' || interest, ',' order by created_at) from lead_events e join leads l on l.id = e.lead_id where l.email = '${EMAIL}'`);
    assert.equal(ev, 'assessment:Decision Maturity Self-Check,assessment:Decision Maturity Self-Check,interest:Organisational Debt Diagnostic');
    assert.equal(sql(`select consent from leads where email = '${EMAIL}'`), 't');
    console.log('ok  the store holds the contact, both results and the interest, with consent');
  }

  // 8. Phone width: nothing scrolls sideways.
  const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true });
  if (!LIVE) await answerLocally(phone);
  const p2 = await phone.newPage();
  for (const path of ['/assessment/self-check/', `/verify/?id=${cert2}`, '/work/decision-maturity-assessment/']) {
    await p2.goto(`${BASE}${path}`);
    await p2.waitForLoadState('networkidle');
    await noSideScroll(p2, path);
  }
  await shot(p2, 'verify-phone');
  await p2.goto(`${BASE}/assessment/self-check/?fresh=1`);
  await shot(p2, 'selfcheck-phone');
  console.log('ok  no sideways scroll at 390px');

  assert.deepEqual(errors, []);
  console.log(`\nAll checks passed${LIVE ? ' against the live store' : ''}. Issued: ${issued.join(', ')}`);
} finally {
  await browser.close();
  server.kill();
}
