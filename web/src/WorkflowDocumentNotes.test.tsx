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

function backend(opts: { flags?: string[]; reject?: { code: string; message: string }; usable?: number; noText?: boolean }) {
  return vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const u = String(url);
    const method = init?.method || "GET";
    if (u.includes("/api/v1/workspace")) return json({ documents: [], benchmarks: [] });
    if (u.includes("/api/v1/documents?") && method === "POST") {
      if (opts.reject) return json({ error: { code: opts.reject.code, message: opts.reject.message, retryable: false } }, 422);
      return json({ id: U2, document_version_id: U2, project_id: U1, resource_id: U2, artifact_id: U2, logical_id: "d", revision: 1, sha256: SHA, size_bytes: 10, page_count: 834,
        flagged: Boolean(opts.flags?.length), flags: opts.flags ?? [] }, 201);
    }
    if (u.includes("/parse") && method === "POST") return json({ job_id: JOB, status: "queued" }, 202);
    if (u.includes(`/api/v1/jobs/${JOB}`)) return json({ id: JOB, project_id: U1, kind: "document.parse", stage: "finishing", status: "succeeded", row_version: 1, attempt: 1, max_attempts: 3, retryable: true });
    if (u.includes("/api/v1/benchmarks?") && method === "POST") return json({ benchmark_revision_id: U1, logical_id: "b", revision: 1, status: "parsing", document_version_id: U2, n_target: 20, q_min: 2 }, 201);
    if (u.includes("/plan") && method === "POST") {
      return json({ benchmark_revision_id: U1, logical_id: "b", revision: 1, status: "planning", parse_job_id: JOB, compliance_projection: "partial",
        pages: { usable: Array.from({ length: opts.usable ?? 3 }, (_, i) => i), insufficient_material: {} }, document_flags: opts.flags ?? [], no_text_layer: Boolean(opts.noText) });
    }
    return json({ error: { message: "not found" } }, 404);
  }) as unknown as typeof fetch;
}

async function upload(f: typeof fetch) {
  render(<Workflow fetchFn={f} />);
  fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: TOKEN } });
  fireEvent.change(screen.getByLabelText(/Project ID/i), { target: { value: U1 } });
  fireEvent.change(screen.getByLabelText(/選擇 PDF 檔案/i), { target: { files: [new File(["%PDF-1.4"], "a.pdf", { type: "application/pdf" })] } });
  fireEvent.click(screen.getByRole("button", { name: "上傳 PDF" }));
}

describe("Workflow: what the owner is told about the document (#204, #203)", () => {
  it("shows the notes recorded at upload in zh-TW: a listed reader note is harmless, a missing text layer says no OCR", async () => {
    await upload(backend({ flags: ["no_text_layer", "xref_not_zero_indexed"] }));
    await waitFor(() => expect(screen.getAllByTestId("upload-flag").length).toBe(2));
    const text = screen.getAllByTestId("upload-flag").map((e) => e.textContent).join("\n");
    expect(text).toContain("沒有文字層");
    expect(text).toContain("不做 OCR");
    expect(text).toContain("沒有被修改");
  });

  it("shows no note for a clean document", async () => {
    await upload(backend({ flags: [] }));
    await waitFor(() => expect(screen.getByTestId("upload-result")).toBeTruthy());
    expect(screen.queryAllByTestId("upload-flag")).toEqual([]);
  });

  it("names a rejection in words: 'cannot be read' and 'readable but not on the list' read differently", async () => {
    await upload(backend({ reject: { code: "pdf_parser_warning", message: "document rejected: pdf_parser_warning (x)" } }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("讀得開"));
    expect(screen.getByRole("alert").textContent).toContain("無害清單");
    expect(screen.getByRole("alert").textContent).toContain("pdf_parser_warning");        // the server's own message stays visible
  });

  it("tells the owner at the plan that a scan has no text to ask about, and lists usable pages in a bounded way", async () => {
    await upload(backend({ flags: ["no_text_layer"], usable: 834, noText: true }));
    await waitFor(() => expect(screen.getByTestId("upload-result")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "開始解析" }));
    await waitFor(() => expect(screen.getByTestId("step3-status").textContent).toBe("succeeded"));
    fireEvent.change(screen.getByLabelText(/Benchmark Logical ID/i), { target: { value: "b" } });
    fireEvent.click(screen.getByRole("button", { name: "建立 Benchmark 與素材計畫" }));
    await waitFor(() => expect(screen.getByTestId("plan-no-text")).toBeTruthy());
    expect(screen.getByTestId("plan-no-text").textContent).toContain("沒有文字層");
    expect(screen.getByTestId("plan-no-text").textContent).toContain("不做 OCR");
    const pages = screen.getByTestId("plan-usable-pages").textContent ?? "";
    expect(pages).toContain("共 834 頁");
    expect(pages.length).toBeLessThan(120);                                               // not 834 numbers
  }, 20000);

  it("a normal plan has no no-text notice", async () => {
    await upload(backend({}));
    await waitFor(() => expect(screen.getByTestId("upload-result")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "開始解析" }));
    await waitFor(() => expect(screen.getByTestId("step3-status").textContent).toBe("succeeded"));
    fireEvent.change(screen.getByLabelText(/Benchmark Logical ID/i), { target: { value: "b" } });
    fireEvent.click(screen.getByRole("button", { name: "建立 Benchmark 與素材計畫" }));
    await waitFor(() => expect(screen.getByText("素材計畫建立成功！")).toBeTruthy());
    expect(screen.queryByTestId("plan-no-text")).toBeNull();
  }, 20000);
});
