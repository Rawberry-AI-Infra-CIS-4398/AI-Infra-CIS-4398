import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { chromium } from 'playwright';

const url = new URL(process.argv[2]);
const required = {
  '/uploadmedical': ['get', 'post'],
  '/uploadedu': ['get', 'post'],
  '/me/system-prompt': ['get', 'put'],
  '/conversations': ['get', 'post'],
  '/conversations/{conversation_id}/messages': ['get'],
  '/chat': ['post'],
  '/chat/stream': ['post'],
  '/conversations/{conversation_id}/shares': ['get', 'put'],
  '/shared': ['get'],
  '/shared/{conversation_id}': ['get'],
};

async function read(path, headers) {
  const response = await fetch(new URL(path, url), { headers, signal: AbortSignal.timeout(30_000) });
  assert.equal(response.status, 200, `GET ${path}`);
  return response.json();
}

assert.equal((await read('/health')).status, 'healthy');
const { paths } = await read('/openapi.json');
const headers = { 'X-User-ID': `demo-smoke-${randomUUID()}` };
assert.equal((await read('/me/system-prompt', headers)).prompt, '');
assert.deepEqual(await read('/conversations', headers), []);
console.log('Cloud Run health, OpenAPI, and read-only prompt and history checks passed.');

const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  const docs = await page.goto(new URL('/docs', url).href);
  assert.equal(docs.status(), 200);
  await page.locator('.opblock-summary-path[data-path="/chat"]').waitFor({ state: 'visible' });
  const health = page.locator('.opblock-get').filter({ has: page.locator('[data-path="/health"]') });
  await health.locator('.opblock-summary-control').click();
  await health.getByRole('button', { name: 'Try it out', exact: true }).click();
  const [result] = await Promise.all([
    page.waitForResponse((response) => new URL(response.url()).pathname === '/health'),
    health.getByRole('button', { name: 'Execute', exact: true }).click(),
  ]);
  assert.equal(result.status(), 200);
  assert.equal((await result.json()).status, 'healthy');
  console.log('Deployed Swagger rendered in Chromium and executed GET /health successfully.');
} finally {
  await browser.close();
}

const missing = Object.entries(required).flatMap(([path, methods]) =>
  methods.filter((method) => !paths[path]?.[method]).map((method) => `${method.toUpperCase()} ${path}`));
if (missing.length) {
  console.error(`Missing demo operations: ${missing.join(', ')}`);
  process.exitCode = 1;
}
