// Readiness of the LAB API as shown in the UI. Every state comes from a real
// HTTP response (or its absence); nothing here invents a "ready" result, and
// any inconsistent or malformed response is shown as an error.
export type CheckState = { ok: boolean; detail: string };

export type GenerationStatus = {
  state: "worker_running" | "no_worker" | "unknown";
  providerHost: string | null;
  model: string | null;
  lastCallAt: string | null;
  detail: string | null;
};

export type ApiStatus =
  | { kind: "loading" }
  | {
      kind: "ready" | "not_ready";
      providerMode: string;
      generation: GenerationStatus;
      checks: Record<string, CheckState>;
    }
  | { kind: "error"; message: string };

export const STATUS_TIMEOUT_MS = 10_000;

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

const DEFAULT_GENERATION: GenerationStatus = {
  state: "unknown",
  providerHost: null,
  model: null,
  lastCallAt: null,
  detail: null,
};

function parseGeneration(v: unknown): GenerationStatus {
  if (!isRecord(v)) return DEFAULT_GENERATION;
  if (v.state !== "worker_running" && v.state !== "no_worker" && v.state !== "unknown") {
    return DEFAULT_GENERATION;
  }
  const state = v.state;

  let providerHost: string | null = null;
  if (typeof v.provider_host === "string") {
    providerHost = v.provider_host;
  } else if (v.provider_host !== null && v.provider_host !== undefined) {
    return DEFAULT_GENERATION;
  }

  let model: string | null = null;
  let lastCallAt: string | null = null;
  if (v.last_real_call !== null && v.last_real_call !== undefined) {
    if (!isRecord(v.last_real_call)) {
      return DEFAULT_GENERATION;
    }
    if (typeof v.last_real_call.model !== "string" || typeof v.last_real_call.at !== "string") {
      return DEFAULT_GENERATION;
    }
    model = v.last_real_call.model;
    lastCallAt = v.last_real_call.at;
  }

  let detail: string | null = null;
  if (typeof v.detail === "string") {
    detail = v.detail;
  } else if (v.detail !== null && v.detail !== undefined) {
    return DEFAULT_GENERATION;
  }

  return { state, providerHost, model, lastCallAt, detail };
}

function parseChecks(v: unknown): Record<string, CheckState> | null {
  if (!isRecord(v)) return null;
  const out: Record<string, CheckState> = {};
  for (const [name, c] of Object.entries(v)) {
    if (!isRecord(c) || typeof c.ok !== "boolean" || typeof c.detail !== "string") return null;
    out[name] = { ok: c.ok, detail: c.detail };
  }
  return out;
}

export async function fetchApiStatus(
  fetchFn: typeof fetch = fetch,
  timeoutMs: number = STATUS_TIMEOUT_MS,
): Promise<ApiStatus> {
  let res: Response;
  try {
    res = await fetchFn("/api/readyz", {
      headers: { accept: "application/json" },
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch {
    return { kind: "error", message: "無法連線到 LAB API（或逾時）" };
  }
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    return { kind: "error", message: `LAB API 回應無法解析（HTTP ${res.status}）` };
  }
  const bad = { kind: "error", message: `LAB API 回應格式不符（HTTP ${res.status}）` } as const;
  if (!isRecord(body) || typeof body.provider_mode !== "string") return bad;
  const generation = parseGeneration(body.generation);
  const checks = parseChecks(body.checks);
  if (checks === null) return bad;
  const names = Object.keys(checks);
  const allOk = names.length > 0 && names.every((n) => checks[n].ok);
  // "ready" only when HTTP status, reported status and every check agree.
  if (res.status === 200 && body.status === "ready" && allOk) {
    return { kind: "ready", providerMode: body.provider_mode, generation, checks };
  }
  if (res.status === 503 && body.status === "not_ready" && !allOk) {
    return { kind: "not_ready", providerMode: body.provider_mode, generation, checks };
  }
  return bad;
}
