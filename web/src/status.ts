// Readiness of the LAB API as shown in the UI. Every state comes from a real
// HTTP response (or its absence); nothing here invents a "ready" result.
export type CheckState = { ok: boolean; detail: string };

export type ApiStatus =
  | { kind: "loading" }
  | { kind: "ready" | "not_ready"; providerMode: string; checks: Record<string, CheckState> }
  | { kind: "error"; message: string };

export async function fetchApiStatus(fetchFn: typeof fetch = fetch): Promise<ApiStatus> {
  let res: Response;
  try {
    res = await fetchFn("/api/readyz", { headers: { accept: "application/json" } });
  } catch {
    return { kind: "error", message: "無法連線到 LAB API" };
  }
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    return { kind: "error", message: `LAB API 回應無法解析（HTTP ${res.status}）` };
  }
  const b = body as { status?: unknown; provider_mode?: unknown; checks?: unknown };
  const validStatus =
    (res.status === 200 && b.status === "ready") || (res.status === 503 && b.status === "not_ready");
  if (!validStatus || typeof b.provider_mode !== "string" || typeof b.checks !== "object" || b.checks === null) {
    return { kind: "error", message: `LAB API 回應格式不符（HTTP ${res.status}）` };
  }
  return {
    kind: b.status as "ready" | "not_ready",
    providerMode: b.provider_mode,
    checks: b.checks as Record<string, CheckState>,
  };
}
