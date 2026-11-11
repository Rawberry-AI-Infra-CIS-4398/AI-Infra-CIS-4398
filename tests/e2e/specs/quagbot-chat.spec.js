import { expect, test } from '@playwright/test';

test('QuagBot restores chat history and answers from an uploaded document', async ({ page }, testInfo) => {
  const runId = Date.now();
  const firstQuestion = `E2E first question ${runId}`;
  const followUp = `E2E follow-up question ${runId}`;
  const chat = page.locator('[data-chat-window]');
  const input = chat.getByRole('textbox', { name: 'Chat message' });
  // The mock echoes each question in a Markdown paragraph. User bubbles use divs.
  const assistantEcho = (question) => chat.locator('p').and(chat.getByText(question, { exact: true }));

  await page.goto('/');
  await expect(input).toBeEnabled();

  const conversationCreated = page.waitForResponse((response) =>
    response.url().endsWith('/api/conversations') && response.request().method() === 'POST');
  const firstSend = page.waitForResponse((response) =>
    response.url().endsWith('/api/messages') && response.request().method() === 'POST');
  await input.fill(firstQuestion);
  await page.getByRole('button', { name: 'Send' }).click();
  const createdResponse = await conversationCreated;
  expect(createdResponse.status()).toBe(201);
  const firstResponse = await firstSend;
  expect(firstResponse.status()).toBe(200);
  const firstPayload = await firstResponse.json();
  const conversationId = firstResponse.request().postDataJSON().conversationId;
  expect(conversationId).toBe((await createdResponse.json()).id);
  expect(firstPayload.reply).toContain(firstQuestion);
  await expect(assistantEcho(firstQuestion)).toBeVisible();

  const secondSend = page.waitForResponse((response) =>
    response.url().endsWith('/api/messages') && response.request().method() === 'POST');
  await input.fill(followUp);
  await page.getByRole('button', { name: 'Send' }).click();
  const secondResponse = await secondSend;
  expect(secondResponse.status()).toBe(200);
  expect(secondResponse.request().postDataJSON().conversationId).toBe(conversationId);
  const secondPayload = await secondResponse.json();
  expect(secondPayload.reply).toContain('Conversation history (prior messages):');
  expect(secondPayload.reply).toContain(firstQuestion);
  expect(secondPayload.reply).toContain(followUp);
  await expect(assistantEcho(followUp)).toBeVisible();

  const history = await page.request.get(`/api/conversations/${conversationId}/messages`);
  expect(history.status()).toBe(200);
  expect(await history.json()).toMatchObject([
    { role: 'user', content: expect.stringContaining(firstQuestion), status: 'completed' },
    { role: 'assistant', content: firstPayload.reply, status: 'completed' },
    { role: 'user', content: expect.stringContaining(followUp), status: 'completed' },
    { role: 'assistant', content: secondPayload.reply, status: 'completed' },
  ]);
  await page.screenshot({ path: testInfo.outputPath('chat-after-follow-up.png'), fullPage: true });

  await page.reload();
  await page.getByRole('button', { name: firstQuestion, exact: true }).click();
  for (const question of [firstQuestion, followUp]) {
    const turnText = chat.getByText(question, { exact: true });
    await expect(chat.locator('div').and(turnText)).toBeVisible();
    await expect(assistantEcho(question)).toBeVisible();
  }
  await page.screenshot({ path: testInfo.outputPath('chat-after-reload.png'), fullPage: true });

  const fact = 'The harborcrane reference code is ORCHID-7.';
  const source = new FormData();
  // CRLF and the invisible character exercise cleaning before chunking.
  const text = `${fact.replace('reference', 'ref\u200berence')}\r\n\r\n` + 'Archive inventory notes. '.repeat(100);
  source.append('files', new Blob([text], { type: 'text/plain' }), `demo-source-${runId}.txt`);
  const upload = await fetch('http://127.0.0.1:8000/uploadedu', {
    method: 'POST',
    headers: { 'X-Developer-Key': 'test-upload-key' },
    body: source,
  });
  expect(upload.status).toBe(200);
  const [document] = await upload.json();
  expect(document.chunk_count).toBeGreaterThan(1);

  const sourceAnswer = page.waitForResponse((response) =>
    response.url().endsWith('/api/messages') && response.request().method() === 'POST');
  await input.fill('What is the harborcrane reference code?');
  await chat.getByRole('button', { name: 'Send' }).click();
  const sourceResponse = await sourceAnswer;
  expect(sourceResponse.status()).toBe(200);
  expect((await sourceResponse.json()).reply).toContain(fact);
  await expect(chat.getByText(fact, { exact: false })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('chat-document-retrieval.png'), fullPage: true });
});
