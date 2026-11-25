import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { expect, test } from '@playwright/test';

test.use({ baseURL: 'http://127.0.0.1:8000', storageState: { cookies: [], origins: [] } });

test('Swagger transport splits a 36 MiB selection and stores every PDF', async ({ page }) => {
  const pdf = execFileSync(process.env.RAWBERRY_PYTHON || 'python', ['-B', '-c',
    "import base64; from pdf_samples import make_pdf; print(base64.b64encode(make_pdf('Large batch source.')).decode())"],
  { cwd: fileURLToPath(new URL('../../../rawberry-api/tests/', import.meta.url)), encoding: 'utf8' }).trim();
  await page.goto('/docs');
  await expect(page.locator('.opblock-summary-path[data-path="/upload"]')).toBeVisible();
  const result = await page.evaluate(async (encoded) => {
    // Add a valid PDF comment before xref; update its pointer so the original
    // small text layer remains readable without manufacturing thousands of chunks.
    const small = atob(encoded);
    const marker = 'startxref\n';
    const xref = Number(small.slice(small.lastIndexOf(marker) + marker.length).split('\n')[0]);
    const padding = '%' + 'x'.repeat(12 * 1024 * 1024) + '\n';
    const tail = small.slice(xref).replace(marker + xref, marker + (xref + padding.length));
    const body = new FormData();
    body.append('domain', 'education');
    for (let index = 0; index < 3; index += 1) {
      body.append('files', new File([small.slice(0, xref), padding, tail], `${index}.pdf`, { type: 'application/pdf' }));
    }
    const user = `large-batch-${crypto.randomUUID()}`;
    // Exercise Swagger's actual request serialization and configured interceptor.
    const response = await ui.fn.fetch({
      url: location.origin + '/upload', method: 'POST', body, headers: { 'X-User-ID': user },
      requestInterceptor: ui.getConfigs().requestInterceptor,
    });
    const listed = await fetch('/upload?domain=education', { headers: { 'X-User-ID': user } }).then((res) => res.json());
    return { status: response.status, batches: response.headers['x-upload-batches'],
      uploaded: response.body, listed };
  }, pdf);
  expect(result.status).toBe(201);
  expect(result.batches).toBe('2');
  expect(result.uploaded.map(({ filename }) => filename)).toEqual(['0.pdf', '1.pdf', '2.pdf']);
  expect(result.listed.map(({ id }) => id).sort())
    .toEqual(result.uploaded.map(({ document_id }) => document_id).sort());
});
