/* Reusable Fetch-compatible bulk uploader. Used by /docs before network send. */
(function (root) {
  'use strict';
  const MiB = 1024 * 1024;

  function jsonResponse(body, status = 201, batches = 0) {
    return new Response(JSON.stringify(body), {
      status, headers: { 'Content-Type': 'application/json', 'X-Upload-Batches': String(batches) },
    });
  }

  function createUploadFetch(fetchImpl = root.fetch.bind(root), options = {}) {
    const maxBatchBytes = options.maxBatchBytes ?? 30 * MiB;
    const maxFileBytes = options.maxFileBytes ?? 25 * MiB;
    const encoder = new TextEncoder();

    return async function uploadFetch(url, init = {}) {
      const base = /^https?:/.test(root.location?.href || '') ? root.location.href : 'http://localhost/';
      const path = new URL(url, base).pathname;
      if ((init.method || 'GET').toUpperCase() !== 'POST'
          || !/\/(upload|uploadedu|uploadmedical)\/?$/.test(path)
          || !(init.body instanceof FormData)) {
        return fetchImpl(url, init);
      }

      const form = init.body;
      const files = form.getAll('files');
      if (!files.length || files.some((file) => !(file instanceof Blob))) {
        return fetchImpl(url, init); // Preserve the API's validation of malformed forms.
      }
      const uploaded = [];
      const completed = new Set();
      let requests = 0;
      const names = (indices) => indices.map((index) => ({ index, filename: files[index].name || 'blob' }));
      const failure = (status, code, message, failed, uncertain = false) => jsonResponse({
        success: false, error_code: code, message,
        uploaded, failed_files: names(failed),
        not_attempted_files: names(files.map((_, index) => index)
          .filter((index) => !completed.has(index) && !failed.includes(index))),
        may_have_completed: uncertain,
      }, status, requests);

      // Validate every file before sending any part of the selection.
      const oversized = files.map((file, index) => file.size > maxFileBytes ? index : -1)
        .filter((index) => index !== -1);
      if (oversized.length) {
        return failure(413, 'FILE_TOO_LARGE', 'An individual file exceeds the 25 MiB limit. No files were sent.', oversized);
      }

      // Reserve multipart framing space, including repeated domain/other fields.
      // The default leaves another 2 MiB below Cloud Run's 32 MiB ceiling.
      const extras = [...form.entries()].filter(([key]) => key !== 'files');
      const baseSize = 2048 + extras.reduce((sum, [key, value]) => sum + 1024
        + encoder.encode(key).length + (value instanceof Blob ? value.size : encoder.encode(value).length), 0);
      const cost = (file) => file.size + 2048 + encoder.encode(file.name || 'blob').length
        + encoder.encode(file.type || '').length;
      const batches = [];
      let batch = [];
      let size = baseSize;
      for (let index = 0; index < files.length; index += 1) {
        if (baseSize + cost(files[index]) > maxBatchBytes) {
          return failure(413, 'REQUEST_TOO_LARGE', 'A file and its form fields exceed the safe request size. No files were sent.', [index]);
        }
        if (batch.length && (size + cost(files[index]) > maxBatchBytes || batch.length >= 50)) {
          batches.push(batch);
          batch = [];
          size = baseSize;
        }
        batch.push(index);
        size += cost(files[index]);
      }
      if (batch.length) batches.push(batch);

      async function send(indices) {
        const body = new FormData();
        for (const [key, value] of extras) body.append(key, value);
        for (const index of indices) body.append('files', files[index], files[index].name || 'blob');
        const headers = new Headers(init.headers);
        headers.delete('content-type'); // Fetch must generate each batch's multipart boundary.
        headers.delete('content-length');
        let response;
        requests += 1;
        try {
          response = await fetchImpl(url, { ...init, headers, body });
        } catch (error) {
          // A dropped connection can happen after commit. Never blindly retry.
          throw { status: 502, code: 'UPLOAD_INTERRUPTED', indices, uncertain: true,
            message: 'Upload interrupted. Check the document list before retrying; the current batch may have completed.' };
        }
        // A definite 413 means the request was rejected. Smaller batches are safe
        // to retry; no other status or transport failure is automatically retried.
        if (response.status === 413 && indices.length > 1) {
          try { await response.body?.cancel(); } catch (_) { /* Nothing else to read. */ }
          const middle = Math.ceil(indices.length / 2);
          await send(indices.slice(0, middle));
          await send(indices.slice(middle));
          return;
        }
        let data;
        try { data = await response.json(); } catch (_) { data = null; }
        if (!response.ok) {
          throw { status: response.status, code: data?.error_code || 'UPLOAD_FAILED', indices,
            message: data?.message || (response.status === 413
              ? 'The server rejected this file as too large. Upload a smaller file.'
              : `Upload failed with HTTP ${response.status}. Remaining files were not sent.`) };
        }
        if (!Array.isArray(data) || data.length !== indices.length
            || data.some((item) => item.success !== true || !item.document_id)) {
          throw { status: 502, code: 'INVALID_UPLOAD_RESPONSE', indices, uncertain: true,
            message: 'The server did not confirm every file. Check the document list before retrying.' };
        }
        uploaded.push(...data);
        indices.forEach((index) => completed.add(index));
      }

      try {
        for (const indices of batches) await send(indices);
      } catch (error) {
        if (!error.indices) {
          return failure(502, 'UPLOAD_INTERRUPTED',
            'Could not confirm the upload. Check the document list before retrying.',
            files.map((_, index) => index).filter((index) => !completed.has(index)), true);
        }
        return failure(error.status, error.code, error.message, error.indices, error.uncertain);
      }
      return jsonResponse(uploaded, 201, requests);
    };
  }

  const uploadFetch = createUploadFetch();
  root.RAWBerryUploads = {
    createUploadFetch,
    // Swagger serializes multipart data before invoking this interceptor.
    requestInterceptor(request) {
      request.userFetch = uploadFetch;
      return request;
    },
  };
}(globalThis));
