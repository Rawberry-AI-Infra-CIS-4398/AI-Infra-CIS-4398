import assert from 'node:assert/strict';

// Playwright buffers these responses; verify both incremental text and the
// completed persisted result rather than treating the entire SSE body as JSON.
export async function readChatResult(response) {
  const text = await response.text();
  const events = text.split(/\r?\n\r?\n/).filter(Boolean).map((block) => {
    const lines = block.split(/\r?\n/);
    const name = lines.find((line) => line.startsWith('event: '))?.slice(7);
    const data = lines.filter((line) => line.startsWith('data: '))
      .map((line) => line.slice(6)).join('\n');
    return { name, data: JSON.parse(data) };
  });
  const final = events.at(-1);
  assert.equal(final?.name, 'status', 'Chat must end with a status event');
  assert.equal(final.data.status, 'completed', 'Chat must complete successfully');
  const answer = events.filter(({ name }) => name === 'delta')
    .map(({ data }) => data.delta).join('');
  assert.equal(answer, final.data.response.reply, 'Deltas must match the saved answer');
  return final.data.response;
}
