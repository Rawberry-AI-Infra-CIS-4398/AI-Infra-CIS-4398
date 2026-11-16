import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { expect, test } from '@playwright/test';

test.use({ baseURL: 'http://127.0.0.1:8000', storageState: { cookies: [], origins: [] } });

async function execute(page, method, path, { user, id, body, files, status = 200 } = {}) {
  const operation = page.locator(`.opblock-${method.toLowerCase()}`).filter({
    has: page.locator(`.opblock-summary-path[data-path="${path}"]`),
  });
  const summary = operation.locator('.opblock-summary-control');
  await summary.click();
  const tryIt = operation.getByRole('button', { name: 'Try it out', exact: true });
  const executeButton = operation.getByRole('button', { name: 'Execute', exact: true });
  await expect(tryIt.or(executeButton)).toBeVisible();
  if (await tryIt.isVisible()) await tryIt.click();
  if (user) await operation.locator('tr[data-param-name="X-User-ID"] input').fill(user);
  if (id) await operation.locator('tr[data-param-name="conversation_id"] input').fill(id);
  if (body) await operation.locator('textarea.body-param__text').fill(JSON.stringify(body));
  if (files) {
    await operation.locator('tr[data-param-name="X-Developer-Key"] input').fill('test-upload-key');
    for (const [index, file] of files.entries()) {
      if (index > 0) await operation.getByRole('button', { name: 'Add string item', exact: true }).click();
      await operation.locator('input[type="file"]').nth(index).setInputFiles(file);
    }
  }

  const requestPath = id ? path.replace('{conversation_id}', id) : path;
  const [response] = await Promise.all([
    page.waitForResponse((res) => new URL(res.url()).pathname === requestPath && res.request().method() === method),
    executeButton.click(),
  ]);
  expect(response.status(), `${method} ${path}`).toBe(status);
  await expect(operation.locator('.live-responses-table .response-col_status').last()).toHaveText(new RegExp(`^${status}\\b`));
  await summary.click();
  return response.json();
}

test('Swagger demonstrates prompts, follow-up context, history, and sharing', async ({ page }) => {
  const owner = `swagger-owner-${randomUUID()}`;
  const recipient = `swagger-recipient-${randomUUID()}`;
  const prompt = 'Explain the answer in one sentence.';
  await page.goto('/docs');
  await execute(page, 'PUT', '/me/system-prompt', { user: owner, body: { prompt } });
  expect(await execute(page, 'GET', '/me/system-prompt', { user: owner })).toMatchObject({ prompt });
  expect(await execute(page, 'GET', '/me/system-prompt', { user: recipient })).toMatchObject({ prompt: '' });

  const { id } = await execute(page, 'POST', '/conversations', {
    user: owner, status: 201, body: { title: 'Swagger demo', domain: 'education' },
  });
  const first = 'What was the Continental Congress?';
  const followUp = 'What did it do next?';
  for (const message of [first, followUp]) {
    const { reply } = await execute(page, 'POST', '/chat', { user: owner, body: { conversation_id: id, message } });
    expect(reply).toContain(first);
    if (message === followUp) expect(reply).toContain('Conversation history (prior messages):');
  }
  const history = await execute(page, 'GET', '/conversations/{conversation_id}/messages', { user: owner, id });
  expect(history.map(({ role, status }) => [role, status])).toEqual([
    ['user', 'completed'], ['assistant', 'completed'], ['user', 'completed'], ['assistant', 'completed'],
  ]);
  expect([history[0].content, history[2].content]).toEqual([first, followUp]);

  const shares = '/conversations/{conversation_id}/shares';
  const shared = '/shared/{conversation_id}';
  await execute(page, 'PUT', shares, { user: owner, id, body: { user_ids: [recipient] } });
  expect(await execute(page, 'GET', shared, { user: recipient, id })).toMatchObject({ messages: history });
  await execute(page, 'PUT', shares, { user: owner, id, body: { user_ids: [] } });
  await execute(page, 'GET', shared, { user: recipient, id, status: 404 });
});

test('Swagger uploads PDF, DOCX, and TXT and retrieves evidence from the correct corpus', async ({ page }) => {
  const user = `swagger-ingestion-${randomUUID()}`;
  const samples = JSON.parse(execFileSync(process.env.RAWBERRY_PYTHON || 'python', ['-B', '-c', `
import base64
import json
from docx_samples import make_docx
from pdf_samples import make_pdf

samples = [make_pdf('Harborcrane registry code is BANYAN-41.'), make_docx('Schoolhouse registry code is LARCH-83.')]
print(json.dumps([base64.b64encode(sample).decode() for sample in samples]))
`], { cwd: fileURLToPath(new URL('../../../rawberry-api/tests/', import.meta.url)), encoding: 'utf8' }));
  await page.goto('/docs');
  const medical = await execute(page, 'POST', '/uploadmedical', {
    files: [{ name: `medical-${user}.pdf`, mimeType: 'application/pdf', buffer: Buffer.from(samples[0], 'base64') }],
  });
  const education = await execute(page, 'POST', '/uploadedu', {
    files: [
      { name: `education-${user}.docx`, mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', buffer: Buffer.from(samples[1], 'base64') },
      { name: `education-${user}.txt`, mimeType: 'text/plain', buffer: Buffer.from('Archive inventory notes. '.repeat(100)) },
    ],
  });
  expect([...medical, ...education].map(({ embedding_status }) => embedding_status)).toEqual(['completed', 'completed', 'completed']);
  expect(education[1].chunk_count).toBeGreaterThan(1);

  for (const [domain, expected, other] of [['medical', 'BANYAN-41', 'LARCH-83'], ['education', 'LARCH-83', 'BANYAN-41']]) {
    const { id } = await execute(page, 'POST', '/conversations', {
      user, status: 201, body: { title: `Swagger ${domain} retrieval`, domain },
    });
    const { reply } = await execute(page, 'POST', '/chat', {
      user, body: { conversation_id: id, message: 'What is the harborcrane or schoolhouse registry code?' },
    });
    expect(reply).toContain(expected);
    expect(reply).not.toContain(other);
  }
});
