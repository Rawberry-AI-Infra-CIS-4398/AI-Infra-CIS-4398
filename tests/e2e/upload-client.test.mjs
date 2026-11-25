import assert from 'node:assert/strict';
import { File } from 'node:buffer';
import test from 'node:test';
import '../../rawberry-api/app/static/upload-client.js';

const { createUploadFetch } = globalThis.RAWBerryUploads;
const smallLimits = { maxBatchBytes: 14_000, maxFileBytes: 10_000 };
const result = (form) => form.getAll('files').map((file) => ({
  success: true, document_id: file.name, filename: file.name, embedding_status: 'completed',
}));
const ok = (form) => new Response(JSON.stringify(result(form)), { status: 201 });
function form(count, size = 3000) {
  const body = new FormData();
  body.append('domain', 'education');
  for (let index = 0; index < count; index += 1) {
    body.append('files', new File([new Uint8Array(size)], `${index}.pdf`, { type: 'application/pdf' }));
  }
  return body;
}

test('three 12 MiB PDFs are sent in two requests below the platform ceiling', async () => {
  const counts = [];
  const upload = createUploadFetch(async (url, init) => {
    counts.push(init.body.getAll('files').length);
    const encoded = await new Response(init.body).blob();
    assert.ok(encoded.size < 30 * 1024 * 1024);
    return ok(init.body);
  });
  const response = await upload('https://api.example/upload', { method: 'POST', body: form(3, 12 * 1024 * 1024) });
  assert.equal(response.status, 201);
  assert.deepEqual(counts, [2, 1]);
  assert.equal((await response.json()).length, 3);
  assert.equal(response.headers.get('x-upload-batches'), '2');
});

test('each batch preserves user, domain, credentials, and cancellation signal', async () => {
  const signal = new AbortController().signal;
  let calls = 0;
  const upload = createUploadFetch(async (_, init) => {
    calls += 1;
    assert.equal(init.headers.get('x-user-id'), 'alice');
    assert.equal(init.headers.has('content-type'), false);
    assert.equal(init.headers.has('content-length'), false);
    assert.equal(init.body.get('domain'), 'education');
    assert.equal(init.credentials, 'include');
    assert.equal(init.signal, signal);
    return ok(init.body);
  }, smallLimits);
  const response = await upload('/upload', {
    method: 'POST', body: form(3), signal, credentials: 'include',
    headers: { 'X-User-ID': 'alice', 'Content-Type': 'multipart/form-data; boundary=old', 'Content-Length': '99999' },
  });
  assert.equal(response.status, 201);
  assert.equal(calls, 2);
});

test('an oversized individual file rejects the whole selection before sending', async () => {
  let calls = 0;
  const body = form(1);
  body.append('files', new File([new Uint8Array(10_001)], 'large.pdf'));
  const upload = createUploadFetch(async () => { calls += 1; }, smallLimits);
  const response = await upload('/upload', { method: 'POST', body });
  assert.equal(response.status, 413);
  const error = await response.json();
  assert.equal(calls, 0);
  assert.equal(error.error_code, 'FILE_TOO_LARGE');
  assert.deepEqual(error.uploaded, []);
  assert.deepEqual(error.failed_files, [{ index: 1, filename: 'large.pdf' }]);
  assert.deepEqual(error.not_attempted_files, [{ index: 0, filename: '0.pdf' }]);
});

test('a file exactly at the configured per-file limit is accepted', async () => {
  const upload = createUploadFetch(async (_, init) => ok(init.body), { ...smallLimits, maxBatchBytes: 20_000 });
  assert.equal((await upload('/upload', { method: 'POST', body: form(1, 10_000) })).status, 201);
});

test('confirmed multi-file 413 responses split and retry only rejected batches', async () => {
  const counts = [];
  const sent = [];
  const upload = createUploadFetch(async (_, init) => {
    const files = init.body.getAll('files');
    counts.push(files.length);
    if (files.length > 1) return new Response('<h1>Request too large</h1>', { status: 413 });
    sent.push(files[0].name);
    return ok(init.body);
  }, smallLimits);
  const response = await upload('/upload', { method: 'POST', body: form(4) });
  assert.equal(response.status, 201);
  assert.deepEqual(counts, [2, 1, 1, 2, 1, 1]);
  assert.deepEqual(sent, ['0.pdf', '1.pdf', '2.pdf', '3.pdf']);
});

test('later failure reports completed, failed, and unattempted files without retrying successes', async () => {
  let calls = 0;
  const upload = createUploadFetch(async (_, init) => {
    calls += 1;
    if (calls === 2) return new Response(JSON.stringify({ error_code: 'STORAGE_UNAVAILABLE', message: 'Storage unavailable.' }), { status: 503 });
    return ok(init.body);
  }, smallLimits);
  const response = await upload('/upload', { method: 'POST', body: form(5) });
  assert.equal(response.status, 503);
  const error = await response.json();
  assert.equal(calls, 2);
  assert.equal(error.uploaded.length, 2);
  assert.deepEqual(error.failed_files.map(({ filename }) => filename), ['2.pdf', '3.pdf']);
  assert.deepEqual(error.not_attempted_files.map(({ filename }) => filename), ['4.pdf']);
  assert.equal(error.error_code, 'STORAGE_UNAVAILABLE');
});

test('transport failures and malformed success responses are never retried', async () => {
  for (const malformed of [false, true]) {
    let calls = 0;
    const upload = createUploadFetch(async () => {
      calls += 1;
      if (malformed) return new Response('[]', { status: 201 });
      throw new TypeError('Network connection closed');
    }, smallLimits);
    const response = await upload('/upload', { method: 'POST', body: form(3) });
    assert.equal(response.status, 502);
    assert.equal(calls, 1);
    const error = await response.json();
    assert.equal(error.may_have_completed, true);
    assert.equal(error.not_attempted_files.length, 1);
  }
});

test('single-file gateway 413 becomes a clear JSON error and is not retried', async () => {
  let calls = 0;
  const upload = createUploadFetch(async () => {
    calls += 1;
    return new Response('<html>413 Request Entity Too Large</html>', { status: 413 });
  });
  const response = await upload('/upload', { method: 'POST', body: form(1) });
  assert.equal(response.status, 413);
  assert.equal(calls, 1);
  assert.match((await response.json()).message, /smaller file/);
});

test('shared corpus uploads preserve developer credentials', async () => {
  for (const path of ['/uploadedu', '/uploadmedical']) {
    const upload = createUploadFetch(async (url, init) => {
      assert.equal(url, path);
      assert.equal(init.headers.get('x-developer-key'), 'test-only');
      return ok(init.body);
    }, smallLimits);
    assert.equal((await upload(path, { method: 'POST', headers: { 'X-Developer-Key': 'test-only' }, body: form(3) })).status, 201);
  }
});

test('unrelated requests are passed through unchanged', async () => {
  const init = { method: 'POST', body: JSON.stringify({ message: 'hi' }) };
  const upload = createUploadFetch(async (url, received) => {
    assert.equal(url, '/chat');
    assert.equal(received, init);
    return new Response('event: status\n\n');
  });
  assert.equal(await (await upload('/chat', init)).text(), 'event: status\n\n');
});

test('Swagger interceptor installs the size-aware transport', () => {
  const request = { url: '/upload', method: 'POST', body: form(1) };
  assert.equal(globalThis.RAWBerryUploads.requestInterceptor(request), request);
  assert.equal(typeof request.userFetch, 'function');
});
