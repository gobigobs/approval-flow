#!/usr/bin/env node
// Real screenshots of the running mock-Slack dashboard — no mockups.
// The demo server must already be running (./run.sh starts it).
// Usage: node docs/capture.mjs --url http://127.0.0.1:18765/ --out docs/x.png [--wait-text "PAID"]
// Playwright is resolved from (in order): $PLAYWRIGHT_MODULE, the shared
// workspace playwright install, or a local `playwright` package.
import fs from 'node:fs';

const args = Object.fromEntries(
  process.argv.slice(2).map((a, i, arr) =>
    a.startsWith('--') ? [a.slice(2), arr[i + 1]] : []).filter(Boolean));
const url = args.url || 'http://127.0.0.1:18765/';
const out = args.out || 'docs/shot.png';
const waitText = args['wait-text'] || '';

const candidates = [
  process.env.PLAYWRIGHT_MODULE,
  '/home/hatch/workspace/.scratch/shot/node_modules/playwright/index.mjs',
  'playwright',
].filter(Boolean);

let chromium = null;
let lastErr = null;
for (const mod of candidates) {
  try {
    ({ chromium } = await import(mod));
    break;
  } catch (err) { lastErr = err; }
}
if (!chromium) {
  console.error('SCREENSHOT FAILED: playwright not found. Tried:', candidates.join(', '));
  console.error('Install it once: npm i -D playwright && npx playwright install chromium');
  console.error('Last error:', String(lastErr).slice(0, 300));
  process.exit(1);
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
try {
  await page.goto(url, { waitUntil: 'networkidle' });
  if (waitText) await page.waitForSelector(`text=${waitText}`, { timeout: 30000 });
  await page.waitForTimeout(700); // let styles settle
  fs.mkdirSync('docs', { recursive: true });
  await page.screenshot({ path: out, fullPage: true });
  console.log('screenshot saved:', out);
} catch (err) {
  console.error('SCREENSHOT FAILED:', err.message);
  process.exitCode = 1;
} finally {
  await browser.close();
}
