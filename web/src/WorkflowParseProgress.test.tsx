import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Workflow } from "./Workflow";

afterEach(cleanup);

const U1 = "11111111-1111-1111-1111-111111111111";
const U2 = "22222222-2222-2222-2222-222222222222";
const JOB = "44444444-4444-4444-4444-444444444444";
const SHA = "a".repeat(64);
const TOKEN = `lab_act_${U1}_${SHA}`;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function backend(jobAnswers: unknown[]) {
  let polls = 0;
  const f = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const u = String(url);
    const method = init?.method || "GET";
    if (u.includes("/api/v1/workspace")) return json({ documents: [], benchmarks: [] });
    if (u.includes("/api/v1/documents?") && method === "POST") {
      return json({ id: U2, document_version_id: U2, project_id: U1, resource_id: U2, artifact_id: U2, logical_id: "d", revision: 1, sha256: SHA, size_bytes: 10, page_count: 834 }, 201);
    }
    if (u.includes("/parse") && method === "POST") return json({ job_id: JOB, status: "queued" }, 202);
    if (u.includes(`/api/v1/jobs/${JOB}`)) return json(jobAnswers[Math.min(polls++, jobAnswers.length - 1)]);
    return json({ error: { message: "not found" } }, 404);
  });
  return f as unknown as typeof fetch;
}

async function startParse(f: typeof fetch) {
  render(<Workflow fetchFn={f} />);
  fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: TOKEN } });
  fireEvent.change(screen.getByLabelText(/Project ID/i), { target: { value: U1 } });
  fireEvent.change(screen.getByLabelText(/選擇 PDF 檔案/i), { target: { files: [new File(["%PDF-1.4"], "a.pdf", { type: "application/pdf" })] } });
  fireEvent.click(screen.getByRole("button", { name: "上傳 PDF" }));
  await waitFor(() => expect(screen.getByTestId("upload-result")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "開始解析" }));
}

const base = { id: JOB, project_id: U1, kind: "document.parse", stage: "queued", row_version: 1, attempt: 1, max_attempts: 3, retryable: true };

describe("Workflow: parse progress (#205)", () => {
  it("shows a progress bar with pages and percent while the parse runs, and no bar before the first number", async () => {
    const f = backend([
      { ...base, status: "running", progress: null },
      { ...base, status: "running", progress: { pages_done: 120, pages_total: 834 } },
      { ...base, status: "succeeded", progress: { pages_done: 834, pages_total: 834 } },
    ]);
    await startParse(f);
    await waitFor(() => expect(screen.getByTestId("parse-progress-none").textContent).toContain("還沒有進度數字"));
    expect(screen.queryByTestId("parse-progress")).toBeNull();
    await waitFor(() => expect(screen.getByTestId("parse-progress-text").textContent).toContain("已處理 120 / 834 頁（14%）"), { timeout: 8000 });
    const bar = screen.getByLabelText("解析進度") as HTMLProgressElement;
    expect(bar.value).toBe(14);
    await waitFor(() => expect(screen.getByTestId("step3-status").textContent).toBe("succeeded"), { timeout: 8000 });
    expect(screen.queryByTestId("parse-progress")).toBeNull();                       // finished: the bar goes away
  }, 25000);

  it("a failed parse says WHY in a short code and where to look", async () => {
    const f = backend([{ ...base, status: "failed", progress: { pages_done: 834, pages_total: 834 },
      transitions: [{ from: "running", to: "failed", reason: "persist_failed", reason_truncated: false, occurred_at: "2026-11-16T07:53:58Z" }] }]);
    await startParse(f);
    await waitFor(() => expect(screen.getByTestId("step3-failure-reason").textContent).toContain("persist_failed"), { timeout: 8000 });
    expect(screen.getByTestId("step3-failure-reason").textContent).toContain("logs/app.log");
    expect(screen.getByText(/解析任務失敗/)).toBeTruthy();
  }, 15000);
});
