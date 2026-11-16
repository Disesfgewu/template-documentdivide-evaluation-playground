import { describe, expect, it } from "vitest";
import { getJobStatus, logRequestId } from "./workflowApi";

const JOB = "11111111-1111-1111-1111-111111111111";
const ID = "req-" + "ab12cd34".repeat(4);

const failing = (headers: Record<string, string>) => async () =>
  new Response(JSON.stringify({ error: { message: "沒有權限", code: "permission_denied" } }), { status: 403, headers });

describe("the request id that is in logs/app.log", () => {
  it("is appended to a failed action's message and kept on the error", async () => {
    const r = await getJobStatus({ jobId: JOB, token: "t", fetchFn: failing({ "x-lab-request-id": ID }) as unknown as typeof fetch });
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.error.message).toContain("沒有權限");
    expect(r.error.message).toContain(`請求編號 ${ID}`);
    expect(r.error.message).toContain("logs/app.log");
    expect(r.error.requestId).toBe(ID);
    expect(r.error.code).toBe("permission_denied");
  });

  it("is not added when the response has none", async () => {
    const r = await getJobStatus({ jobId: JOB, token: "t", fetchFn: failing({}) as unknown as typeof fetch });
    if (r.ok) throw new Error("expected a failure");
    expect(r.error.message).toBe("沒有權限");
    expect(r.error.requestId).toBeUndefined();
  });

  it("shows only an id of the expected shape (it is rendered on the page)", () => {
    const h = (v: string) => ({ headers: new Headers({ "x-lab-request-id": v }) });
    expect(logRequestId(h(ID))).toBe(ID);
    for (const bad of ["<script>alert(1)</script>", "req-xyz", "abc", "req-" + "a".repeat(200)]) {
      expect(logRequestId(h(bad))).toBeUndefined();
    }
  });
});
