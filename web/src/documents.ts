/**
 * Document intake and download API client functions.
 *
 * Implements authenticated PDF upload and authorized PDF download matching
 * the contract in services/api/lab_api/documents.py.
 *
 * Security & Reliability Invariants:
 * - Token is held only in memory; never persisted, stored, or logged. Secrets are never shown.
 * - Project and Document IDs are strictly validated as UUIDs before any network call.
 * - Response shapes and hashes are strictly validated against request context; malformed responses fail closed.
 * - Downloads strictly enforce application/pdf media type and %PDF- magic signature before saving.
 * - Streaming download and error reads are bounded with explicit byte limits and finite deadlines.
 * - SPEC 512 MiB maximum PDF capacity (SPEC 16.6) is preserved as the default ceiling.
 * - SPEC 16.4 error structures are parsed safely within bounded memory (default 64 KiB).
 * - Malformed percent-encoded Content-Disposition headers never throw.
 * - Object URLs generated for PDF downloads are strictly managed and revoked immediately.
 */

/**
 * Default maximum PDF size: 512 MiB (SPEC 16.6).
 * Preserved to match backend contract in services/api/lab_api/pdf_body.py.
 */
export const DEFAULT_MAX_PDF_BYTES = 512 * 1024 * 1024;

/**
 * Default maximum error payload size: 64 KiB (SPEC 16.4 error structures are small).
 */
export const DEFAULT_MAX_ERROR_BYTES = 64 * 1024;

/**
 * Default finite timeout for uploads: 120 seconds.
 */
export const DEFAULT_UPLOAD_TIMEOUT_MS = 120_000;

/**
 * Default finite timeout for downloads: 120 seconds.
 */
export const DEFAULT_DOWNLOAD_TIMEOUT_MS = 120_000;

export type UploadSuccess = {
  id: string;
  documentVersionId: string;
  projectId: string;
  resourceId: string;
  artifactId: string;
  logicalId: string;
  revision: number;
  sha256: string;
  sizeBytes: number;
  pageCount: number;
  flagged?: boolean;
  warnings?: string[];
  replayed?: boolean;
};

export type ApiErrorDetail = {
  message: string;
  code?: string;
  status?: number;
  isPermissionDenied?: boolean;
  isUnauthorized?: boolean;
  details?: Record<string, unknown>;
};

export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: ApiErrorDetail };

const UUID_REGEX =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const SHA256_REGEX = /^[0-9a-f]{64}$/i;

/**
 * Sanitize error message to prevent inadvertent secret/token leakage.
 */
export function sanitizeErrorMessage(msg: string, token?: string): string {
  if (token && token.length > 5 && msg.includes(token)) {
    return msg.replaceAll(token, "[REDACTED]");
  }
  return msg;
}

/**
 * Verifies that bytes represent a valid PDF file.
 * Matches backend importer (services/parser/lab_parser/importer.py):
 * Allows leading whitespace (space, \t, \r, \n, \f, \v, \0) within the first
 * 1024 bytes, followed immediately by %PDF- (5 bytes).
 */
export function hasValidPdfSignature(bytes: Uint8Array): boolean {
  let idx = 0;
  // Whitespace characters: space (32), \t (9), \n (10), \v (11), \f (12), \r (13), \0 (0)
  while (idx < bytes.length && idx < 1024) {
    const b = bytes[idx];
    if (b === 32 || b === 9 || b === 10 || b === 11 || b === 12 || b === 13 || b === 0) {
      idx++;
    } else {
      break;
    }
  }
  if (idx >= 1024) return false;
  if (bytes.length < idx + 5) return false;
  return (
    bytes[idx] === 0x25 && // %
    bytes[idx + 1] === 0x50 && // P
    bytes[idx + 2] === 0x44 && // D
    bytes[idx + 3] === 0x46 && // F
    bytes[idx + 4] === 0x2d // -
  );
}

export function validateUuid(val: string): boolean {
  return UUID_REGEX.test(val.trim());
}

export function validateSha256(val: string): boolean {
  return SHA256_REGEX.test(val.trim());
}

export const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

export function parseErrorResponse(status: number, body: unknown): ApiErrorDetail {
  let message = `請求失敗 (HTTP ${status})`;
  let code: string | undefined;
  let details: Record<string, unknown> | undefined;

  if (isRecord(body)) {
    if (isRecord(body.error)) {
      if (typeof body.error.message === "string" && body.error.message.trim()) {
        message = body.error.message.trim();
      }
      if (typeof body.error.code === "string" && body.error.code.trim()) {
        code = body.error.code.trim();
      }
      if (isRecord(body.error.details)) {
        details = body.error.details;
      }
    } else if (typeof body.detail === "string" && body.detail.trim()) {
      message = body.detail.trim();
    } else if (typeof body.message === "string" && body.message.trim()) {
      message = body.message.trim();
    }
  }

  const isPermissionDenied = status === 403 || code === "permission_denied";
  const isUnauthorized = status === 401 || code === "unauthorized";

  return {
    message,
    code,
    status,
    isPermissionDenied,
    isUnauthorized,
    details,
  };
}

/**
 * Reads response JSON with a strict byte limit and abort signal.
 * Prevents unbounded memory buffering on large/malformed error responses.
 */
export async function readBoundedJson(
  res: Response,
  maxBytes: number = DEFAULT_MAX_ERROR_BYTES,
  signal?: AbortSignal,
): Promise<unknown> {
  const abortPromise = signal
    ? new Promise<never>((_, reject) => {
        if (signal.aborted) {
          reject(signal.reason ?? new DOMException("The operation was aborted.", "AbortError"));
          return;
        }
        signal.addEventListener(
          "abort",
          () => reject(signal.reason ?? new DOMException("The operation was aborted.", "AbortError")),
          { once: true },
        );
      })
    : null;

  if (res.body && typeof res.body.getReader === "function") {
    const reader = res.body.getReader();
    const chunks: Uint8Array[] = [];
    let totalBytes = 0;
    try {
      while (true) {
        const readPromise = reader.read();
        const { done, value } = abortPromise
          ? await Promise.race([readPromise, abortPromise])
          : await readPromise;
        if (done) break;
        if (value && value.byteLength > 0) {
          totalBytes += value.byteLength;
          if (totalBytes > maxBytes) {
            try {
              await reader.cancel("payload_too_large");
            } catch {
              // ignore
            }
            break;
          }
          chunks.push(value);
        }
      }
    } catch (err: unknown) {
      try {
        await reader.cancel();
      } catch {
        // ignore
      }
      throw err;
    }

    const merged = new Uint8Array(Math.min(totalBytes, maxBytes));
    let offset = 0;
    for (const chunk of chunks) {
      const take = Math.min(chunk.byteLength, merged.byteLength - offset);
      merged.set(chunk.subarray(0, take), offset);
      offset += take;
      if (offset >= merged.byteLength) break;
    }
    const text = new TextDecoder("utf-8").decode(merged);
    try {
      return JSON.parse(text);
    } catch {
      return text ? { message: text.slice(0, 500) } : null;
    }
  }

  // Fallback for mocks without streaming res.body
  if (typeof res.text === "function") {
    const textPromise = res.text();
    const text = abortPromise ? await Promise.race([textPromise, abortPromise]) : await textPromise;
    const boundedText = text.slice(0, maxBytes);
    try {
      return JSON.parse(boundedText);
    } catch {
      return boundedText ? { message: boundedText.slice(0, 500) } : null;
    }
  } else if (typeof res.json === "function") {
    const jsonPromise = res.json();
    return abortPromise ? await Promise.race([jsonPromise, abortPromise]) : await jsonPromise;
  }

  return null;
}

/**
 * Reads a ReadableStream with an upper bound on bytes, abort signal, and finite deadline.
 * If the byte count exceeds maxBytes, cancels the stream immediately and throws payload_too_large error.
 */
export async function readBoundedStream(
  bodyStream: ReadableStream<Uint8Array> | null | undefined,
  fallbackResponse: Response,
  maxBytes: number,
  signal: AbortSignal,
): Promise<{ chunks: Uint8Array[]; totalBytes: number }> {
  const abortPromise = new Promise<never>((_, reject) => {
    if (signal.aborted) {
      reject(signal.reason ?? new DOMException("The operation was aborted.", "AbortError"));
      return;
    }
    const onAbort = () => {
      reject(signal.reason ?? new DOMException("The operation was aborted.", "AbortError"));
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });

  if (bodyStream && typeof bodyStream.getReader === "function") {
    const reader = bodyStream.getReader();
    const chunks: Uint8Array[] = [];
    let totalBytes = 0;
    try {
      while (true) {
        const { done, value } = await Promise.race([reader.read(), abortPromise]);
        if (done) break;
        if (value && value.byteLength > 0) {
          totalBytes += value.byteLength;
          if (totalBytes > maxBytes) {
            try {
              await reader.cancel("payload_too_large");
            } catch {
              // ignore
            }
            const err = new Error(`下載檔案大小超過上限（已讀取超過 ${maxBytes} 位元組）`);
            (err as any).code = "payload_too_large";
            (err as any).status = 413;
            throw err;
          }
          chunks.push(value);
        }
      }
      return { chunks, totalBytes };
    } catch (err: unknown) {
      try {
        await reader.cancel();
      } catch {
        // ignore
      }
      throw err;
    }
  }

  // Fallback for mocks without ReadableStream getReader
  if (typeof fallbackResponse.blob === "function") {
    const blob = await Promise.race([fallbackResponse.blob(), abortPromise]);
    if (blob.size > maxBytes) {
      const err = new Error(`下載檔案大小超過上限（${blob.size} > ${maxBytes} 位元組）`);
      (err as any).code = "payload_too_large";
      (err as any).status = 413;
      throw err;
    }
    const buf = await blob.arrayBuffer();
    const u8 = new Uint8Array(buf);
    return { chunks: [u8], totalBytes: u8.byteLength };
  } else if (typeof fallbackResponse.arrayBuffer === "function") {
    const buf = await Promise.race([fallbackResponse.arrayBuffer(), abortPromise]);
    if (buf.byteLength > maxBytes) {
      const err = new Error(`下載檔案大小超過上限（${buf.byteLength} > ${maxBytes} 位元組）`);
      (err as any).code = "payload_too_large";
      (err as any).status = 413;
      throw err;
    }
    const u8 = new Uint8Array(buf);
    return { chunks: [u8], totalBytes: u8.byteLength };
  }

  return { chunks: [], totalBytes: 0 };
}

export function parseUploadResponse(
  body: unknown,
  expectedProjectId?: string,
): UploadSuccess | null {
  if (!isRecord(body)) return null;

  const id = typeof body.id === "string" ? body.id.trim() : null;
  const docVerId =
    typeof body.document_version_id === "string"
      ? body.document_version_id.trim()
      : null;
  const projectId =
    typeof body.project_id === "string" ? body.project_id.trim() : null;
  const resourceId =
    typeof body.resource_id === "string" ? body.resource_id.trim() : null;
  const artifactId =
    typeof body.artifact_id === "string" ? body.artifact_id.trim() : null;
  const logicalId =
    typeof body.logical_id === "string" ? body.logical_id.trim() : null;
  const revision = typeof body.revision === "number" ? body.revision : null;
  const sha256 = typeof body.sha256 === "string" ? body.sha256.trim() : null;
  const sizeBytes = typeof body.size_bytes === "number" ? body.size_bytes : null;
  const pageCount = typeof body.page_count === "number" ? body.page_count : null;

  // 1. Mandatory presence and UUID checks
  if (
    !id ||
    !validateUuid(id) ||
    !projectId ||
    !validateUuid(projectId) ||
    !resourceId ||
    !validateUuid(resourceId) ||
    !artifactId ||
    !validateUuid(artifactId)
  ) {
    return null;
  }

  // 2. Consistency checks: id and document_version_id must match if both present
  const documentVersionId = docVerId || id;
  if (!validateUuid(documentVersionId) || (docVerId && docVerId !== id)) {
    return null;
  }

  // 3. Project ID consistency with request
  if (
    expectedProjectId &&
    (!validateUuid(expectedProjectId) ||
      projectId.toLowerCase() !== expectedProjectId.trim().toLowerCase())
  ) {
    return null;
  }

  // The API counts Unicode code points, not JavaScript UTF-16 code units.
  if (!logicalId || Array.from(logicalId).length > 128) {
    return null;
  }

  // 5. SHA-256 hash check
  if (!sha256 || !validateSha256(sha256)) {
    return null;
  }

  // 6. Safe positive integer checks
  if (
    revision === null ||
    !Number.isSafeInteger(revision) ||
    revision < 1 ||
    sizeBytes === null ||
    !Number.isSafeInteger(sizeBytes) ||
    sizeBytes <= 0 ||
    pageCount === null ||
    !Number.isSafeInteger(pageCount) ||
    pageCount <= 0
  ) {
    return null;
  }

  // 7. Optional flagged/warnings/replayed checks
  if (body.flagged !== undefined && typeof body.flagged !== "boolean") {
    return null;
  }
  if (body.replayed !== undefined && typeof body.replayed !== "boolean") {
    return null;
  }

  const warnings: string[] = [];
  if (body.warnings !== undefined) {
    if (!Array.isArray(body.warnings)) return null;
    for (const w of body.warnings) {
      if (typeof w !== "string") return null;
      warnings.push(w);
    }
  }

  return {
    id,
    documentVersionId,
    projectId,
    resourceId,
    artifactId,
    logicalId,
    revision,
    sha256: sha256.toLowerCase(),
    sizeBytes,
    pageCount,
    flagged: typeof body.flagged === "boolean" ? body.flagged : undefined,
    warnings: warnings.length > 0 ? warnings : undefined,
    replayed: typeof body.replayed === "boolean" ? body.replayed : undefined,
  };
}

export type UploadDocumentOptions = {
  projectId: string;
  token: string;
  file: File | Blob;
  logicalId?: string;
  idempotencyKey?: string;
  fetchFn?: typeof fetch;
  baseUrl?: string;
  signal?: AbortSignal;
  timeoutMs?: number;
  maxBytes?: number;
  maxErrorBytes?: number;
};

export async function uploadDocument({
  projectId,
  token,
  file,
  logicalId,
  idempotencyKey,
  fetchFn = fetch,
  baseUrl = "",
  signal,
  timeoutMs = DEFAULT_UPLOAD_TIMEOUT_MS,
  maxBytes = DEFAULT_MAX_PDF_BYTES,
  maxErrorBytes = DEFAULT_MAX_ERROR_BYTES,
}: UploadDocumentOptions): Promise<ApiResult<UploadSuccess>> {
  const cleanToken = token.trim();
  if (!cleanToken) {
    return {
      ok: false,
      error: {
        message: "認證失敗：未提供 Token",
        status: 401,
        code: "unauthorized",
        isUnauthorized: true,
      },
    };
  }

  const cleanProjId = projectId.trim();
  if (!validateUuid(cleanProjId)) {
    return {
      ok: false,
      error: {
        message: "Project ID 格式錯誤（須為 UUID）",
        status: 400,
        code: "bad_request",
      },
    };
  }

  if (!file || (typeof file.size === "number" && file.size === 0)) {
    return {
      ok: false,
      error: {
        message: "請選擇非空白的 PDF 檔案",
        status: 422,
        code: "empty_file",
      },
    };
  }

  if (typeof file.size === "number" && file.size > maxBytes) {
    return {
      ok: false,
      error: {
        message: `檔案大小超過上限（${file.size} > ${maxBytes} 位元組）`,
        status: 413,
        code: "payload_too_large",
      },
    };
  }

  if (signal?.aborted) {
    return {
      ok: false,
      error: {
        message: "上傳已由使用者取消",
        code: "aborted",
      },
    };
  }

  const formData = new FormData();
  formData.append("file", file);

  if (logicalId && logicalId.trim()) {
    formData.append("logical_id", logicalId.trim());
  }

  const headers: Record<string, string> = {
    Authorization: `Bearer ${cleanToken}`,
  };

  if (idempotencyKey && idempotencyKey.trim()) {
    headers["Idempotency-Key"] = idempotencyKey.trim();
  }

  const endpoint = `${baseUrl}/api/v1/projects/${encodeURIComponent(cleanProjId)}/documents`;

  const controller = new AbortController();
  let timeoutId: ReturnType<typeof setTimeout> | undefined;

  if (signal) {
    signal.addEventListener(
      "abort",
      () => {
        controller.abort(signal.reason ?? new DOMException("The operation was aborted.", "AbortError"));
      },
      { once: true },
    );
  }

  if (timeoutMs > 0 && timeoutMs < Infinity) {
    timeoutId = setTimeout(() => {
      controller.abort(new DOMException("The operation timed out.", "TimeoutError"));
    }, timeoutMs);
  }

  let res: Response;
  try {
    res = await fetchFn(endpoint, {
      method: "POST",
      headers,
      body: formData,
      signal: controller.signal,
    });
  } catch (err: unknown) {
    const errMessage = err instanceof Error ? err.message : String(err);
    if (signal?.aborted) {
      return {
        ok: false,
        error: {
          message: "上傳已由使用者取消",
          code: "aborted",
        },
      };
    }
    if (
      controller.signal.aborted &&
      (controller.signal.reason?.name === "TimeoutError" ||
        (err instanceof DOMException && err.name === "TimeoutError") ||
        errMessage.includes("timed out"))
    ) {
      return {
        ok: false,
        error: {
          message: "請求逾時（操作超過設定時限）",
          code: "timeout",
          status: 408,
        },
      };
    }
    return {
      ok: false,
      error: {
        message: sanitizeErrorMessage(`連線失敗或逾時（${errMessage}）`, cleanToken),
        code: "network_error",
      },
    };
  } finally {
    if (timeoutId !== undefined) {
      clearTimeout(timeoutId);
    }
  }

  let body: unknown;
  try {
    body = await readBoundedJson(res, maxErrorBytes, controller.signal);
  } catch (err: unknown) {
    if (signal?.aborted) {
      return {
        ok: false,
        error: {
          message: "上傳已由使用者取消",
          code: "aborted",
        },
      };
    }
    if (
      controller.signal.aborted &&
      (controller.signal.reason?.name === "TimeoutError" ||
        (err instanceof DOMException && err.name === "TimeoutError"))
    ) {
      return {
        ok: false,
        error: {
          message: "請求逾時（操作超過設定時限）",
          code: "timeout",
          status: 408,
        },
      };
    }
    body = null;
  }

  if (!res.ok) {
    return {
      ok: false,
      error: parseErrorResponse(res.status, body),
    };
  }

  const parsed = parseUploadResponse(body, cleanProjId);
  if (!parsed) {
    return {
      ok: false,
      error: {
        message: `伺服器回應資料驗證失敗（欄位格式不符或識別碼不一致）`,
        status: res.status,
        code: "invalid_response_shape",
      },
    };
  }

  return { ok: true, data: parsed };
}

export type DownloadDocumentOptions = {
  documentId: string;
  token: string;
  fetchFn?: typeof fetch;
  baseUrl?: string;
  triggerSave?: (blob: Blob, filename: string) => void;
  signal?: AbortSignal;
  timeoutMs?: number;
  maxBytes?: number;
  maxErrorBytes?: number;
};

export function sanitizeFilename(rawFilename: string, fallback: string): string {
  const base = rawFilename.replace(/^.*[\\/]/, "").trim();
  const unquoted = base.replace(/^["']|["']$/g, "").trim();
  if (!unquoted || unquoted === "." || unquoted === "..") {
    return fallback;
  }
  const sanitized = unquoted.replace(/[<>:"/\\|?*\x00-\x1F]/g, "_");
  return sanitized.endsWith(".pdf") ? sanitized : `${sanitized}.pdf`;
}

export function extractFilenameFromHeader(
  header: string | null,
  fallback: string,
): string {
  if (!header) return fallback;
  try {
    // Check RFC 5987 / 8187 filename* first
    const extMatch = header.match(
      /filename\*\s*=\s*(?:[A-Za-z0-9_-]+)?'[^']*'([^;\n]+)/i,
    );
    let rawName: string | null = null;
    if (extMatch && extMatch[1]) {
      rawName = extMatch[1].trim().replace(/^["']|["']$/g, "");
    } else {
      const stdMatch = header.match(
        /filename\s*=\s*(?:(["'])(.*?)\1|([^;\n]+))/i,
      );
      if (stdMatch) {
        rawName = (stdMatch[2] ?? stdMatch[3] ?? "").trim();
      }
    }

    if (!rawName) return fallback;

    try {
      rawName = decodeURIComponent(rawName);
    } catch {
      // Malformed percent-encoding: fall back
      return fallback;
    }
    return sanitizeFilename(rawName, fallback);
  } catch {
    return fallback;
  }
}

export async function downloadDocument({
  documentId,
  token,
  fetchFn = fetch,
  baseUrl = "",
  triggerSave,
  signal,
  timeoutMs = DEFAULT_DOWNLOAD_TIMEOUT_MS,
  maxBytes = DEFAULT_MAX_PDF_BYTES,
  maxErrorBytes = DEFAULT_MAX_ERROR_BYTES,
}: DownloadDocumentOptions): Promise<
  ApiResult<{ blob: Blob; filename: string; sizeBytes: number }>
> {
  const cleanToken = token.trim();
  if (!cleanToken) {
    return {
      ok: false,
      error: {
        message: "認證失敗：未提供 Token",
        status: 401,
        code: "unauthorized",
        isUnauthorized: true,
      },
    };
  }

  const cleanDocId = documentId.trim();
  if (!validateUuid(cleanDocId)) {
    return {
      ok: false,
      error: {
        message: "Document ID 格式錯誤（須為 UUID）",
        status: 400,
        code: "bad_request",
      },
    };
  }

  if (signal?.aborted) {
    return {
      ok: false,
      error: {
        message: "下載已由使用者取消",
        code: "aborted",
      },
    };
  }

  const endpoint = `${baseUrl}/api/v1/documents/${encodeURIComponent(cleanDocId)}/download`;

  const controller = new AbortController();
  let timeoutId: ReturnType<typeof setTimeout> | undefined;

  if (signal) {
    signal.addEventListener(
      "abort",
      () => {
        controller.abort(signal.reason ?? new DOMException("The operation was aborted.", "AbortError"));
      },
      { once: true },
    );
  }

  if (timeoutMs > 0 && timeoutMs < Infinity) {
    timeoutId = setTimeout(() => {
      controller.abort(new DOMException("The operation timed out.", "TimeoutError"));
    }, timeoutMs);
  }

  let res: Response;
  try {
    res = await fetchFn(endpoint, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${cleanToken}`,
        Accept: "application/pdf",
      },
      signal: controller.signal,
    });
  } catch (err: unknown) {
    const errMessage = err instanceof Error ? err.message : String(err);
    if (signal?.aborted) {
      return {
        ok: false,
        error: {
          message: "下載已由使用者取消",
          code: "aborted",
        },
      };
    }
    if (
      controller.signal.aborted &&
      (controller.signal.reason?.name === "TimeoutError" ||
        (err instanceof DOMException && err.name === "TimeoutError") ||
        errMessage.includes("timed out"))
    ) {
      return {
        ok: false,
        error: {
          message: "請求逾時（操作超過設定時限）",
          code: "timeout",
          status: 408,
        },
      };
    }
    return {
      ok: false,
      error: {
        message: sanitizeErrorMessage(`連線失敗或逾時（${errMessage}）`, cleanToken),
        code: "network_error",
      },
    };
  }

  if (!res.ok) {
    let errBody: unknown;
    try {
      errBody = await readBoundedJson(res, maxErrorBytes, controller.signal);
    } catch {
      errBody = null;
    } finally {
      if (timeoutId !== undefined) {
        clearTimeout(timeoutId);
      }
    }
    return {
      ok: false,
      error: parseErrorResponse(res.status, errBody),
    };
  }

  // 1. Strict Content-Type enforcement (exact normalized media type token, not substring/includes)
  const rawContentType = res.headers?.get("Content-Type") ?? "";
  const mediaType = rawContentType.split(";")[0].trim().toLowerCase();
  if (mediaType !== "application/pdf") {
    if (timeoutId !== undefined) {
      clearTimeout(timeoutId);
    }
    if (res.body && typeof res.body.cancel === "function") {
      try {
        await res.body.cancel();
      } catch {
        // ignore
      }
    }
    return {
      ok: false,
      error: {
        message: `伺服器回應之內容類型非 PDF（收到 ${mediaType || "未知類型"}）`,
        status: res.status,
        code: "invalid_media_type",
      },
    };
  }

  // 2. Strict Content-Length header verification
  const rawContentLength = res.headers?.get("Content-Length");
  let expectedLength: number | null = null;
  if (rawContentLength !== null && rawContentLength !== undefined && rawContentLength.trim() !== "") {
    const parsedCl = Number(rawContentLength.trim());
    if (!Number.isSafeInteger(parsedCl) || parsedCl < 0) {
      if (timeoutId !== undefined) {
        clearTimeout(timeoutId);
      }
      if (res.body && typeof res.body.cancel === "function") {
        try {
          await res.body.cancel();
        } catch {
          // ignore
        }
      }
      return {
        ok: false,
        error: {
          message: "伺服器回應之 Content-Length 標頭格式不正確",
          status: 400,
          code: "invalid_content_length",
        },
      };
    }
    if (parsedCl > maxBytes) {
      if (timeoutId !== undefined) {
        clearTimeout(timeoutId);
      }
      if (res.body && typeof res.body.cancel === "function") {
        try {
          await res.body.cancel();
        } catch {
          // ignore
        }
      }
      return {
        ok: false,
        error: {
          message: `檔案大小超過上限（Content-Length: ${parsedCl} > ${maxBytes} 位元組）`,
          status: 413,
          code: "payload_too_large",
        },
      };
    }
    expectedLength = parsedCl;
  }

  // 3. Read bounded stream with deadline and actual byte count enforcement
  let streamResult: { chunks: Uint8Array[]; totalBytes: number };
  try {
    streamResult = await readBoundedStream(res.body, res, maxBytes, controller.signal);
  } catch (err: unknown) {
    const errMessage = err instanceof Error ? err.message : String(err);
    if (signal?.aborted) {
      return {
        ok: false,
        error: {
          message: "下載已由使用者取消",
          code: "aborted",
        },
      };
    }
    if (
      controller.signal.aborted &&
      (controller.signal.reason?.name === "TimeoutError" ||
        (err instanceof DOMException && err.name === "TimeoutError") ||
        errMessage.includes("timed out"))
    ) {
      return {
        ok: false,
        error: {
          message: "請求逾時（操作超過設定時限）",
          code: "timeout",
          status: 408,
        },
      };
    }
    if ((err as any)?.code === "payload_too_large") {
      return {
        ok: false,
        error: {
          message: errMessage,
          status: 413,
          code: "payload_too_large",
        },
      };
    }
    return {
      ok: false,
      error: {
        message: sanitizeErrorMessage(`讀取檔案內容失敗（${errMessage}）`, cleanToken),
        status: 500,
        code: "read_error",
      },
    };
  } finally {
    if (timeoutId !== undefined) {
      clearTimeout(timeoutId);
    }
  }

  const { chunks, totalBytes } = streamResult;

  if (expectedLength !== null && totalBytes !== expectedLength) {
    return {
      ok: false,
      error: {
        message: `下載檔案大小與 Content-Length 標頭不符（預期 ${expectedLength}，實際 ${totalBytes} 位元組）`,
        status: 500,
        code: "read_error",
      },
    };
  }

  if (totalBytes < 5) {
    return {
      ok: false,
      error: {
        message: "下載之檔案過小或為空，非有效 PDF",
        status: 500,
        code: "empty_file",
      },
    };
  }

  // 4. Strict PDF signature verification (%PDF- allowing leading whitespace within first 1024 bytes)
  let headerBytes: Uint8Array;
  if (chunks.length === 1) {
    headerBytes = chunks[0].subarray(0, 1029);
  } else {
    const needed = Math.min(1029, totalBytes);
    headerBytes = new Uint8Array(needed);
    let offset = 0;
    for (const chunk of chunks) {
      const take = Math.min(chunk.byteLength, needed - offset);
      headerBytes.set(chunk.subarray(0, take), offset);
      offset += take;
      if (offset >= needed) break;
    }
  }

  if (!hasValidPdfSignature(headerBytes)) {
    return {
      ok: false,
      error: {
        message: "檔案非有效 PDF 格式（未找到有效的 %PDF- 簽章標頭）",
        status: 500,
        code: "invalid_pdf_signature",
      },
    };
  }

  const blob = new Blob(chunks as BlobPart[], { type: "application/pdf" });

  const fallbackFilename = `${cleanDocId}.pdf`;
  const filename = extractFilenameFromHeader(
    res.headers?.get("Content-Disposition") ?? null,
    fallbackFilename,
  );

  if (triggerSave) {
    triggerSave(blob, filename);
  } else if (
    typeof URL !== "undefined" &&
    typeof URL.createObjectURL === "function" &&
    typeof document !== "undefined"
  ) {
    const objectUrl = URL.createObjectURL(blob);
    try {
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } finally {
      URL.revokeObjectURL(objectUrl);
    }
  }

  return {
    ok: true,
    data: {
      blob,
      filename,
      sizeBytes: totalBytes,
    },
  };
}
