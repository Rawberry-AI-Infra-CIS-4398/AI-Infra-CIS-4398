import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

test.use({
  baseURL: 'http://127.0.0.1:8000',
  storageState: { cookies: [], origins: [] },
});

test('RAWBerry stores a system prompt and shares a transcript with one recipient', async ({ request }) => {
  const runId = randomUUID();
  const owner = { 'X-User-ID': `demo-owner-${runId}` };
  const recipient = { 'X-User-ID': `demo-recipient-${runId}` };
  const outsider = { 'X-User-ID': `demo-outsider-${runId}` };
  const prompt = 'Answer briefly using plain language.';

  const saved = await request.put('/me/system-prompt', { headers: owner, data: { prompt } });
  await expect(saved).toBeOK();
  const ownerPrompt = await request.get('/me/system-prompt', { headers: owner });
  await expect(ownerPrompt).toBeOK();
  expect(await ownerPrompt.json()).toMatchObject({ prompt });
  const recipientPrompt = await request.get('/me/system-prompt', { headers: recipient });
  await expect(recipientPrompt).toBeOK();
  expect(await recipientPrompt.json()).toMatchObject({ prompt: '' });

  const created = await request.post('/conversations', {
    headers: owner,
    data: { title: 'Demo sharing', domain: 'education' },
  });
  expect(created.status()).toBe(201);
  const { id } = await created.json();
  const turn = { conversation_id: id, message: 'What did the Continental Congress do?' };
  const chat = await request.post('/chat', { headers: owner, data: turn });
  await expect(chat).toBeOK();
  const { reply } = await chat.json();
  expect(reply).not.toContain(prompt);

  const shares = `/conversations/${id}/shares`;
  const shared = `/shared/${id}`;
  const granted = await request.put(shares, { headers: owner, data: { user_ids: [recipient['X-User-ID']] } });
  await expect(granted).toBeOK();
  const inbox = await request.get('/shared', { headers: recipient });
  await expect(inbox).toBeOK();
  expect(await inbox.json()).toMatchObject([{ id }]);
  const transcript = await request.get(shared, { headers: recipient });
  await expect(transcript).toBeOK();
  expect(await transcript.json()).toMatchObject({
    conversation: { id },
    messages: [
      { role: 'user', content: turn.message, status: 'completed' },
      { role: 'assistant', content: reply, status: 'completed' },
    ],
  });
  expect((await request.get(shared, { headers: outsider })).status()).toBe(404);
  expect((await request.post('/chat', { headers: recipient, data: turn })).status()).toBe(404);

  const revoked = await request.put(shares, { headers: owner, data: { user_ids: [] } });
  await expect(revoked).toBeOK();
  expect((await request.get(shared, { headers: recipient })).status()).toBe(404);

  const cleared = await request.put('/me/system-prompt', { headers: owner, data: { prompt: '' } });
  await expect(cleared).toBeOK();
  expect(await cleared.json()).toMatchObject({ prompt: '' });
});
