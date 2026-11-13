/**
 * Workflow API Client functions for end-to-end PDF evaluation pipeline.
 *
 * Implements endpoints strictly following deploy/demo_run.py:
 * 1. Upload PDF: POST /api/v1/documents?project_id=...&logical_id=... (Content-Type: application/pdf)
 * 2. Parse: POST /api/v1/documents/{document_id}/parse -> poll GET /api/v1/jobs/{job_id}
 * 3. Benchmark + Plan: POST /api/v1/benchmarks -> POST /api/v1/benchmarks/{logical_id}/revisions/{revision}/plan
 * 4. Generate: POST /api/v1/benchmarks/{logical_id}/revisions/{revision}/generate -> poll GET .../generate
 * 5. Validate: POST /api/v1/benchmarks/{logical_id}/revisions/{revision}/validate -> poll GET .../generate
 * 6. Freeze: POST /api/v1/benchmarks/{logical_id}/revisions/{revision}/freeze
 * 7. Run Methods:
 *    - Lexical Baseline: POST /api/v1/benchmarks/{logical_id}/revisions/{revision}/baseline-run
 *    - Answering Method: POST /api/v1/benchmarks/{logical_id}/revisions/{revision}/method-runs -> poll GET /api/v1/jobs/{job_id}
 * 8. Results: GET /api/v1/runs/{run_id}/results
 *
 * Security & Reliability Invariants:
 * - Token is held ONLY in memory; never persisted, stored in localStorage/cookie/sessionStorage, or logged.
 * - Error messages are sanitized to never contain the token.
 * - All fetch requests enforce finite timeouts and support AbortController cancellation.
 * - UUID format is strictly validated before any request.
 * - No fake data; missing backend fields display "—".
 */

export const DEFAULT_REQUEST_TIMEOUT_MS = 60_000;
export const DEFAULT_UPLOAD_TIMEOUT_MS = 120_000;
export const MAX_POLL_TIMEOUT_MS = 600_000; // 10 minutes

const UUID_REGEX = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function validateUuid(val: string): boolean {
  return UUID_REGEX.test(val.trim());
}

export function sanitizeErrorMessage(msg: string, token?: string): string {
  if (token && token.trim()) {
    const cleanToken = token.trim();
    const escaped = cleanToken.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return msg.replace(new RegExp(escaped, "gi"), "[REDACTED]");
  }
  return msg;
}

export type ApiErrorDetail = {
  message: string;
  code?: string;
  status?: number;
  details?: unknown;
};

export type ApiResult<T> =
  | { ok: true; status: number; data: T }
  | { ok: false; status?: number; error: ApiErrorDetail };

export const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

export function parseErrorBody(status: number, body: unknown, token?: string): ApiErrorDetail {
  let message = `請求失敗 (HTTP ${status})`;
  let code: string | undefined;
  let details: unknown;

  if (isRecord(body)) {
    if (isRecord(body.error)) {
      if (typeof body.error.message === "string" && body.error.message.trim()) {
        message = body.error.message.trim();
      }
      if (typeof body.error.code === "string" && body.error.code.trim()) {
        code = body.error.code.trim();
      }
      if (body.error.details !== undefined) {
        details = body.error.details;
      }
    } else if (typeof body.detail === "string" && body.detail.trim()) {
      message = body.detail.trim();
    } else if (typeof body.message === "string" && body.message.trim()) {
      message = body.message.trim();
    }
  }

  return {
    message: sanitizeErrorMessage(message, token),
    code,
    status,
    details,
  };
}

async function executeFetch<T>(
  endpoint: string,
  init: RequestInit,
  token: string,
  timeoutMs: number,
  fetchFn: typeof fetch,
  externalSignal?: AbortSignal,
): Promise<ApiResult<T>> {
  const controller = new AbortController();
  let timeoutId: ReturnType<typeof setTimeout> | undefined;

  const onExternalAbort = () => {
    controller.abort(externalSignal?.reason ?? new DOMException("Aborted", "AbortError"));
  };

  if (externalSignal) {
    if (externalSignal.aborted) {
      return {
        ok: false,
        error: {
          message: "操作已取消",
          code: "aborted",
        },
      };
    }
    externalSignal.addEventListener("abort", onExternalAbort, { once: true });
  }

  if (timeoutMs > 0 && timeoutMs < Infinity) {
    timeoutId = setTimeout(() => {
      controller.abort(new DOMException("The operation timed out.", "TimeoutError"));
    }, timeoutMs);
  }

  try {
    let res: Response;
    try {
      res = await fetchFn(endpoint, {
        ...init,
        signal: controller.signal,
      });
    } catch (err: unknown) {
      if (externalSignal?.aborted) {
        return {
          ok: false,
          error: {
            message: "操作已取消",
            code: "aborted",
          },
        };
      }
      const isTimeout =
        controller.signal.aborted &&
        (controller.signal.reason?.name === "TimeoutError" ||
          (err instanceof DOMException && err.name === "TimeoutError") ||
          (err instanceof Error && err.message.includes("timed out")));
      if (isTimeout) {
        return {
          ok: false,
          status: 408,
          error: {
            message: "請求逾時（操作超過設定時限）",
            status: 408,
            code: "timeout",
          },
        };
      }
      const rawMsg = err instanceof Error ? err.message : String(err);
      return {
        ok: false,
        error: {
          message: sanitizeErrorMessage(`連線失敗（${rawMsg}）`, token),
          code: "network_error",
        },
      };
    }

    let parsedBody: unknown = null;
    try {
      const text = await res.text();
      if (text.trim()) {
        parsedBody = JSON.parse(text);
      }
    } catch {
      parsedBody = null;
    }

    if (!res.ok) {
      const parsedError = parseErrorBody(res.status, parsedBody, token);
      return {
        ok: false,
        status: res.status,
        error: parsedError,
      };
    }

    return {
      ok: true,
      status: res.status,
      data: parsedBody as T,
    };
  } finally {
    if (timeoutId !== undefined) clearTimeout(timeoutId);
    if (externalSignal) {
      externalSignal.removeEventListener("abort", onExternalAbort);
    }
  }
}

// -----------------------------------------------------------------------------
// Step 2: Upload PDF
// -----------------------------------------------------------------------------
export type UploadDocumentResponse = {
  id: string;
  document_version_id?: string;
  project_id: string;
  resource_id: string;
  artifact_id: string;
  logical_id: string;
  revision: number;
  sha256: string;
  size_bytes: number;
  page_count: number;
  flagged?: boolean;
  warnings?: string[];
};

export async function uploadPdf({
  projectId,
  token,
  file,
  logicalId,
  fetchFn = fetch,
  signal,
  timeoutMs = DEFAULT_UPLOAD_TIMEOUT_MS,
  baseUrl = "",
}: {
  projectId: string;
  token: string;
  file: File | Blob;
  logicalId?: string;
  fetchFn?: typeof fetch;
  signal?: AbortSignal;
  timeoutMs?: number;
  baseUrl?: string;
}): Promise<ApiResult<UploadDocumentResponse>> {
  const cleanToken = token.trim();
  if (!cleanToken) {
    return { ok: false, error: { message: "未提供認證 Token", status: 401, code: "unauthorized" } };
  }
  const cleanProjId = projectId.trim();
  if (!validateUuid(cleanProjId)) {
    return { ok: false, error: { message: "Project ID 格式錯誤（須為 UUID）", status: 400, code: "bad_request" } };
  }
  if (!file || (typeof file.size === "number" && file.size === 0)) {
    return { ok: false, error: { message: "請選擇非空白的 PDF 檔案", status: 422, code: "empty_file" } };
  }

  const queryParams = new URLSearchParams({ project_id: cleanProjId });
  if (logicalId && logicalId.trim()) {
    queryParams.set("logical_id", logicalId.trim());
  }

  const endpoint = `${baseUrl}/api/v1/documents?${queryParams.toString()}`;
  return executeFetch<UploadDocumentResponse>(
    endpoint,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${cleanToken}`,
        "Content-Type": "application/pdf",
      },
      body: file,
    },
    cleanToken,
    timeoutMs,
    fetchFn,
    signal,
  );
}

// -----------------------------------------------------------------------------
// Step 3: Parse Document & Jobs
// -----------------------------------------------------------------------------
export type ParseJobResponse = {
  job_id: string;
  document_version_id: string;
  project_id: string;
  kind: string;
  status: string;
  profile?: string;
  page_selection?: number[] | null;
  budget_ref?: string | null;
};

export async function parseDocument({
  documentId,
  token,
  fetchFn = fetch,
  signal,
  timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
  baseUrl = "",
}: {
  documentId: string;
  token: string;
  fetchFn?: typeof fetch;
  signal?: AbortSignal;
  timeoutMs?: number;
  baseUrl?: string;
}): Promise<ApiResult<ParseJobResponse>> {
  const cleanToken = token.trim();
  if (!cleanToken) {
    return { ok: false, error: { message: "未提供認證 Token", status: 401, code: "unauthorized" } };
  }
  const cleanDocId = documentId.trim();
  if (!validateUuid(cleanDocId)) {
    return { ok: false, error: { message: "Document ID 格式錯誤（須為 UUID）", status: 400, code: "bad_request" } };
  }

  const endpoint = `${baseUrl}/api/v1/documents/${encodeURIComponent(cleanDocId)}/parse`;
  return executeFetch<ParseJobResponse>(
    endpoint,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${cleanToken}`,
        "Content-Type": "application/json",
      },
      body: "{}",
    },
    cleanToken,
    timeoutMs,
    fetchFn,
    signal,
  );
}

export type JobTransition = {
  from: string | null;
  to: string;
  reason: string;
  reason_truncated: boolean;
  occurred_at: string;
};

export type JobStatusResponse = {
  id: string;
  project_id: string;
  kind: string;
  stage: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled" | string;
  row_version: number;
  attempt: number;
  max_attempts: number;
  retryable: boolean;
  transitions?: JobTransition[];
  transitions_truncated?: boolean;
};

export async function getJobStatus({
  jobId,
  token,
  fetchFn = fetch,
  signal,
  timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
  baseUrl = "",
}: {
  jobId: string;
  token: string;
  fetchFn?: typeof fetch;
  signal?: AbortSignal;
  timeoutMs?: number;
  baseUrl?: string;
}): Promise<ApiResult<JobStatusResponse>> {
  const cleanToken = token.trim();
  if (!cleanToken) {
    return { ok: false, error: { message: "未提供認證 Token", status: 401, code: "unauthorized" } };
  }
  const cleanJobId = jobId.trim();
  if (!validateUuid(cleanJobId)) {
    return { ok: false, error: { message: "Job ID 格式錯誤（須為 UUID）", status: 400, code: "bad_request" } };
  }

  const endpoint = `${baseUrl}/api/v1/jobs/${encodeURIComponent(cleanJobId)}`;
  return executeFetch<JobStatusResponse>(
    endpoint,
    {
      method: "GET",
      headers: {
        Authorization: `Bearer ${cleanToken}`,
      },
    },
    cleanToken,
    timeoutMs,
    fetchFn,
    signal,
  );
}

export async function cancelJob({
  jobId,
  token,
  fetchFn = fetch,
  signal,
  timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
  baseUrl = "",
}: {
  jobId: string;
  token: string;
  fetchFn?: typeof fetch;
  signal?: AbortSignal;
  timeoutMs?: number;
  baseUrl?: string;
}): Promise<ApiResult<unknown>> {
  const cleanToken = token.trim();
  if (!cleanToken) {
    return { ok: false, error: { message: "未提供認證 Token", status: 401, code: "unauthorized" } };
  }
  const cleanJobId = jobId.trim();
  if (!validateUuid(cleanJobId)) {
    return { ok: false, error: { message: "Job ID 格式錯誤（須為 UUID）", status: 400, code: "bad_request" } };
  }

  const endpoint = `${baseUrl}/api/v1/jobs/${encodeURIComponent(cleanJobId)}/cancel`;
  return executeFetch<unknown>(
    endpoint,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${cleanToken}`,
      },
    },
    cleanToken,
    timeoutMs,
    fetchFn,
    signal,
  );
}

// -----------------------------------------------------------------------------
// Step 4: Benchmark + Plan
// -----------------------------------------------------------------------------
export type CreateBenchmarkResponse = {
  benchmark_revision_id: string;
  logical_id: string;
  revision: number;
  status: string;
  document_version_id: string;
  n_target: number;
  q_min: number;
};

export async function createBenchmark({
  projectId,
  documentId,
  logicalId,
  token,
  fetchFn = fetch,
  signal,
  timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
  baseUrl = "",
}: {
  projectId: string;
  documentId: string;
  logicalId?: string;
  token: string;
  fetchFn?: typeof fetch;
  signal?: AbortSignal;
  timeoutMs?: number;
  baseUrl?: string;
}): Promise<ApiResult<CreateBenchmarkResponse>> {
  const cleanToken = token.trim();
  if (!cleanToken) {
    return { ok: false, error: { message: "未提供認證 Token", status: 401, code: "unauthorized" } };
  }
  const cleanProjId = projectId.trim();
  if (!validateUuid(cleanProjId)) {
    return { ok: false, error: { message: "Project ID 格式錯誤（須為 UUID）", status: 400, code: "bad_request" } };
  }
  const cleanDocId = documentId.trim();
  if (!validateUuid(cleanDocId)) {
    return { ok: false, error: { message: "Document ID 格式錯誤（須為 UUID）", status: 400, code: "bad_request" } };
  }

  const endpoint = `${baseUrl}/api/v1/benchmarks?project_id=${encodeURIComponent(cleanProjId)}`;
  const body: Record<string, string> = {
    document_version_id: cleanDocId,
  };
  if (logicalId && logicalId.trim()) {
    body.logical_id = logicalId.trim();
  }

  return executeFetch<CreateBenchmarkResponse>(
    endpoint,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${cleanToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    },
    cleanToken,
    timeoutMs,
    fetchFn,
    signal,
  );
}

export type BenchmarkPlanResponse = {
  benchmark_revision_id: string;
  logical_id: string;
  revision: number;
  status: string;
  parse_job_id: string;
  promotion_rule?: string;
  pages?: {
    usable: number[];
    insufficient_material: Record<string, string>;
  };
  buckets?: Record<
    string,
    {
      status: string;
      required_minimum: number;
      reason: string | null;
    }
  >;
  human_review?: {
    status: string;
    reason?: string | null;
  };
  compliance_projection?: string;
};

export async function planBenchmark({
  projectId,
  logicalId,
  revision = 1,
  parseJobId,
  token,
  fetchFn = fetch,
  signal,
  timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
  baseUrl = "",
}: {
  projectId: string;
  logicalId: string;
  revision?: number;
  parseJobId?: string;
  token: string;
  fetchFn?: typeof fetch;
  signal?: AbortSignal;
  timeoutMs?: number;
  baseUrl?: string;
}): Promise<ApiResult<BenchmarkPlanResponse>> {
  const cleanToken = token.trim();
  if (!cleanToken) {
    return { ok: false, error: { message: "未提供認證 Token", status: 401, code: "unauthorized" } };
  }
  const cleanProjId = projectId.trim();
  if (!validateUuid(cleanProjId)) {
    return { ok: false, error: { message: "Project ID 格式錯誤（須為 UUID）", status: 400, code: "bad_request" } };
  }
  const cleanLogicalId = logicalId.trim();
  if (!cleanLogicalId) {
    return { ok: false, error: { message: "Benchmark Logical ID 不得為空", status: 400, code: "bad_request" } };
  }

  const endpoint = `${baseUrl}/api/v1/benchmarks/${encodeURIComponent(cleanLogicalId)}/revisions/${revision}/plan?project_id=${encodeURIComponent(cleanProjId)}`;
  const body = parseJobId && parseJobId.trim() ? JSON.stringify({ parse_job_id: parseJobId.trim() }) : "{}";

  return executeFetch<BenchmarkPlanResponse>(
    endpoint,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${cleanToken}`,
        "Content-Type": "application/json",
      },
      body,
    },
    cleanToken,
    timeoutMs,
    fetchFn,
    signal,
  );
}

// -----------------------------------------------------------------------------
// Step 5: Question Generation
// -----------------------------------------------------------------------------
export type StartGenerationResponse = {
  job_id: string;
  benchmark_revision_id: string;
  status: string;
  phase: string;
  benchmark_status: string;
  model: string;
  max_output_tokens: number;
  budget_cap_micros: number;
  benchmark_cap_micros: number;
  general_target: number;
  cross_page_target: number;
  confusable_target: number;
};

export type GenerationProgressResponse = {
  benchmark_revision_id: string;
  logical_id: string;
  revision: number;
  benchmark_status: string;
  general_target: number;
  cross_page_target: number;
  confusable_target: number;
  human_review: string;
  limits: string[];
  questions: number;
  by_bucket: {
    general: number;
    cross_page: number;
    confusable: number;
  };
  shortfall: number;
  job?: {
    job_id: string;
    phase: string;
    status: "queued" | "running" | "succeeded" | "failed" | "cancelled" | string;
    attempt: number;
    model: string;
    max_output_tokens: number;
    currency: string;
    pricing_revision: string;
  } | null;
  ledger?: {
    calls_by_state: Record<string, number>;
    spent_micros: number;
    held_micros: number;
    cap_micros: number;
    remaining_micros: number;
  };
  last_generation?: string | null;
  validation?: {
    pass: number;
    fail: number;
    needs_review: number;
    unvalidated: number;
    note?: string;
  };
};

export async function startQuestionGeneration({
  projectId,
  logicalId,
  revision = 1,
  model = "claude-haiku-4-5-20251001",
  budgetCapMicros = 200000,
  token,
  fetchFn = fetch,
  signal,
  timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
  baseUrl = "",
}: {
  projectId: string;
  logicalId: string;
  revision?: number;
  model?: string;
  budgetCapMicros?: number;
  token: string;
  fetchFn?: typeof fetch;
  signal?: AbortSignal;
  timeoutMs?: number;
  baseUrl?: string;
}): Promise<ApiResult<StartGenerationResponse>> {
  const cleanToken = token.trim();
  if (!cleanToken) {
    return { ok: false, error: { message: "未提供認證 Token", status: 401, code: "unauthorized" } };
  }
  const cleanProjId = projectId.trim();
  if (!validateUuid(cleanProjId)) {
    return { ok: false, error: { message: "Project ID 格式錯誤（須為 UUID）", status: 400, code: "bad_request" } };
  }

  const endpoint = `${baseUrl}/api/v1/benchmarks/${encodeURIComponent(logicalId.trim())}/revisions/${revision}/generate?project_id=${encodeURIComponent(cleanProjId)}`;
  return executeFetch<StartGenerationResponse>(
    endpoint,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${cleanToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: model.trim(),
        budget_cap_micros: Math.floor(budgetCapMicros),
      }),
    },
    cleanToken,
    timeoutMs,
    fetchFn,
    signal,
  );
}

export async function getGenerationProgress({
  projectId,
  logicalId,
  revision = 1,
  token,
  fetchFn = fetch,
  signal,
  timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
  baseUrl = "",
}: {
  projectId: string;
  logicalId: string;
  revision?: number;
  token: string;
  fetchFn?: typeof fetch;
  signal?: AbortSignal;
  timeoutMs?: number;
  baseUrl?: string;
}): Promise<ApiResult<GenerationProgressResponse>> {
  const cleanToken = token.trim();
  if (!cleanToken) {
    return { ok: false, error: { message: "未提供認證 Token", status: 401, code: "unauthorized" } };
  }
  const cleanProjId = projectId.trim();
  if (!validateUuid(cleanProjId)) {
    return { ok: false, error: { message: "Project ID 格式錯誤（須為 UUID）", status: 400, code: "bad_request" } };
  }

  const endpoint = `${baseUrl}/api/v1/benchmarks/${encodeURIComponent(logicalId.trim())}/revisions/${revision}/generate?project_id=${encodeURIComponent(cleanProjId)}`;
  return executeFetch<GenerationProgressResponse>(
    endpoint,
    {
      method: "GET",
      headers: {
        Authorization: `Bearer ${cleanToken}`,
      },
    },
    cleanToken,
    timeoutMs,
    fetchFn,
    signal,
  );
}

// -----------------------------------------------------------------------------
// Step 6: Automated Question Validation
// -----------------------------------------------------------------------------
export async function startQuestionValidation({
  projectId,
  logicalId,
  revision = 1,
  model = "claude-haiku-4-5-20251001",
  budgetCapMicros = 200000,
  token,
  fetchFn = fetch,
  signal,
  timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
  baseUrl = "",
}: {
  projectId: string;
  logicalId: string;
  revision?: number;
  model?: string;
  budgetCapMicros?: number;
  token: string;
  fetchFn?: typeof fetch;
  signal?: AbortSignal;
  timeoutMs?: number;
  baseUrl?: string;
}): Promise<ApiResult<StartGenerationResponse>> {
  const cleanToken = token.trim();
  if (!cleanToken) {
    return { ok: false, error: { message: "未提供認證 Token", status: 401, code: "unauthorized" } };
  }
  const cleanProjId = projectId.trim();
  if (!validateUuid(cleanProjId)) {
    return { ok: false, error: { message: "Project ID 格式錯誤（須為 UUID）", status: 400, code: "bad_request" } };
  }

  const endpoint = `${baseUrl}/api/v1/benchmarks/${encodeURIComponent(logicalId.trim())}/revisions/${revision}/validate?project_id=${encodeURIComponent(cleanProjId)}`;
  return executeFetch<StartGenerationResponse>(
    endpoint,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${cleanToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: model.trim(),
        budget_cap_micros: Math.floor(budgetCapMicros),
      }),
    },
    cleanToken,
    timeoutMs,
    fetchFn,
    signal,
  );
}

// -----------------------------------------------------------------------------
// Step 7: Freeze Benchmark
// -----------------------------------------------------------------------------
export type FreezeResponse = {
  benchmark_revision_id: string;
  logical_id: string;
  revision: number;
  status: "frozen";
  compliance: "partial";
  frozen_at: string;
  questions: number;
  by_bucket: Record<string, number>;
  n_target: number;
  q_min: number;
  shortfall: Record<string, number>;
  validation: {
    pass: number;
    fail: number;
    needs_review: number;
    unvalidated: number;
  };
  human_review: string;
  limits: string[];
};

export async function freezeBenchmark({
  projectId,
  logicalId,
  revision = 1,
  token,
  fetchFn = fetch,
  signal,
  timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
  baseUrl = "",
}: {
  projectId: string;
  logicalId: string;
  revision?: number;
  token: string;
  fetchFn?: typeof fetch;
  signal?: AbortSignal;
  timeoutMs?: number;
  baseUrl?: string;
}): Promise<ApiResult<FreezeResponse>> {
  const cleanToken = token.trim();
  if (!cleanToken) {
    return { ok: false, error: { message: "未提供認證 Token", status: 401, code: "unauthorized" } };
  }
  const cleanProjId = projectId.trim();
  if (!validateUuid(cleanProjId)) {
    return { ok: false, error: { message: "Project ID 格式錯誤（須為 UUID）", status: 400, code: "bad_request" } };
  }

  const endpoint = `${baseUrl}/api/v1/benchmarks/${encodeURIComponent(logicalId.trim())}/revisions/${revision}/freeze?project_id=${encodeURIComponent(cleanProjId)}`;
  return executeFetch<FreezeResponse>(
    endpoint,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${cleanToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ compliance: "partial" }),
    },
    cleanToken,
    timeoutMs,
    fetchFn,
    signal,
  );
}

// -----------------------------------------------------------------------------
// Step 8: Run Methods
// -----------------------------------------------------------------------------
export type BaselineRunResponse = {
  run_id: string;
  evaluation_id: string;
  method: string;
  status: "completed" | string;
  evaluation_status: string;
  questions: number;
  results_url: string;
  limits: string[];
};

export async function runBaseline({
  projectId,
  logicalId,
  revision = 1,
  token,
  fetchFn = fetch,
  signal,
  timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
  baseUrl = "",
}: {
  projectId: string;
  logicalId: string;
  revision?: number;
  token: string;
  fetchFn?: typeof fetch;
  signal?: AbortSignal;
  timeoutMs?: number;
  baseUrl?: string;
}): Promise<ApiResult<BaselineRunResponse>> {
  const cleanToken = token.trim();
  if (!cleanToken) {
    return { ok: false, error: { message: "未提供認證 Token", status: 401, code: "unauthorized" } };
  }
  const cleanProjId = projectId.trim();
  if (!validateUuid(cleanProjId)) {
    return { ok: false, error: { message: "Project ID 格式錯誤（須為 UUID）", status: 400, code: "bad_request" } };
  }

  const endpoint = `${baseUrl}/api/v1/benchmarks/${encodeURIComponent(logicalId.trim())}/revisions/${revision}/baseline-run?project_id=${encodeURIComponent(cleanProjId)}`;
  return executeFetch<BaselineRunResponse>(
    endpoint,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${cleanToken}`,
        "Content-Type": "application/json",
      },
      body: "{}",
    },
    cleanToken,
    timeoutMs,
    fetchFn,
    signal,
  );
}

export type MethodRunResponse = {
  run_id: string;
  job_id: string;
  method: string;
  status: "queued" | string;
  model: string;
  max_output_tokens: number;
  budget_cap_micros: number;
  benchmark_cap_micros: number;
  prompt_version: string;
  limits: string[];
  results_url?: string;
};

export async function startMethodRun({
  projectId,
  logicalId,
  revision = 1,
  model = "claude-haiku-4-5-20251001",
  budgetCapMicros = 200000,
  token,
  fetchFn = fetch,
  signal,
  timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
  baseUrl = "",
}: {
  projectId: string;
  logicalId: string;
  revision?: number;
  model?: string;
  budgetCapMicros?: number;
  token: string;
  fetchFn?: typeof fetch;
  signal?: AbortSignal;
  timeoutMs?: number;
  baseUrl?: string;
}): Promise<ApiResult<MethodRunResponse>> {
  const cleanToken = token.trim();
  if (!cleanToken) {
    return { ok: false, error: { message: "未提供認證 Token", status: 401, code: "unauthorized" } };
  }
  const cleanProjId = projectId.trim();
  if (!validateUuid(cleanProjId)) {
    return { ok: false, error: { message: "Project ID 格式錯誤（須為 UUID）", status: 400, code: "bad_request" } };
  }

  const endpoint = `${baseUrl}/api/v1/benchmarks/${encodeURIComponent(logicalId.trim())}/revisions/${revision}/method-runs?project_id=${encodeURIComponent(cleanProjId)}`;
  return executeFetch<MethodRunResponse>(
    endpoint,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${cleanToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: model.trim(),
        budget_cap_micros: Math.floor(budgetCapMicros),
      }),
    },
    cleanToken,
    timeoutMs,
    fetchFn,
    signal,
  );
}

export type JudgeRunResponse = {
  job_id: string;
  run_id: string;
  status: string;
  answered_submissions?: number;
};

export type JudgeSummary = {
  judged_questions: number;
  questions_with_a_contradicted_fact: number;
  model: string;
  prompt_revision: string;
  limitation: string;
};

export async function startJudgeRun({
  projectId,
  runId,
  model = "claude-haiku-4-5-20251001",
  budgetCapMicros = 200000,
  token,
  fetchFn = fetch,
  signal,
  timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
  baseUrl = "",
}: {
  projectId: string;
  runId: string;
  model?: string;
  budgetCapMicros?: number;
  token: string;
  fetchFn?: typeof fetch;
  signal?: AbortSignal;
  timeoutMs?: number;
  baseUrl?: string;
}): Promise<ApiResult<JudgeRunResponse>> {
  const cleanToken = token.trim();
  if (!cleanToken) {
    return { ok: false, error: { message: "未提供認證 Token", status: 401, code: "unauthorized" } };
  }
  const cleanProjId = projectId.trim();
  if (!validateUuid(cleanProjId)) {
    return { ok: false, error: { message: "Project ID 格式錯誤（須為 UUID）", status: 400, code: "bad_request" } };
  }
  const cleanRunId = runId.trim();
  if (!validateUuid(cleanRunId)) {
    return { ok: false, error: { message: "Run ID 格式錯誤（須為 UUID）", status: 400, code: "bad_request" } };
  }

  const endpoint = `${baseUrl}/api/v1/runs/${encodeURIComponent(cleanRunId)}/judge-runs?project_id=${encodeURIComponent(cleanProjId)}`;
  return executeFetch<JudgeRunResponse>(
    endpoint,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${cleanToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: model.trim(),
        budget_cap_micros: Math.floor(budgetCapMicros),
      }),
    },
    cleanToken,
    timeoutMs,
    fetchFn,
    signal,
  );
}

// -----------------------------------------------------------------------------
// Step 9: Results
// -----------------------------------------------------------------------------
export type MetricSummary = {
  metric_name: string;
  scored: number;
  not_applicable: number;
  pending: number;
  error: number;
  mean: number | null;
};

export type ResultItem = {
  question_id: string;
  ordinal: number;
  quota_bucket: string;
  metric_name: string;
  status: string;
  raw_value: number | null;
  reason_codes: string[];
};

export type RunResultsResponse = {
  run_id: string;
  // The API nests these under "evaluation"; the flat fields are kept optional for older callers.
  evaluation?: { id: string; grader_revision: string; status: string; created_at?: string };
  evaluation_id?: string;
  grader_revision?: string;
  evaluation_status?: string;
  provisional?: boolean;
  /** Every metric value and mean is on this 0-100 scale. */
  scale?: string;
  items?: ResultItem[];
  metrics: MetricSummary[];
  submissions?: {
    answered: number;
    abstained_by_model: number;
    /** Submissions of a method with no model behind it (the lexical baseline only returns pages): no answer, not a model's abstention. */
    no_answer_retrieval_only?: number;
    method_failures: Record<string, number>;
  };
  judge?: JudgeSummary | null;
  excluded_questions?: { ordinal: number; reason: string }[];
};

export async function getRunResults({
  runId,
  evaluationId,
  metricName,
  limit,
  token,
  fetchFn = fetch,
  signal,
  timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
  baseUrl = "",
}: {
  runId: string;
  evaluationId?: string;
  metricName?: string;
  limit?: number;
  token: string;
  fetchFn?: typeof fetch;
  signal?: AbortSignal;
  timeoutMs?: number;
  baseUrl?: string;
}): Promise<ApiResult<RunResultsResponse>> {
  const cleanToken = token.trim();
  if (!cleanToken) {
    return { ok: false, error: { message: "未提供認證 Token", status: 401, code: "unauthorized" } };
  }
  const cleanRunId = runId.trim();
  if (!validateUuid(cleanRunId)) {
    return { ok: false, error: { message: "Run ID 格式錯誤（須為 UUID）", status: 400, code: "bad_request" } };
  }

  const queryParams = new URLSearchParams();
  if (evaluationId && evaluationId.trim()) {
    queryParams.set("evaluation_id", evaluationId.trim());
  }
  if (metricName) queryParams.set("metric_name", metricName);
  if (limit) queryParams.set("limit", String(limit));
  const qs = queryParams.toString();
  const endpoint = `${baseUrl}/api/v1/runs/${encodeURIComponent(cleanRunId)}/results${qs ? `?${qs}` : ""}`;

  return executeFetch<RunResultsResponse>(
    endpoint,
    {
      method: "GET",
      headers: {
        Authorization: `Bearer ${cleanToken}`,
      },
    },
    cleanToken,
    timeoutMs,
    fetchFn,
    signal,
  );
}

// -----------------------------------------------------------------------------
// Question list (public view: no gold, no answers)
// -----------------------------------------------------------------------------
export type PublicQuestion = { id: string; question_text: string; language?: string };

export type QuestionListResponse = {
  benchmark_revision_id: string;
  status: string;
  items: PublicQuestion[];
  next_cursor: string | null;
};

export async function listQuestions({
  projectId,
  logicalId,
  revision = 1,
  cursor,
  token,
  fetchFn = fetch,
  signal,
  timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
  baseUrl = "",
}: {
  projectId: string;
  logicalId: string;
  revision?: number;
  cursor?: string | null;
  token: string;
  fetchFn?: typeof fetch;
  signal?: AbortSignal;
  timeoutMs?: number;
  baseUrl?: string;
}): Promise<ApiResult<QuestionListResponse>> {
  const cleanToken = token.trim();
  if (!cleanToken) {
    return { ok: false, error: { message: "未提供認證 Token", status: 401, code: "unauthorized" } };
  }
  const cleanProjId = projectId.trim();
  if (!validateUuid(cleanProjId)) {
    return { ok: false, error: { message: "Project ID 格式錯誤（須為 UUID）", status: 400, code: "bad_request" } };
  }
  const qs = new URLSearchParams({ project_id: cleanProjId, limit: "100" });
  if (cursor) qs.set("cursor", cursor);
  const endpoint = `${baseUrl}/api/v1/benchmarks/${encodeURIComponent(logicalId.trim())}/revisions/${revision}/questions?${qs.toString()}`;
  return executeFetch<QuestionListResponse>(
    endpoint,
    { method: "GET", headers: { Authorization: `Bearer ${cleanToken}` } },
    cleanToken,
    timeoutMs,
    fetchFn,
    signal,
  );
}

// -----------------------------------------------------------------------------
// Review the questions and exclude some before the freeze (owner only for the action)
// -----------------------------------------------------------------------------
export type ReviewItem = {
  id: string;
  ordinal: number;
  quota_bucket: string;
  question_text: string;
  verdict: "pass" | "fail" | "needs_review" | "unvalidated" | string;
  verdict_reason: string | null;
  excluded: boolean;
  exclusion_reason: string | null;
};

export type ReviewResponse = {
  benchmark_revision_id: string;
  status: string;
  items: ReviewItem[];
  total: number;
  active: number;
  excluded: number;
  validation_of_active: { pass: number; fail: number; needs_review: number; unvalidated: number };
  can_freeze: boolean;
  note?: string;
};

type BenchRef = {
  projectId: string;
  logicalId: string;
  revision?: number;
  token: string;
  fetchFn?: typeof fetch;
  signal?: AbortSignal;
  timeoutMs?: number;
  baseUrl?: string;
};

function checkRef(ref: BenchRef): ApiResult<never> | null {
  if (!ref.token.trim()) return { ok: false, error: { message: "未提供認證 Token", status: 401, code: "unauthorized" } };
  if (!validateUuid(ref.projectId.trim())) {
    return { ok: false, error: { message: "Project ID 格式錯誤（須為 UUID）", status: 400, code: "bad_request" } };
  }
  return null;
}

export async function getQuestionReview(ref: BenchRef): Promise<ApiResult<ReviewResponse>> {
  const bad = checkRef(ref);
  if (bad) return bad;
  const endpoint = `${ref.baseUrl ?? ""}/api/v1/benchmarks/${encodeURIComponent(ref.logicalId.trim())}/revisions/${ref.revision ?? 1}/questions/review?project_id=${encodeURIComponent(ref.projectId.trim())}`;
  return executeFetch<ReviewResponse>(
    endpoint, { method: "GET", headers: { Authorization: `Bearer ${ref.token.trim()}` } },
    ref.token.trim(), ref.timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS, ref.fetchFn ?? fetch, ref.signal);
}

export async function setQuestionExcluded(
  ref: BenchRef & { questionId: string; excluded: boolean; reason: string },
): Promise<ApiResult<{ question_id: string; excluded: boolean; active_questions: number }>> {
  const bad = checkRef(ref);
  if (bad) return bad;
  if (!validateUuid(ref.questionId)) {
    return { ok: false, error: { message: "題目 ID 格式錯誤", status: 400, code: "bad_request" } };
  }
  const endpoint = `${ref.baseUrl ?? ""}/api/v1/benchmarks/${encodeURIComponent(ref.logicalId.trim())}/revisions/${ref.revision ?? 1}/questions/${ref.questionId}/exclusion?project_id=${encodeURIComponent(ref.projectId.trim())}`;
  return executeFetch(
    endpoint,
    {
      method: "POST",
      headers: { Authorization: `Bearer ${ref.token.trim()}`, "Content-Type": "application/json" },
      body: JSON.stringify({ excluded: ref.excluded, reason: ref.reason.trim() }),
    },
    ref.token.trim(), ref.timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS, ref.fetchFn ?? fetch, ref.signal);
}
