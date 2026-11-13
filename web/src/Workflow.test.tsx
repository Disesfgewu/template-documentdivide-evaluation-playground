import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Workflow } from "./Workflow";
import { App } from "./App";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
  sessionStorage.clear();
});

const VALID_UUID_1 = "11111111-1111-1111-1111-111111111111";
const VALID_UUID_2 = "22222222-2222-2222-2222-222222222222";
const VALID_UUID_3 = "33333333-3333-3333-3333-333333333333";
const VALID_UUID_4 = "44444444-4444-4444-4444-444444444444";
const VALID_SHA256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
const MOCK_TOKEN = `lab_act_${VALID_UUID_1}_${VALID_SHA256}`;

function createMockPdf(name = "workflow_test.pdf"): File {
  return new File(["%PDF-1.4 dummy binary content"], name, { type: "application/pdf" });
}

describe("Workflow Component", () => {
  it("未填 token 時按鈕停用 (buttons are disabled when token is not filled)", () => {
    render(<Workflow />);

    const uploadBtn = screen.getByRole("button", { name: "上傳 PDF" });
    expect((uploadBtn as HTMLButtonElement).disabled).toBe(true);

    const parseBtn = screen.getByRole("button", { name: "開始解析" });
    expect((parseBtn as HTMLButtonElement).disabled).toBe(true);

    const planBtn = screen.getByRole("button", { name: "建立 Benchmark 與素材計畫" });
    expect((planBtn as HTMLButtonElement).disabled).toBe(true);

    const genBtn = screen.getByRole("button", { name: "開始生成題目" });
    expect((genBtn as HTMLButtonElement).disabled).toBe(true);

    const valBtn = screen.getByRole("button", { name: "開始自動驗題" });
    expect((valBtn as HTMLButtonElement).disabled).toBe(true);

    const freezeBtn = screen.getByRole("button", { name: "凍結 Benchmark" });
    expect((freezeBtn as HTMLButtonElement).disabled).toBe(true);

    const baselineBtn = screen.getByRole("button", { name: "詞彙 baseline" });
    expect((baselineBtn as HTMLButtonElement).disabled).toBe(true);

    const methodBtn = screen.getByRole("button", { name: "模型回答方法" });
    expect((methodBtn as HTMLButtonElement).disabled).toBe(true);
  });

  it("上傳成功後顯示 document id (document id displayed after successful upload)", async () => {
    const mockFetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const urlStr = String(url);
      if (urlStr.includes("/api/v1/documents?") && init?.method === "POST") {
        return new Response(
          JSON.stringify({
            id: VALID_UUID_2,
            document_version_id: VALID_UUID_2,
            project_id: VALID_UUID_1,
            resource_id: VALID_UUID_3,
            artifact_id: VALID_UUID_4,
            logical_id: "demo-doc",
            revision: 1,
            sha256: VALID_SHA256,
            size_bytes: 4096,
            page_count: 6,
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({ error: { message: "not found" } }), { status: 404 });
    });

    render(<Workflow fetchFn={mockFetch as unknown as typeof fetch} />);

    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: MOCK_TOKEN } });
    fireEvent.change(screen.getByLabelText(/Project ID/i), { target: { value: VALID_UUID_1 } });

    const file = createMockPdf();
    fireEvent.change(screen.getByLabelText(/選擇 PDF 檔案/i), { target: { files: [file] } });

    const uploadBtn = screen.getByRole("button", { name: "上傳 PDF" });
    expect((uploadBtn as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(uploadBtn);

    await waitFor(() => {
      expect(screen.getByTestId("upload-result")).toBeTruthy();
    });

    const docIdEl = screen.getByTestId("step2-doc-id");
    expect(docIdEl.textContent).toBe(VALID_UUID_2);

    const pageCountEl = screen.getByTestId("step2-page-count");
    expect(pageCountEl.textContent).toBe("6");
  });

  it("pending metric 顯示為「尚未評分」而非 0 (pending metric is labeled as 尚未評分 instead of 0)", async () => {
    const benchId = "bench-pending-test";
    const runId = VALID_UUID_3;

    const mockFetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const urlStr = String(url);
      const method = init?.method || "GET";

      if (urlStr.includes("/api/v1/documents?") && method === "POST") {
        return new Response(
          JSON.stringify({
            id: VALID_UUID_2,
            document_version_id: VALID_UUID_2,
            project_id: VALID_UUID_1,
            resource_id: VALID_UUID_3,
            artifact_id: VALID_UUID_4,
            logical_id: "demo-doc",
            revision: 1,
            sha256: VALID_SHA256,
            size_bytes: 4096,
            page_count: 6,
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }

      if (urlStr.includes("/parse") && method === "POST") {
        return new Response(
          JSON.stringify({
            job_id: VALID_UUID_4,
            status: "queued",
          }),
          { status: 202, headers: { "Content-Type": "application/json" } },
        );
      }

      if (urlStr.includes(`/api/v1/jobs/${VALID_UUID_4}`)) {
        return new Response(
          JSON.stringify({
            id: VALID_UUID_4,
            project_id: VALID_UUID_1,
            status: "succeeded",
            stage: "done",
            attempt: 1,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }

      if (urlStr.includes("/api/v1/benchmarks?") && method === "POST") {
        return new Response(
          JSON.stringify({
            benchmark_revision_id: VALID_UUID_1,
            logical_id: benchId,
            revision: 1,
            status: "parsing",
            document_version_id: VALID_UUID_2,
            n_target: 20,
            q_min: 5,
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }

      if (urlStr.includes("/plan") && method === "POST") {
        return new Response(
          JSON.stringify({
            benchmark_revision_id: VALID_UUID_1,
            logical_id: benchId,
            revision: 1,
            status: "planning",
            parse_job_id: VALID_UUID_4,
            pages: { usable: [0, 1, 5], insufficient_material: {} },
            compliance_projection: "partial",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }

      if (urlStr.includes("/generate") && method === "POST") {
        return new Response(
          JSON.stringify({
            job_id: VALID_UUID_2,
            status: "queued",
            phase: "generate",
          }),
          { status: 202, headers: { "Content-Type": "application/json" } },
        );
      }

      if (urlStr.includes("/generate") && method === "GET") {
        return new Response(
          JSON.stringify({
            benchmark_revision_id: VALID_UUID_1,
            logical_id: benchId,
            revision: 1,
            benchmark_status: "validating",
            general_target: 5,
            cross_page_target: 2,
            confusable_target: 2,
            questions: 6,
            by_bucket: { general: 4, cross_page: 1, confusable: 1 },
            shortfall: 3,
            job: { status: "succeeded", phase: "validate" },
            validation: { pass: 6, fail: 0, needs_review: 0, unvalidated: 0 },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }

      if (urlStr.includes("/validate") && method === "POST") {
        return new Response(
          JSON.stringify({
            job_id: VALID_UUID_3,
            status: "queued",
            phase: "validate",
          }),
          { status: 202, headers: { "Content-Type": "application/json" } },
        );
      }

      if (urlStr.includes("/freeze") && method === "POST") {
        return new Response(
          JSON.stringify({
            benchmark_revision_id: VALID_UUID_1,
            logical_id: benchId,
            revision: 1,
            status: "frozen",
            compliance: "partial",
            limits: ["Compliance is PARTIAL"],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }

      if (urlStr.includes("/baseline-run") && method === "POST") {
        return new Response(
          JSON.stringify({
            run_id: runId,
            evaluation_id: VALID_UUID_4,
            method: "local-lexical-baseline",
            status: "completed",
            evaluation_status: "provisional",
            questions: 6,
            limits: [],
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }

      if (urlStr.includes(`/api/v1/runs/${runId}/results`)) {
        return new Response(
          JSON.stringify({
            run_id: runId,
            evaluation_id: VALID_UUID_4,
            grader_revision: "rule-based-scorer-1",
            evaluation_status: "provisional",
            metrics: [
              {
                metric_name: "recall_at_k",
                scored: 6,
                not_applicable: 0,
                pending: 0,
                error: 0,
                mean: 0.8333,
              },
              {
                metric_name: "correctness",
                scored: 0,
                not_applicable: 0,
                pending: 6,
                error: 0,
                mean: null,
              },
            ],
            submissions: { answered: 6, abstained_by_model: 0, method_failures: {} },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }

      return new Response(JSON.stringify({ error: { message: "not found" } }), { status: 404 });
    });

    render(<Workflow fetchFn={mockFetch as unknown as typeof fetch} />);

    // Auth
    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: MOCK_TOKEN } });
    fireEvent.change(screen.getByLabelText(/Project ID/i), { target: { value: VALID_UUID_1 } });

    // Step 2: Upload
    fireEvent.change(screen.getByLabelText(/選擇 PDF 檔案/i), { target: { files: [createMockPdf()] } });
    fireEvent.click(screen.getByRole("button", { name: "上傳 PDF" }));
    await waitFor(() => expect(screen.getByTestId("upload-result")).toBeTruthy());

    // Step 3: Parse
    fireEvent.click(screen.getByRole("button", { name: "開始解析" }));
    await waitFor(() => expect(screen.getByTestId("step3-status").textContent).toBe("succeeded"));

    // Step 4: Plan
    fireEvent.change(screen.getByLabelText(/Benchmark Logical ID/i), { target: { value: benchId } });
    fireEvent.click(screen.getByRole("button", { name: "建立 Benchmark 與素材計畫" }));
    await waitFor(() => expect(screen.getByText("素材計畫建立成功！")).toBeTruthy());

    // Step 5: Generate
    fireEvent.click(screen.getByRole("button", { name: "開始生成題目" }));
    await waitFor(() => expect(screen.getByText("題目生成完成！")).toBeTruthy());

    // Step 6: Validate
    fireEvent.click(screen.getByRole("button", { name: "開始自動驗題" }));
    await waitFor(() => expect(screen.getByText("驗證結果題數統計：")).toBeTruthy());

    // Step 7: Freeze
    fireEvent.click(screen.getByRole("button", { name: "凍結 Benchmark" }));
    await waitFor(() => expect(screen.getByText(/Benchmark 成功凍結/i)).toBeTruthy());

    // Step 8: Baseline run
    fireEvent.click(screen.getByRole("button", { name: "詞彙 baseline" }));

    // Step 9: Results table
    await waitFor(() => {
      expect(screen.getByTestId("results-table")).toBeTruthy();
    });

    const table = screen.getByTestId("results-table");
    expect(table.textContent).toContain("correctness");
    expect(table.textContent).toContain("recall_at_k");

    // Check correctness row
    const rows = table.querySelectorAll("tbody tr");
    let correctnessRowText = "";
    rows.forEach((row) => {
      if (row.textContent?.includes("correctness")) {
        correctnessRowText = row.textContent;
      }
    });

    // Verification: correctness metric shows "尚未評分" and mean is NOT 0
    expect(correctnessRowText).toContain("尚未評分");
    expect(correctnessRowText).toContain("尚未評分（judge 未執行）");
    // Ensure mean column does NOT claim 0 or 0.00
    expect(correctnessRowText).not.toMatch(/\b0\.00\b/);
  });

  it("409 凍結訊息被顯示 (409 freeze message is displayed)", async () => {
    const benchId = "bench-409-test";
    const honestMessage =
      "every question needs a passing automated validation before the freeze; verdicts: {'pass': 4, 'fail': 2, 'needs_review': 0, 'unvalidated': 0}. Nothing is excluded or deleted: resolve them (validate again) first.";

    const mockFetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const urlStr = String(url);
      const method = init?.method || "GET";

      if (urlStr.includes("/api/v1/documents?") && method === "POST") {
        return new Response(
          JSON.stringify({
            id: VALID_UUID_2,
            document_version_id: VALID_UUID_2,
            project_id: VALID_UUID_1,
            resource_id: VALID_UUID_3,
            artifact_id: VALID_UUID_4,
            logical_id: "demo-doc",
            revision: 1,
            sha256: VALID_SHA256,
            size_bytes: 4096,
            page_count: 6,
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }

      if (urlStr.includes("/parse") && method === "POST") {
        return new Response(
          JSON.stringify({ job_id: VALID_UUID_4, status: "queued" }),
          { status: 202, headers: { "Content-Type": "application/json" } },
        );
      }

      if (urlStr.includes(`/api/v1/jobs/${VALID_UUID_4}`)) {
        return new Response(
          JSON.stringify({ id: VALID_UUID_4, status: "succeeded", stage: "done", attempt: 1 }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }

      if (urlStr.includes("/api/v1/benchmarks?") && method === "POST") {
        return new Response(
          JSON.stringify({
            benchmark_revision_id: VALID_UUID_1,
            logical_id: benchId,
            revision: 1,
            status: "parsing",
            document_version_id: VALID_UUID_2,
            n_target: 20,
            q_min: 5,
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }

      if (urlStr.includes("/plan") && method === "POST") {
        return new Response(
          JSON.stringify({
            benchmark_revision_id: VALID_UUID_1,
            logical_id: benchId,
            revision: 1,
            status: "planning",
            parse_job_id: VALID_UUID_4,
            pages: { usable: [0, 1, 5], insufficient_material: {} },
            compliance_projection: "partial",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }

      if (urlStr.includes("/generate") && method === "POST") {
        return new Response(
          JSON.stringify({ job_id: VALID_UUID_2, status: "queued", phase: "generate" }),
          { status: 202, headers: { "Content-Type": "application/json" } },
        );
      }

      if (urlStr.includes("/generate") && method === "GET") {
        return new Response(
          JSON.stringify({
            benchmark_revision_id: VALID_UUID_1,
            logical_id: benchId,
            revision: 1,
            benchmark_status: "validating",
            questions: 6,
            by_bucket: { general: 4, cross_page: 1, confusable: 1 },
            shortfall: 3,
            job: { status: "succeeded", phase: "validate" },
            validation: { pass: 4, fail: 2, needs_review: 0, unvalidated: 0 },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }

      if (urlStr.includes("/validate") && method === "POST") {
        return new Response(
          JSON.stringify({ job_id: VALID_UUID_3, status: "queued", phase: "validate" }),
          { status: 202, headers: { "Content-Type": "application/json" } },
        );
      }

      // Freeze returns 409 Conflict with honest explanation
      if (urlStr.includes("/freeze") && method === "POST") {
        return new Response(
          JSON.stringify({
            error: {
              code: "conflict",
              message: honestMessage,
            },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      }

      return new Response(JSON.stringify({ error: { message: "not found" } }), { status: 404 });
    });

    render(<Workflow fetchFn={mockFetch as unknown as typeof fetch} />);

    // Steps 1 to 6
    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: MOCK_TOKEN } });
    fireEvent.change(screen.getByLabelText(/Project ID/i), { target: { value: VALID_UUID_1 } });

    fireEvent.change(screen.getByLabelText(/選擇 PDF 檔案/i), { target: { files: [createMockPdf()] } });
    fireEvent.click(screen.getByRole("button", { name: "上傳 PDF" }));
    await waitFor(() => expect(screen.getByTestId("upload-result")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "開始解析" }));
    await waitFor(() => expect(screen.getByTestId("step3-status").textContent).toBe("succeeded"));

    fireEvent.change(screen.getByLabelText(/Benchmark Logical ID/i), { target: { value: benchId } });
    fireEvent.click(screen.getByRole("button", { name: "建立 Benchmark 與素材計畫" }));
    await waitFor(() => expect(screen.getByText("素材計畫建立成功！")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "開始生成題目" }));
    await waitFor(() => expect(screen.getByText("題目生成完成！")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "開始自動驗題" }));
    await waitFor(() => expect(screen.getByText("驗證結果題數統計：")).toBeTruthy());

    // Step 7: Freeze
    fireEvent.click(screen.getByRole("button", { name: "凍結 Benchmark" }));

    // Verify 409 message is displayed honestly
    await waitFor(() => {
      expect(screen.getByTestId("freeze-409-message")).toBeTruthy();
    });

    const msgEl = screen.getByTestId("freeze-409-message");
    expect(msgEl.textContent).toContain(honestMessage);
    expect(msgEl.textContent).toContain("409 誠實結果");

    // Baseline button remains disabled because freeze was not completed
    const baselineBtn = screen.getByRole("button", { name: "詞彙 baseline" });
    expect((baselineBtn as HTMLButtonElement).disabled).toBe(true);
  });

  it("錯誤訊息不含 token (error messages do not contain token)", async () => {
    const SECRET_TOKEN = "lab_act_very_secret_token_value_99999";
    const mockFetch = vi.fn(async () => {
      return new Response(
        JSON.stringify({
          error: {
            code: "internal_error",
            message: `Database failure while executing for ${SECRET_TOKEN} in session`,
          },
        }),
        { status: 500, headers: { "Content-Type": "application/json" } },
      );
    });

    render(<Workflow fetchFn={mockFetch as unknown as typeof fetch} />);

    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: SECRET_TOKEN } });
    fireEvent.change(screen.getByLabelText(/Project ID/i), { target: { value: VALID_UUID_1 } });

    fireEvent.change(screen.getByLabelText(/選擇 PDF 檔案/i), { target: { files: [createMockPdf()] } });

    fireEvent.click(screen.getByRole("button", { name: "上傳 PDF" }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeTruthy();
    });

    const alertText = screen.getByRole("alert").textContent || "";
    expect(alertText).toContain("[REDACTED]");
    expect(alertText).not.toContain(SECRET_TOKEN);
    expect(document.body.textContent).not.toContain(SECRET_TOKEN);

    // Ensure token is not written to localStorage or sessionStorage
    expect(JSON.stringify({ ...localStorage })).not.toContain(SECRET_TOKEN);
    expect(JSON.stringify({ ...sessionStorage })).not.toContain(SECRET_TOKEN);
  });

  it("所有會花錢的步驟皆有『會呼叫真實模型、會花錢』警示標記", () => {
    render(<Workflow />);
    const warnings = screen.getAllByText("會呼叫真實模型、會花錢");
    // Step 5 (Generate), Step 6 (Validate), Step 8 (Method Run)
    expect(warnings.length).toBe(3);
  });

  it("App 支援分頁切換 (App switches between the workflow and operator tabs)", () => {
    render(<App />);
    expect(screen.getByRole("region", { name: "工作流程頁面" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "文件操作員" }));
    expect(screen.getByRole("region", { name: "文件操作員介面" })).toBeTruthy();
    expect(screen.queryByRole("region", { name: "工作流程頁面" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "工作流程" }));
    expect(screen.getByRole("region", { name: "工作流程頁面" })).toBeTruthy();
  });

  it("進度列與「現在該做」提示：一開始指向登入，填好 Token 與 Project ID 後指向上傳", () => {
    render(<Workflow />);
    expect(screen.getByTestId("next-hint").textContent).toContain("貼上 Token 與 Project ID");
    expect(screen.getByRole("list", { name: "進度" }).querySelectorAll("li").length).toBe(9);
    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: MOCK_TOKEN } });
    fireEvent.change(screen.getByLabelText(/Project ID/i), { target: { value: VALID_UUID_1 } });
    expect(screen.getByTestId("next-hint").textContent).toContain("上傳");
    expect(screen.getByRole("list", { name: "進度" }).querySelector('[aria-current="step"]')?.textContent).toContain("上傳");
  });
});

describe("Workflow local auto-login", () => {
  it("hides the token fields and enables upload when a dev session is provided", () => {
    render(<Workflow fetchFn={vi.fn()} devToken="lab_act_dev" devProjectId="11111111-1111-4111-8111-111111111111" />);
    expect(screen.getByTestId("workflow-autologin").textContent).toContain("11111111-1111-4111-8111-111111111111");
    expect(screen.queryByLabelText(/認證 Token/)).toBeNull();
  });

  it("asks for credentials when no dev session is provided", () => {
    render(<Workflow fetchFn={vi.fn()} devToken="" devProjectId="" />);
    expect(screen.queryByTestId("workflow-autologin")).toBeNull();
    expect(screen.getByLabelText(/認證 Token/)).toBeTruthy();
  });
});
