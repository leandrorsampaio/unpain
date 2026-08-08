#!/usr/bin/env node
/* Real-browser smoke test for every application page on an isolated FA_ROOT. */
const { spawn, spawnSync } = require('child_process');
const { once } = require('events');
const fs = require('fs');
const http = require('http');
const net = require('net');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright');

const PROJECT = path.resolve(__dirname, '..');
const PYTHON = path.join(PROJECT, '.venv', 'bin', 'python');
const UVICORN = path.join(PROJECT, '.venv', 'bin', 'uvicorn');
const SCREENSHOTS = path.join(__dirname, 'screenshots');
const TABS = [
  'overview', 'dashboard', 'transactions', 'review', 'rules', 'categories',
  'settlement', 'tax', 'add', 'ingest', 'feedback', 'settings',
];  // Accounts moved into Settings › Accounts; its #accounts deep-link is exercised in the anchor-entry flow below

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      server.close(error => error ? reject(error) : resolve(port));
    });
  });
}

function metaReady(url) {
  return new Promise(resolve => {
    const request = http.get(url, response => {
      response.resume();
      resolve(response.statusCode === 200);
    });
    request.on('error', () => resolve(false));
    request.setTimeout(1000, () => { request.destroy(); resolve(false); });
  });
}

async function waitForServer(url, child) {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error('server exited before becoming ready');
    if (await metaReady(url + '/api/meta')) return;
    await delay(150);
  }
  throw new Error('server did not answer /api/meta within 15 seconds');
}

async function stopServer(child) {
  // A signal-terminated child keeps exitCode null (signalCode is set instead),
  // so "has it exited" must come from the race outcome — re-awaiting a fresh
  // once(child, 'exit') after the event already fired would hang forever and
  // let the event loop drain into a silent exit 0.
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  const exited = once(child, 'exit');
  child.kill('SIGTERM');
  const graceful = await Promise.race([exited.then(() => true), delay(3000).then(() => false)]);
  if (!graceful) {
    child.kill('SIGKILL');
    await exited;
  }
}

function ignoredConsoleError(message) {
  const location = message.location();
  return message.type() === 'error' && /favicon\.ico(?:$|\?)/.test(location.url || '') && /404/.test(message.text());
}

async function main() {
  const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), 'fa-ui-smoke-'));
  let server = null;
  let browser = null;
  let serverOutput = '';
  try {
    const bootstrap = spawnSync(PYTHON, [path.join(__dirname, 'ui_smoke_bootstrap.py'), sandbox], {
      cwd: PROJECT, encoding: 'utf8', env: { ...process.env, PYTHONPYCACHEPREFIX: path.join(sandbox, 'pycache') },
    });
    if (bootstrap.status !== 0) {
      throw new Error(`sandbox bootstrap failed\n${bootstrap.stdout}${bootstrap.stderr}`);
    }

    const port = await freePort();
    const baseUrl = `http://127.0.0.1:${port}`;
    server = spawn(UVICORN, ['app.server:app', '--host', '127.0.0.1', '--port', String(port)], {
      cwd: PROJECT,
      env: { ...process.env, FA_ROOT: sandbox, PYTHONPYCACHEPREFIX: path.join(sandbox, 'pycache') },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    server.stdout.on('data', chunk => { serverOutput += chunk; });
    server.stderr.on('data', chunk => { serverOutput += chunk; });
    await waitForServer(baseUrl, server);

    fs.mkdirSync(SCREENSHOTS, { recursive: true });
    browser = await chromium.launch({ headless: true, args: ['--disable-gpu'] });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    const results = new Map(TABS.map(tab => [tab, { rendered: false, errors: [] }]));
    let activeTab = 'startup';
    const recordError = detail => {
      errors.push({ tab: activeTab, detail });
      if (results.has(activeTab)) results.get(activeTab).errors.push(detail);
    };
    page.on('pageerror', error => recordError(`pageerror: ${error.stack || error.message}`));
    page.on('console', message => {
      if (message.type() === 'error' && !ignoredConsoleError(message)) {
        recordError(`console: ${message.text()} (${message.location().url || 'unknown source'})`);
      }
    });

    for (const tab of TABS) {
      activeTab = tab;
      const result = results.get(tab);
      try {
        await page.goto(`${baseUrl}/?smoke=${tab}#${tab}`, { waitUntil: 'networkidle', timeout: 15000 });
        await page.waitForFunction(() => (document.querySelector('#main')?.textContent || '').trim().length > 0,
          null, { timeout: 10000 });
        await page.waitForTimeout(300);
        await page.screenshot({ path: path.join(SCREENSHOTS, `${tab}.png`), fullPage: true, animations: 'disabled' });
        result.rendered = true;
      } catch (error) {
        const detail = `render: ${error.stack || error.message}`;
        result.errors.push(detail);
        errors.push({ tab, detail });
      }
    }

    activeTab = 'dashboard';
    try {
      await page.goto(`${baseUrl}/?smoke=coverage#dashboard`, { waitUntil: 'networkidle', timeout: 15000 });
      await page.locator('#coverage-card .coverage-grid').waitFor({ state: 'visible', timeout: 10000 });
      await page.locator('.coverage-cell[data-account="card1-person1"][data-month="6"].missing').waitFor();
      await page.locator('.coverage-cell[data-account="cash-person2"][data-month="6"].muted').waitFor();
      await page.locator('.coverage-anchor.chip-good', { hasText: 'reconciled' }).waitFor();
      await page.locator('.coverage-anchor.chip-bad', { hasText: 'balance mismatch' }).waitFor();
      await page.locator('.coverage-anchor.chip-neutral', { hasText: 'no anchor' }).first().waitFor();

      // June is inside the fixture's active range and has missing non-low-activity accounts.
      await page.evaluate(() => { toggleMonth(6, 'closed'); });
      const gate = page.locator('md-dialog.app-dialog');
      await gate.waitFor({ state: 'visible', timeout: 5000 });
      await gate.locator('[slot="headline"]').filter({ hasText: 'Close June?' }).waitFor();
      // One dialog, both answers to "is this safe to call settled": what has no
      // statement, and what the integrity check found.
      await gate.locator('.coverage-gap-list').waitFor({ timeout: 5000 });
      await gate.getByText(/Integrity check/).waitFor({ timeout: 5000 });
      await page.screenshot({ path: path.join(SCREENSHOTS, 'coverage-gate.png'), fullPage: true, animations: 'disabled' });
      await gate.locator('.confirm-cancel').click();
      await gate.waitFor({ state: 'detached', timeout: 5000 });
      const juneAfterCancel = await page.evaluate(async () => (await (await fetch('/api/summary?year=2026')).json()).months_state['2026-06']);
      if (juneAfterCancel === 'closed') throw new Error('cancelled coverage gate closed June');

      // May is outside the active range, so it has no coverage gap — but the integrity
      // check still runs, and this fixture carries a balance mismatch. The gate appears
      // on the strength of the check alone, with no statement list.
      await page.evaluate(() => { toggleMonth(5, 'closed'); });
      const mayGate = page.locator('md-dialog.app-dialog');
      await mayGate.waitFor({ state: 'visible', timeout: 5000 });
      await mayGate.getByText(/Integrity check/).waitFor({ timeout: 5000 });
      if (await mayGate.locator('.coverage-gap-list').count()) {
        throw new Error('a month with no coverage gap listed missing statements');
      }
      await mayGate.locator('.confirm-go').click();
      await page.waitForFunction(async () => (await (await fetch('/api/summary?year=2026')).json()).months_state['2026-05'] === 'closed');
      await page.evaluate(() => { toggleMonth(5, 'open'); });
      await page.waitForFunction(async () => (await (await fetch('/api/summary?year=2026')).json()).months_state['2026-05'] === 'open');

      await page.evaluate(() => { toggleMonth(6, 'closed'); });
      await page.locator('md-dialog.app-dialog .confirm-go').click();
      await page.waitForFunction(async () => (await (await fetch('/api/summary?year=2026')).json()).months_state['2026-06'] === 'closed');

      await page.goto(`${baseUrl}/?smoke=anchor-entry#accounts`, { waitUntil: 'networkidle', timeout: 15000 });
      await page.locator('.acct-card[data-id="card1-person1"] md-text-button', { hasText: 'Record balance' }).click();
      const anchorDialog = page.locator('md-dialog.app-dialog:has(.anchor-save)');
      await anchorDialog.locator('.anchor-balance').evaluate(field => {
        field.value = '1234.56';
        field.dispatchEvent(new Event('input', { bubbles: true }));
      });
      await page.screenshot({ path: path.join(SCREENSHOTS, 'balance-anchor-modal.png'), fullPage: true, animations: 'disabled' });
      await anchorDialog.locator('.anchor-save').click();
      await anchorDialog.waitFor({ state: 'detached', timeout: 5000 });
      const savedAnchor = await page.evaluate(async () => (await (await fetch('/api/coverage?year=2026')).json()).anchors['card1-person1']);
      if (!savedAnchor || savedAnchor.status !== 'none') throw new Error('manual anchor was not recorded as the account’s first anchor');
      // Saving used to raise a modal you had to acknowledge; it is a toast now. Same
      // contract — the write is confirmed on screen — asserted on the toast instead.
      const savedToast = page.locator('#toast-host .toast', { hasText: 'Balance anchor recorded' });
      await savedToast.waitFor({ state: 'visible', timeout: 5000 });
      await savedToast.locator('.toast-close').click();
      await savedToast.waitFor({ state: 'detached', timeout: 5000 });

      // Restore a genuinely clean store for the first doctor run: reopen June
      // and replace the deliberately mismatched screenshot anchor with its real balance.
      await page.evaluate(async () => {
        await fetch('/api/close', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ year: 2026, month: 6, state: 'open' }) });
      });
      const transactionDir = path.join(sandbox, 'data', '2026', 'transactions');
      const rawTransactions = fs.readdirSync(transactionDir).filter(name => name.endsWith('.jsonl')).flatMap(name =>
        fs.readFileSync(path.join(transactionDir, name), 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse));
      const bank2Balance = rawTransactions.filter(txn => txn.account === 'bank2-person2')
        .reduce((sum, txn) => sum + Math.round(Number(txn.amount_original) * 100), 0) / 100;
      const anchorPath = path.join(sandbox, 'data', '2026', 'balance-anchors.json');
      const anchorRows = JSON.parse(fs.readFileSync(anchorPath, 'utf8'));
      anchorRows.find(item => item.account === 'bank2-person2' && item.date === '2026-06-30').balance = bank2Balance;
      fs.writeFileSync(anchorPath, JSON.stringify(anchorRows, null, 2));

      await page.evaluate(() => openDoctor());  // health check now lives in Settings > Data
      let doctorDialog = page.locator('md-dialog.app-dialog:has(.doctor-body)');
      await doctorDialog.locator('.doctor-run').click();
      await doctorDialog.locator('.doctor-clear', { hasText: 'All clear' }).waitFor({ timeout: 10000 });
      await page.screenshot({ path: path.join(SCREENSHOTS, 'doctor-all-clear.png'), fullPage: true, animations: 'disabled' });
      await doctorDialog.locator('.doctor-close').click();
      await doctorDialog.waitFor({ state: 'detached', timeout: 5000 });

      // Seed one error, warning and info finding, then exercise grouped rendering.
      const target = rawTransactions.find(txn => txn.kind === 'normal' && txn.date.startsWith('2026-06'));
      const decisionsPath = path.join(sandbox, 'data', '2026', 'decisions.json');
      const decisions = fs.existsSync(decisionsPath) ? JSON.parse(fs.readFileSync(decisionsPath, 'utf8')) : {};
      decisions[target.id] = { category: 'missing/category', splits: [{ amount: 0, category: 'missing/category' }] };
      fs.writeFileSync(decisionsPath, JSON.stringify(decisions, null, 2));
      const marker = { id: 'doctor-ui-marker', account: 'bank1-person1', date: '2026-07-10',
        amount_original: -333, currency: 'EUR', amount_eur: -333, kind: 'internal-transfer',
        transfer_reason: 'marker', counterparty: 'Internal account move', purpose: '', source: { file: 'doctor-ui.csv' } };
      fs.writeFileSync(path.join(transactionDir, 'doctor-ui-marker.jsonl'), `${JSON.stringify(marker)}\n`);
      fs.writeFileSync(path.join(sandbox, 'rules', 'budgets.json'), JSON.stringify({ budgets: { 'missing/category': 50 } }, null, 2));

      await page.evaluate(() => openDoctor());  // health check now lives in Settings > Data
      doctorDialog = page.locator('md-dialog.app-dialog:has(.doctor-body)');
      await doctorDialog.locator('.doctor-run').click();
      await doctorDialog.locator('.doctor-error').waitFor({ timeout: 10000 });
      await doctorDialog.locator('.doctor-warning').waitFor();
      await doctorDialog.locator('.doctor-info').waitFor();
      await page.screenshot({ path: path.join(SCREENSHOTS, 'doctor-findings.png'), fullPage: true, animations: 'disabled' });
      await doctorDialog.locator('.doctor-close').click();
    } catch (error) {
      const detail = `coverage interaction: ${error.stack || error.message}`;
      results.get('dashboard').errors.push(detail);
      errors.push({ tab: 'dashboard', detail });
    }

    activeTab = 'transactions';
    try {
      await page.goto(`${baseUrl}/?smoke=interaction#transactions`, { waitUntil: 'networkidle', timeout: 15000 });
      const edit = page.locator('#t-list .txn-item > .txn-grid .txn-edit').first();
      await edit.waitFor({ state: 'visible', timeout: 10000 });
      await edit.click();
      const dialog = page.locator('md-dialog.app-dialog');
      await dialog.waitFor({ state: 'visible', timeout: 5000 });
      await dialog.locator('.e-cancel').click();
      await dialog.waitFor({ state: 'detached', timeout: 5000 });
    } catch (error) {
      const detail = `interaction: ${error.stack || error.message}`;
      results.get('transactions').errors.push(detail);
      errors.push({ tab: 'transactions', detail });
    }

    console.log('UI smoke summary:');
    for (const tab of TABS) {
      const result = results.get(tab);
      const ok = result.rendered && result.errors.length === 0;
      console.log(`  ${ok ? 'OK  ' : 'FAIL'} #${tab}${result.errors.length ? ` (${result.errors.length} error(s))` : ''}`);
    }
    if (errors.length) {
      console.error('\nBrowser errors:');
      for (const error of errors) console.error(`  #${error.tab}: ${error.detail}`);
      throw new Error(`UI smoke test collected ${errors.length} error(s)`);
    }
    console.log(`Screenshots: ${SCREENSHOTS}`);
  } catch (error) {
    // Set the exit code before cleanup: if cleanup ever drains the event loop,
    // the process must still exit non-zero.
    process.exitCode = 1;
    if (serverOutput) console.error(`\nServer output:\n${serverOutput}`);
    throw error;
  } finally {
    if (browser) await browser.close();
    await stopServer(server);
    fs.rmSync(sandbox, { recursive: true, force: true });
  }
}

main().catch(error => {
  console.error(error.stack || error.message);
  process.exit(1);
});
