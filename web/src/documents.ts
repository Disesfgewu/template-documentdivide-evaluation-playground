/**
 * Document intake and download API client functions.
 *
 * Implements authenticated PDF upload and authorized PDF download matching
 * the contract in services/api/lab_api/documents.py.
 *
 * Security & Reliability Invariants:
 * - Token is held only in memory; never persisted, stored, or logged.
 * - Project and Document IDs are strictly validated as UUIDs before any network call.
 * - Response shapes and hashes are strictly validated against request context; malformed responses fail closed.
 * - Downloads strictly enforce application/pdf media type and %PDF- magic signature before saving.
 * - SPEC 16.4 error structures are parsed safely.
 * - Malformed percent-encoded Content-Disposition headers never throw.
 * - Object URLs generated for PDF downloads are strictly managed and revoked immediately.
 */

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
};

export async function uploadDocument({
  projectId,
  token,
  file,
  logicalId,
  idempotencyKey,
  fetchFn = fetch,
  baseUrl = "",
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

  let res: Response;
  try {
    res = await fetchFn(endpoint, {
      method: "POST",
      headers,
      body: formData,
    });
  } catch (err: unknown) {
    const errMessage = err instanceof Error ? err.message : String(err);
    return {
      ok: false,
      error: {
        message: `連線失敗或逾時（${errMessage}）`,
        code: "network_error",
      },
    };
  }

  let body: unknown;
  try {
    body = await res.json();
  } catch {
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

  const endpoint = `${baseUrl}/api/v1/documents/${encodeURIComponent(cleanDocId)}/download`;

  let res: Response;
  try {
    res = await fetchFn(endpoint, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${cleanToken}`,
        Accept: "application/pdf",
      },
    });
  } catch (err: unknown) {
    const errMessage = err instanceof Error ? err.message : String(err);
    return {
      ok: false,
      error: {
        message: `連線失敗或逾時（${errMessage}）`,
        code: "network_error",
      },
    };
  }

  if (!res.ok) {
    let errBody: unknown;
    try {
      errBody = await res.json();
    } catch {
      errBody = null;
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
    return {
      ok: false,
      error: {
        message: `伺服器回應之內容類型非 PDF（收到 ${mediaType || "未知類型"}）`,
        status: res.status,
        code: "invalid_media_type",
      },
    };
  }

  // 2. Read Blob
  let blob: Blob;
  try {
    blob = await res.blob();
  } catch (err: unknown) {
    const errMessage = err instanceof Error ? err.message : String(err);
    return {
      ok: false,
      error: {
        message: `讀取檔案內容失敗（${errMessage}）`,
        status: 500,
        code: "read_error",
      },
    };
  }

  if (blob.size < 5) {
    return {
      ok: false,
      error: {
        message: "下載之檔案過小或為空，非有效 PDF",
        status: 500,
        code: "empty_file",
      },
    };
  }

  // 3. Strict PDF signature verification (%PDF- allowing leading whitespace within first 1024 bytes)
  try {
    const headerBuffer = await blob.slice(0, 1029).arrayBuffer();
    const headerBytes = new Uint8Array(headerBuffer);
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
  } catch (err: unknown) {
    const errMessage = err instanceof Error ? err.message : String(err);
    return {
      ok: false,
      error: {
        message: `驗證 PDF 簽章失敗（${errMessage}）`,
        status: 500,
        code: "signature_check_error",
      },
    };
  }

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
      sizeBytes: blob.size,
    },
  };
}
