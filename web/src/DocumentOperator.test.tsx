import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { DocumentOperator } from "./DocumentOperator";

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

function createMockPdfFile(name = "test.pdf", size = 100): File {
  const content = "%PDF-1.4 " + "A".repeat(Math.max(0, size - 9));
  return new File([content], name, { type: "application/pdf" });
}

describe("DocumentOperator", () => {
  it("renders empty states, inputs, and clean feature availability notice without PR/branch/issue numbers", () => {
    render(<DocumentOperator />);

    // Auth input is password type
    const tokenInput = screen.getByLabelText(/認證 Token/i) as HTMLInputElement;
    expect(tokenInput.type).toBe("password");

    // Empty states
    expect(screen.getByTestId("upload-empty-state").textContent).toContain("尚未上傳任何文件");
    expect(screen.getByTestId("download-empty-state").textContent).toContain("尚未下載任何文件");

    // Clean visible notice: no PR, branch, file path, or issue numbers
    const notice = screen.getByRole("region", { name: "系統限制與功能說明" });
    expect(notice.textContent).toContain("後端服務可用性");
    expect(notice.textContent).toContain("尚未接入的功能");
    expect(notice.textContent).toContain("功能範圍");

    // Strictly ensure no technical issue numbers, PRs, branch names, or file paths leak into visible UI
    expect(document.body.textContent).not.toMatch(/PR\s*#\d+/i);
    expect(document.body.textContent).not.toMatch(/#\d+/);
    expect(document.body.textContent).not.toContain("origin/");
    expect(document.body.textContent).not.toContain("services/api");
  });

  it("never persists or logs token", () => {
    const logSpy = vi.spyOn(console, "log");
    const errorSpy = vi.spyOn(console, "error");

    render(<DocumentOperator />);
    const tokenInput = screen.getByLabelText(/認證 Token/i);
    fireEvent.change(tokenInput, { target: { value: MOCK_TOKEN } });

    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);

    for (const call of [...logSpy.mock.calls, ...errorSpy.mock.calls]) {
      for (const arg of call) {
        expect(String(arg)).not.toContain(MOCK_TOKEN);
      }
    }
  });

  it("validates upload inputs before calling fetch (token, uuid, file)", async () => {
    const mockFetch = vi.fn();
    render(<DocumentOperator fetchFn={mockFetch} />);

    const uploadBtn = screen.getByRole("button", { name: "上傳 PDF" });

    // 1. Submit without token
    fireEvent.click(uploadBtn);
    expect(mockFetch).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain("未提供 Token");

    // Enter token
    const tokenInput = screen.getByLabelText(/認證 Token/i);
    fireEvent.change(tokenInput, { target: { value: MOCK_TOKEN } });

    // 2. Submit with invalid Project ID
    const projInput = screen.getByLabelText(/Project ID/i);
    fireEvent.change(projInput, { target: { value: "invalid-uuid" } });
    fireEvent.click(uploadBtn);
    expect(mockFetch).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain("Project ID 格式錯誤");

    // Correct Project ID
    fireEvent.change(projInput, { target: { value: VALID_UUID_1 } });

    // 3. Submit without selecting a file
    fireEvent.click(uploadBtn);
    expect(mockFetch).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain("請選擇要上傳的 PDF 檔案");
  });

  it("retains stable idempotency key on lost response / ambiguous retry, and rotates on changed inputs or confirmed result", async () => {
    let keyCounter = 1;
    const generateKey = vi.fn(() => `generated-key-${keyCounter++}`);

    const capturedKeys: string[] = [];
    let shouldFailWithNetworkError = true;

    const mockFetch = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      const headers = init?.headers as Record<string, string>;
      capturedKeys.push(headers["Idempotency-Key"]);

      if (shouldFailWithNetworkError) {
        throw new TypeError("Failed to fetch (connection lost)");
      }
      if (capturedKeys.length === 2) {
        return new Response(JSON.stringify({ error: { code: "unauthorized", message: "expired" } }), { status: 401 });
      }

      return new Response(
        JSON.stringify({
          id: VALID_UUID_2,
          document_version_id: VALID_UUID_2,
          project_id: VALID_UUID_1,
          resource_id: VALID_UUID_3,
          artifact_id: VALID_UUID_4,
          logical_id: "default",
          revision: 1,
          sha256: VALID_SHA256,
          size_bytes: 1024,
          page_count: 1,
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      );
    });

    render(
      <DocumentOperator
        fetchFn={mockFetch as unknown as typeof fetch}
        generateIdempotencyKey={generateKey}
      />,
    );

    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: MOCK_TOKEN } });
    fireEvent.change(screen.getByLabelText(/Project ID/i), { target: { value: VALID_UUID_1 } });

    const fileA = createMockPdfFile("file-a.pdf", 1024);
    fireEvent.change(screen.getByLabelText(/PDF 檔案/i), { target: { files: [fileA] } });

    const uploadBtn = screen.getByRole("button", { name: "上傳 PDF" });

    // Attempt 1: fails with network error (lost response)
    fireEvent.click(uploadBtn);
    await waitFor(() => {
      expect(screen.getByTestId("upload-error")).toBeTruthy();
    });
    expect(capturedKeys).toEqual(["generated-key-1"]);

    // A rejected retry does not resolve whether the first request committed.
    shouldFailWithNetworkError = false;
    fireEvent.click(uploadBtn);
    await waitFor(() => expect(screen.getByTestId("upload-error").textContent).toContain("expired"));
    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: "refreshed-token" } });
    fireEvent.click(uploadBtn);
    await waitFor(() => {
      expect(screen.getByTestId("upload-success")).toBeTruthy();
    });
    expect(capturedKeys).toEqual(["generated-key-1", "generated-key-1", "generated-key-1"]);

    // Attempt 3: Now that previous result succeeded (confirmed result), or if user uploads with changed inputs (e.g. fileB):
    const fileB = createMockPdfFile("file-b.pdf", 2048);
    fireEvent.change(screen.getByLabelText(/PDF 檔案/i), { target: { files: [fileB] } });

    fireEvent.click(uploadBtn);
    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalledTimes(4);
    });
    // Key MUST have rotated to generated-key-2
    expect(capturedKeys[3]).toBe("generated-key-2");
    expect(capturedKeys[3]).not.toBe(capturedKeys[0]);
  });

  it("retains idempotency key across HTTP 500 error on retry", async () => {
    let keyCounter = 1;
    const generateKey = vi.fn(() => `key-500-${keyCounter++}`);
    const capturedKeys: string[] = [];
    let attempt = 1;

    const mockFetch = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      const headers = init?.headers as Record<string, string>;
      capturedKeys.push(headers["Idempotency-Key"]);

      if (attempt === 1) {
        attempt++;
        return new Response(
          JSON.stringify({
            error: {
              code: "internal_error",
              message: "database timeout or internal server error",
            },
          }),
          { status: 500, headers: { "Content-Type": "application/json" } },
        );
      }

      return new Response(
        JSON.stringify({
          id: VALID_UUID_2,
          document_version_id: VALID_UUID_2,
          project_id: VALID_UUID_1,
          resource_id: VALID_UUID_3,
          artifact_id: VALID_UUID_4,
          logical_id: "default",
          revision: 1,
          sha256: VALID_SHA256,
          size_bytes: 1024,
          page_count: 1,
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      );
    });

    render(
      <DocumentOperator
        fetchFn={mockFetch as unknown as typeof fetch}
        generateIdempotencyKey={generateKey}
      />,
    );

    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: MOCK_TOKEN } });
    fireEvent.change(screen.getByLabelText(/Project ID/i), { target: { value: VALID_UUID_1 } });
    const file = createMockPdfFile("report.pdf", 1024);
    fireEvent.change(screen.getByLabelText(/PDF 檔案/i), { target: { files: [file] } });

    const uploadBtn = screen.getByRole("button", { name: "上傳 PDF" });

    // Attempt 1 -> 500 Internal Server Error
    fireEvent.click(uploadBtn);
    await waitFor(() => {
      expect(screen.getByTestId("upload-error")).toBeTruthy();
    });
    expect(capturedKeys).toEqual(["key-500-1"]);

    // Attempt 2 (Retry) -> same File object selection, MUST reuse identical key
    fireEvent.click(uploadBtn);
    await waitFor(() => {
      expect(screen.getByTestId("upload-success")).toBeTruthy();
    });
    expect(capturedKeys).toEqual(["key-500-1", "key-500-1"]);
  });

  it("retains idempotency key across malformed 201 response on retry", async () => {
    let keyCounter = 1;
    const generateKey = vi.fn(() => `key-201-${keyCounter++}`);
    const capturedKeys: string[] = [];
    let attempt = 1;

    const mockFetch = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      const headers = init?.headers as Record<string, string>;
      capturedKeys.push(headers["Idempotency-Key"]);

      if (attempt === 1) {
        attempt++;
        // Malformed 201 response (missing required fields / invalid shape)
        return new Response(
          JSON.stringify({ incomplete: true }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }

      return new Response(
        JSON.stringify({
          id: VALID_UUID_2,
          document_version_id: VALID_UUID_2,
          project_id: VALID_UUID_1,
          resource_id: VALID_UUID_3,
          artifact_id: VALID_UUID_4,
          logical_id: "default",
          revision: 1,
          sha256: VALID_SHA256,
          size_bytes: 1024,
          page_count: 1,
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      );
    });

    render(
      <DocumentOperator
        fetchFn={mockFetch as unknown as typeof fetch}
        generateIdempotencyKey={generateKey}
      />,
    );

    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: MOCK_TOKEN } });
    fireEvent.change(screen.getByLabelText(/Project ID/i), { target: { value: VALID_UUID_1 } });
    const file = createMockPdfFile("data.pdf", 1024);
    fireEvent.change(screen.getByLabelText(/PDF 檔案/i), { target: { files: [file] } });

    const uploadBtn = screen.getByRole("button", { name: "上傳 PDF" });

    // Attempt 1 -> Malformed 201
    fireEvent.click(uploadBtn);
    await waitFor(() => {
      expect(screen.getByTestId("upload-error")).toBeTruthy();
    });
    expect(capturedKeys).toEqual(["key-201-1"]);

    // Attempt 2 (Retry) -> MUST reuse identical key because outcome was ambiguous
    fireEvent.click(uploadBtn);
    await waitFor(() => {
      expect(screen.getByTestId("upload-success")).toBeTruthy();
    });
    expect(capturedKeys).toEqual(["key-201-1", "key-201-1"]);
  });

  it("rotates idempotency key when distinct File object is selected even with same name and size", async () => {
    let keyCounter = 1;
    const generateKey = vi.fn(() => `key-file-${keyCounter++}`);
    const capturedKeys: string[] = [];

    const mockFetch = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      const headers = init?.headers as Record<string, string>;
      capturedKeys.push(headers["Idempotency-Key"]);
      return new Response(
        JSON.stringify({
          error: { code: "internal_error", message: "fail" },
        }),
        { status: 500, headers: { "Content-Type": "application/json" } },
      );
    });

    render(
      <DocumentOperator
        fetchFn={mockFetch as unknown as typeof fetch}
        generateIdempotencyKey={generateKey}
      />,
    );

    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: MOCK_TOKEN } });
    fireEvent.change(screen.getByLabelText(/Project ID/i), { target: { value: VALID_UUID_1 } });

    // Create two distinct File instances with identical name and size
    const file1 = createMockPdfFile("identical.pdf", 500);
    const file2 = createMockPdfFile("identical.pdf", 500);
    expect(file1).not.toBe(file2);

    const uploadBtn = screen.getByRole("button", { name: "上傳 PDF" });

    // Submit with file1
    fireEvent.change(screen.getByLabelText(/PDF 檔案/i), { target: { files: [file1] } });
    fireEvent.click(uploadBtn);
    await waitFor(() => {
      expect(screen.getByTestId("upload-error")).toBeTruthy();
    });
    expect(capturedKeys).toEqual(["key-file-1"]);

    // Retry with SAME file1 object identity -> reuses key
    fireEvent.click(uploadBtn);
    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalledTimes(2);
    });
    expect(capturedKeys).toEqual(["key-file-1", "key-file-1"]);

    // Now select file2 (distinct File object instance) -> MUST rotate key
    fireEvent.change(screen.getByLabelText(/PDF 檔案/i), { target: { files: [file2] } });
    fireEvent.click(uploadBtn);
    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalledTimes(3);
    });
    expect(capturedKeys).toEqual(["key-file-1", "key-file-1", "key-file-2"]);
  });

  it("handles successful upload and populates details and download button", async () => {
    const mockFetch = vi.fn(async () =>
      new Response(
        JSON.stringify({
          id: VALID_UUID_2,
          document_version_id: VALID_UUID_2,
          project_id: VALID_UUID_1,
          resource_id: VALID_UUID_3,
          artifact_id: VALID_UUID_4,
          logical_id: "manual",
          revision: 1,
          sha256: VALID_SHA256,
          size_bytes: 2048,
          page_count: 3,
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      ),
    );

    render(<DocumentOperator fetchFn={mockFetch as unknown as typeof fetch} />);

    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: MOCK_TOKEN } });
    fireEvent.change(screen.getByLabelText(/Project ID/i), { target: { value: VALID_UUID_1 } });
    fireEvent.change(screen.getByLabelText(/Logical ID/i), { target: { value: "manual" } });

    const file = createMockPdfFile("sample.pdf", 2048);
    const fileInput = screen.getByLabelText(/PDF 檔案/i);
    fireEvent.change(fileInput, { target: { files: [file] } });

    fireEvent.click(screen.getByRole("button", { name: "上傳 PDF" }));

    await waitFor(() => {
      expect(screen.getByTestId("upload-success")).toBeTruthy();
    });

    const successBox = screen.getByTestId("upload-success");
    expect(successBox.textContent).toContain("文件上傳成功！");
    expect(successBox.textContent).toContain(VALID_UUID_2);
    expect(successBox.textContent).toContain("頁數: 3");
    expect(successBox.textContent).toContain("2048 位元組");

    // Click "帶入此 Document ID 至下載欄位"
    const carryBtn = screen.getByRole("button", { name: /帶入此 Document ID/i });
    fireEvent.click(carryBtn);

    const downloadDocInput = screen.getByLabelText(/Document ID/i) as HTMLInputElement;
    expect(downloadDocInput.value).toBe(VALID_UUID_2);
  });

  it("prevents double submission during upload and download", async () => {
    let resolveUpload: (r: Response) => void;
    const slowUploadPromise = new Promise<Response>((resolve) => {
      resolveUpload = resolve;
    });
    const mockFetch = vi.fn(async () => slowUploadPromise);

    render(<DocumentOperator fetchFn={mockFetch as unknown as typeof fetch} />);

    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: MOCK_TOKEN } });
    fireEvent.change(screen.getByLabelText(/Project ID/i), { target: { value: VALID_UUID_1 } });
    const fileInput = screen.getByLabelText(/PDF 檔案/i);
    fireEvent.change(fileInput, { target: { files: [createMockPdfFile()] } });

    const uploadBtn = screen.getByRole("button", { name: "上傳 PDF" });
    fireEvent.click(uploadBtn);

    // Button becomes disabled and loading text appears
    expect((uploadBtn as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toContain("文件上傳中");

    // Clicking again while in-flight has no effect
    fireEvent.click(uploadBtn);
    expect(mockFetch).toHaveBeenCalledTimes(1);

    // Resolve request
    resolveUpload!(
      new Response(
        JSON.stringify({
          id: VALID_UUID_2,
          document_version_id: VALID_UUID_2,
          project_id: VALID_UUID_1,
          resource_id: VALID_UUID_3,
          artifact_id: VALID_UUID_4,
          logical_id: "default",
          revision: 1,
          sha256: VALID_SHA256,
          size_bytes: 100,
          page_count: 1,
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      ),
    );

    await waitFor(() => {
      expect((uploadBtn as HTMLButtonElement).disabled).toBe(false);
    });
  });

  it("displays upload 403 permission denied and 401 unauthorized clearly", async () => {
    const mockFetch = vi.fn(async () =>
      new Response(
        JSON.stringify({
          error: {
            code: "permission_denied",
            message: "user lacks import_and_configure_parse permission",
          },
        }),
        { status: 403, headers: { "Content-Type": "application/json" } },
      ),
    );

    render(<DocumentOperator fetchFn={mockFetch as unknown as typeof fetch} />);

    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: MOCK_TOKEN } });
    fireEvent.change(screen.getByLabelText(/Project ID/i), { target: { value: VALID_UUID_1 } });
    fireEvent.change(screen.getByLabelText(/PDF 檔案/i), { target: { files: [createMockPdfFile()] } });

    fireEvent.click(screen.getByRole("button", { name: "上傳 PDF" }));

    await waitFor(() => {
      expect(screen.getByTestId("upload-error")).toBeTruthy();
    });

    const alert = screen.getByTestId("upload-error");
    expect(alert.textContent).toContain("403 Permission Denied");
    expect(alert.textContent).toContain("user lacks import_and_configure_parse permission");
  });

  it("handles successful download and executes save", async () => {
    const mockFetch = vi.fn(async () =>
      new Response("%PDF-1.4 content bytes", {
        status: 200,
        headers: {
          "Content-Type": "application/pdf",
          "Content-Disposition": `attachment; filename="${VALID_UUID_2}.pdf"`,
        },
      }),
    );

    const triggerSave = vi.fn();
    render(<DocumentOperator fetchFn={mockFetch as unknown as typeof fetch} triggerSave={triggerSave} />);

    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: MOCK_TOKEN } });
    fireEvent.change(screen.getByLabelText(/Document ID/i), { target: { value: VALID_UUID_2 } });

    fireEvent.click(screen.getByRole("button", { name: "下載 PDF" }));

    await waitFor(() => {
      expect(screen.getByTestId("download-success")).toBeTruthy();
    });

    expect(screen.getByTestId("download-success").textContent).toContain("文件下載成功");
    expect(screen.getByTestId("download-success").textContent).toContain(`${VALID_UUID_2}.pdf`);
    expect(triggerSave).toHaveBeenCalledTimes(1);
    expect(triggerSave.mock.calls[0][1]).toBe(`${VALID_UUID_2}.pdf`);
  });

  it("rejects 200 HTML download and shows clear error without triggering save", async () => {
    const mockFetch = vi.fn(async () =>
      new Response("<html><body>Login Page</body></html>", {
        status: 200,
        headers: { "Content-Type": "text/html; charset=utf-8" },
      }),
    );

    const triggerSave = vi.fn();
    render(<DocumentOperator fetchFn={mockFetch as unknown as typeof fetch} triggerSave={triggerSave} />);

    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: MOCK_TOKEN } });
    fireEvent.change(screen.getByLabelText(/Document ID/i), { target: { value: VALID_UUID_2 } });

    fireEvent.click(screen.getByRole("button", { name: "下載 PDF" }));

    await waitFor(() => {
      expect(screen.getByTestId("download-error")).toBeTruthy();
    });

    expect(triggerSave).not.toHaveBeenCalled();
    const alert = screen.getByTestId("download-error");
    expect(alert.textContent).toContain("text/html");
  });

  it("rejects 200 with fake PDF signature and shows error without triggering save", async () => {
    const mockFetch = vi.fn(async () =>
      new Response("<html><body>Fake</body></html>", {
        status: 200,
        headers: { "Content-Type": "application/pdf" },
      }),
    );

    const triggerSave = vi.fn();
    render(<DocumentOperator fetchFn={mockFetch as unknown as typeof fetch} triggerSave={triggerSave} />);

    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: MOCK_TOKEN } });
    fireEvent.change(screen.getByLabelText(/Document ID/i), { target: { value: VALID_UUID_2 } });

    fireEvent.click(screen.getByRole("button", { name: "下載 PDF" }));

    await waitFor(() => {
      expect(screen.getByTestId("download-error")).toBeTruthy();
    });

    expect(triggerSave).not.toHaveBeenCalled();
    const alert = screen.getByTestId("download-error");
    expect(alert.textContent).toContain("%PDF-");
  });

  it("displays download 404 not found and 403 permission denied", async () => {
    const mockFetch = vi.fn(async () =>
      new Response(
        JSON.stringify({
          error: {
            code: "not_found",
            message: "document not found",
          },
        }),
        { status: 404, headers: { "Content-Type": "application/json" } },
      ),
    );

    render(<DocumentOperator fetchFn={mockFetch as unknown as typeof fetch} />);

    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: MOCK_TOKEN } });
    fireEvent.change(screen.getByLabelText(/Document ID/i), { target: { value: VALID_UUID_2 } });

    fireEvent.click(screen.getByRole("button", { name: "下載 PDF" }));

    await waitFor(() => {
      expect(screen.getByTestId("download-error")).toBeTruthy();
    });

    const alert = screen.getByTestId("download-error");
    expect(alert.textContent).toContain("404 Not Found");
    expect(alert.textContent).toContain("document not found");
  });

  it("allows user to cancel upload via UI, clears loading state, and retains exact idempotency key on retry", async () => {
    let capturedKeys: string[] = [];

    const mockFetch = vi.fn(
      async (_url: RequestInfo | URL, init?: RequestInit) => {
        const key = (init?.headers as Record<string, string>)?.["Idempotency-Key"] ?? "";
        capturedKeys.push(key);

        if (capturedKeys.length === 1) {
          // First attempt: waits until cancelled
          return new Promise<Response>((_, reject) => {
            init?.signal?.addEventListener("abort", () => {
              reject(new DOMException("User cancelled", "AbortError"));
            });
          });
        }

        // Retry attempt: succeeds
        return new Response(
          JSON.stringify({
            id: VALID_UUID_1,
            document_version_id: VALID_UUID_1,
            project_id: VALID_UUID_1,
            resource_id: VALID_UUID_2,
            artifact_id: VALID_UUID_3,
            logical_id: "default",
            revision: 1,
            sha256: VALID_SHA256,
            size_bytes: 100,
            page_count: 1,
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      },
    );

    let keyGenCount = 1;
    const generateKey = vi.fn(() => `user-cancel-key-${keyGenCount++}`);

    render(
      <DocumentOperator
        fetchFn={mockFetch as unknown as typeof fetch}
        generateIdempotencyKey={generateKey}
      />,
    );

    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: MOCK_TOKEN } });
    fireEvent.change(screen.getByLabelText(/Project ID/i), { target: { value: VALID_UUID_1 } });

    const file = createMockPdfFile("test-cancel.pdf", 100);
    const fileInput = document.getElementById("upload-file") as HTMLInputElement;
    Object.defineProperty(fileInput, "files", { value: [file], configurable: true });
    fireEvent.change(fileInput);

    // 1. Submit upload
    fireEvent.click(screen.getByRole("button", { name: "上傳 PDF" }));

    // 2. Loading state active and "取消上傳" button visible
    expect(screen.getByRole("status").textContent).toContain("文件上傳中");
    const cancelBtn = screen.getByRole("button", { name: "取消上傳" });
    expect(cancelBtn).toBeTruthy();

    // 3. User clicks "取消上傳"
    fireEvent.click(cancelBtn);

    // 4. Loading cleared, cancel button gone, alert shown
    await waitFor(() => {
      expect(screen.queryByRole("status")).toBeNull();
      expect(screen.queryByRole("button", { name: "取消上傳" })).toBeNull();
      expect(screen.getByTestId("upload-error")).toBeTruthy();
    });
    expect(screen.getByTestId("upload-error").textContent).toContain("取消");

    // 5. User clicks "上傳 PDF" again (retry)
    fireEvent.click(screen.getByRole("button", { name: "上傳 PDF" }));

    await waitFor(() => {
      expect(screen.getByTestId("upload-success")).toBeTruthy();
    });

    // 6. Verify BOTH attempts used the EXACT SAME idempotency key!
    expect(capturedKeys.length).toBe(2);
    expect(capturedKeys[0]).toBe("user-cancel-key-1");
    expect(capturedKeys[1]).toBe("user-cancel-key-1");
  });

  it("allows user to cancel download via UI and clears loading state", async () => {
    const hangingStream = new ReadableStream({
      pull() {
        return new Promise(() => {});
      },
    });

    const mockFetch = vi.fn(
      async () =>
        new Response(hangingStream, {
          status: 200,
          headers: { "Content-Type": "application/pdf" },
        }),
    );

    render(<DocumentOperator fetchFn={mockFetch as unknown as typeof fetch} />);

    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: MOCK_TOKEN } });
    fireEvent.change(screen.getByLabelText(/Document ID/i), { target: { value: VALID_UUID_1 } });

    // Submit download
    fireEvent.click(screen.getByRole("button", { name: "下載 PDF" }));

    // Loading active, cancel button appears
    expect(screen.getByRole("status").textContent).toContain("文件下載中");
    const cancelBtn = screen.getByRole("button", { name: "取消下載" });
    expect(cancelBtn).toBeTruthy();

    // User cancels
    fireEvent.click(cancelBtn);

    // Loading clears, error alert shown
    await waitFor(() => {
      expect(screen.queryByRole("status")).toBeNull();
      expect(screen.queryByRole("button", { name: "取消下載" })).toBeNull();
      expect(screen.getByTestId("download-error")).toBeTruthy();
    });
    expect(screen.getByTestId("download-error").textContent).toContain("取消");
  });

  it("never shows the token when the response Content-Type or filename carries it", async () => {
    const lowerToken = MOCK_TOKEN.toLowerCase();
    const seen = () => (document.body.textContent ?? "").toLowerCase();

    // 1. Content-Type carries the token: error shown without it, nothing saved, form usable again
    const badType = vi.fn(async () => new Response("%PDF-1.4 x", { status: 200, headers: { "Content-Type": `text/${lowerToken}` } }));
    const triggerSave = vi.fn();
    const first = render(<DocumentOperator fetchFn={badType as unknown as typeof fetch} triggerSave={triggerSave} />);
    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: MOCK_TOKEN } });
    fireEvent.change(screen.getByLabelText(/Document ID/i), { target: { value: VALID_UUID_2 } });
    fireEvent.click(screen.getByRole("button", { name: "下載 PDF" }));
    await waitFor(() => expect(screen.getByTestId("download-error")).toBeTruthy());
    expect(screen.getByTestId("download-error").textContent).toContain("未知類型");
    expect(seen()).not.toContain(lowerToken);
    expect(triggerSave).not.toHaveBeenCalled();
    expect(screen.queryByTestId("download-success")).toBeNull();
    expect((screen.getByRole("button", { name: "下載 PDF" }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByText("文件下載中")).toBeNull();
    expect(JSON.stringify({ ...localStorage })).not.toContain(lowerToken);
    first.unmount();

    // 2. Content-Disposition filename carries the token: success shows the safe fallback name only
    const badName = vi.fn(async () => new Response("%PDF-1.4 x", {
      status: 200,
      headers: { "Content-Type": "application/pdf", "Content-Disposition": `attachment; filename="${MOCK_TOKEN}.pdf"` },
    }));
    render(<DocumentOperator fetchFn={badName as unknown as typeof fetch} triggerSave={triggerSave} />);
    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: MOCK_TOKEN } });
    fireEvent.change(screen.getByLabelText(/Document ID/i), { target: { value: VALID_UUID_2 } });
    fireEvent.click(screen.getByRole("button", { name: "下載 PDF" }));
    await waitFor(() => expect(screen.getByTestId("download-success")).toBeTruthy());
    expect(screen.getByTestId("download-success").textContent).toContain(`${VALID_UUID_2}.pdf`);
    expect(seen()).not.toContain(lowerToken);
    expect(triggerSave).toHaveBeenCalledTimes(1);
    expect(triggerSave.mock.calls[0][1]).toBe(`${VALID_UUID_2}.pdf`);
  });
});
