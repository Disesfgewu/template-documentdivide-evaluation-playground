import { describe, expect, it, vi } from "vitest";
import {
  downloadDocument,
  extractFilenameFromHeader,
  hasValidPdfSignature,
  parseErrorResponse,
  parseUploadResponse,
  sanitizeFilename,
  uploadDocument,
  validateSha256,
  validateUuid,
} from "./documents";

const VALID_UUID_1 = "11111111-1111-1111-1111-111111111111";
const VALID_UUID_2 = "22222222-2222-2222-2222-222222222222";
const VALID_UUID_3 = "33333333-3333-3333-3333-333333333333";
const VALID_UUID_4 = "44444444-4444-4444-4444-444444444444";
const VALID_SHA256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
const MOCK_TOKEN = `lab_act_${VALID_UUID_1}_${VALID_SHA256}`;

describe("validateUuid and validateSha256", () => {
  it("validates UUIDs strictly", () => {
    expect(validateUuid(VALID_UUID_1)).toBe(true);
    expect(validateUuid("A0B1C2D3-E4F5-6789-abcd-ef0123456789")).toBe(true);
    expect(validateUuid("")).toBe(false);
    expect(validateUuid("not-a-uuid")).toBe(false);
    expect(validateUuid("11111111-1111-1111-1111")).toBe(false);
    expect(validateUuid("../traversal")).toBe(false);
  });

  it("validates SHA256 hashes strictly", () => {
    expect(validateSha256(VALID_SHA256)).toBe(true);
    expect(validateSha256("abc")).toBe(false);
    expect(validateSha256(VALID_SHA256 + "a")).toBe(false);
    expect(validateSha256("g".repeat(64))).toBe(false); // non-hex
  });
});

describe("hasValidPdfSignature", () => {
  it("accepts %PDF- at offset zero", () => {
    const data = new TextEncoder().encode("%PDF-1.4 binary content");
    expect(hasValidPdfSignature(data)).toBe(true);
  });

  it("accepts %PDF- with leading whitespace within first 1024 bytes (matching backend importer)", () => {
    // Leading spaces, tabs, CR, LF, null bytes
    const text = "   \r\n\t\0  %PDF-1.7 content";
    const data = new TextEncoder().encode(text);
    expect(hasValidPdfSignature(data)).toBe(true);

    // Exactly 1023 leading spaces + %PDF- (total idx = 1023 < 1024)
    const longWs = new Uint8Array(1023 + 5);
    longWs.fill(32, 0, 1023);
    longWs.set(new TextEncoder().encode("%PDF-"), 1023);
    expect(hasValidPdfSignature(longWs)).toBe(true);
  });

  it("rejects when leading whitespace exceeds or equals 1024 bytes", () => {
    // 1024 leading spaces before %PDF-
    const tooMuchWs = new Uint8Array(1024 + 5);
    tooMuchWs.fill(32, 0, 1024);
    tooMuchWs.set(new TextEncoder().encode("%PDF-"), 1024);
    expect(hasValidPdfSignature(tooMuchWs)).toBe(false);
  });

  it("rejects non-PDF content or HTML", () => {
    expect(hasValidPdfSignature(new TextEncoder().encode("<!DOCTYPE html>"))).toBe(false);
    expect(hasValidPdfSignature(new TextEncoder().encode("  <html><body>Not a PDF</body></html>"))).toBe(false);
    expect(hasValidPdfSignature(new Uint8Array([]))).toBe(false);
  });
});

describe("sanitizeFilename and extractFilenameFromHeader", () => {
  it("strips path traversal and preserves basename", () => {
    expect(sanitizeFilename("../../../secret.pdf", "fallback.pdf")).toBe("secret.pdf");
    expect(sanitizeFilename("C:\\Users\\admin\\file.pdf", "fallback.pdf")).toBe("file.pdf");
    expect(sanitizeFilename(' "quoted.pdf" ', "fallback.pdf")).toBe("quoted.pdf");
    expect(sanitizeFilename("", "fallback.pdf")).toBe("fallback.pdf");
  });

  it("extracts filename from Content-Disposition header safely", () => {
    expect(
      extractFilenameFromHeader('attachment; filename="doc-version-1.pdf"', "fallback.pdf"),
    ).toBe("doc-version-1.pdf");
    expect(
      extractFilenameFromHeader("attachment; filename=document.pdf", "fallback.pdf"),
    ).toBe("document.pdf");
    expect(
      extractFilenameFromHeader('attachment; filename="valid%20doc.pdf"', "fallback.pdf"),
    ).toBe("valid doc.pdf");
    expect(extractFilenameFromHeader(null, "fallback.pdf")).toBe("fallback.pdf");
  });

  it("never throws on malformed percent-encoding and falls back safely", () => {
    // Incomplete UTF-8 percent-encoded sequence
    expect(() => extractFilenameFromHeader('attachment; filename="%E0%A4"', "fallback.pdf")).not.toThrow();
    expect(extractFilenameFromHeader('attachment; filename="%E0%A4"', "fallback.pdf")).toBe("fallback.pdf");

    // Invalid percent hex characters
    expect(() => extractFilenameFromHeader('attachment; filename*="UTF-8\'\'%ZZ.pdf"', "fallback.pdf")).not.toThrow();
    expect(extractFilenameFromHeader('attachment; filename*="UTF-8\'\'%ZZ.pdf"', "fallback.pdf")).toBe("fallback.pdf");

    // Lone percent sign
    expect(() => extractFilenameFromHeader('attachment; filename="%"', "fallback.pdf")).not.toThrow();
    expect(extractFilenameFromHeader('attachment; filename="%"', "fallback.pdf")).toBe("fallback.pdf");
  });
});

describe("parseErrorResponse", () => {
  it("maps SPEC 16.4 error structure", () => {
    const specErr = {
      error: {
        code: "permission_denied",
        message: "observer requires explicit grant",
        retryable: false,
        request_id: "req-1",
        details: { reason: "forbidden" },
      },
    };
    const parsed = parseErrorResponse(403, specErr);
    expect(parsed.status).toBe(403);
    expect(parsed.code).toBe("permission_denied");
    expect(parsed.message).toBe("observer requires explicit grant");
    expect(parsed.isPermissionDenied).toBe(true);
    expect(parsed.isUnauthorized).toBe(false);
    expect(parsed.details).toEqual({ reason: "forbidden" });
  });

  it("maps 401 unauthorized", () => {
    const specErr = {
      error: {
        code: "unauthorized",
        message: "missing Authorization header",
      },
    };
    const parsed = parseErrorResponse(401, specErr);
    expect(parsed.isUnauthorized).toBe(true);
    expect(parsed.isPermissionDenied).toBe(false);
  });

  it("handles non-JSON or malformed error body safely without crashing", () => {
    expect(parseErrorResponse(502, null).message).toContain("502");
    expect(parseErrorResponse(500, "internal server error").message).toContain("500");
    expect(parseErrorResponse(400, { detail: "bad query" }).message).toBe("bad query");
  });
});

describe("parseUploadResponse strict validation (fail closed)", () => {
  const validBase = {
    id: VALID_UUID_1,
    document_version_id: VALID_UUID_1,
    project_id: VALID_UUID_2,
    resource_id: VALID_UUID_3,
    artifact_id: VALID_UUID_4,
    logical_id: "default",
    revision: 1,
    sha256: VALID_SHA256,
    size_bytes: 4096,
    page_count: 3,
    flagged: false,
    warnings: ["warning-1"],
  };

  it("parses valid upload response matching expected project", () => {
    const parsed = parseUploadResponse(validBase, VALID_UUID_2);
    expect(parsed).not.toBeNull();
    expect(parsed?.id).toBe(VALID_UUID_1);
    expect(parsed?.projectId).toBe(VALID_UUID_2);
    expect(parsed?.revision).toBe(1);
    expect(parsed?.pageCount).toBe(3);
    expect(parsed?.sizeBytes).toBe(4096);
    expect(parsed?.sha256).toBe(VALID_SHA256);
  });

  it("fails closed on inconsistent project ID vs expected request project", () => {
    // Response project_id does not match expected project_id
    expect(parseUploadResponse(validBase, VALID_UUID_1)).toBeNull();
  });

  it("fails closed on inconsistent id vs document_version_id", () => {
    const inconsistent = { ...validBase, document_version_id: VALID_UUID_2 };
    expect(parseUploadResponse(inconsistent, VALID_UUID_2)).toBeNull();
  });

  it("fails closed on invalid UUIDs in any ID field", () => {
    expect(parseUploadResponse({ ...validBase, id: "bad-uuid" }, VALID_UUID_2)).toBeNull();
    expect(parseUploadResponse({ ...validBase, project_id: "bad-uuid" })).toBeNull();
    expect(parseUploadResponse({ ...validBase, resource_id: "bad-uuid" }, VALID_UUID_2)).toBeNull();
    expect(parseUploadResponse({ ...validBase, artifact_id: "bad-uuid" }, VALID_UUID_2)).toBeNull();
  });

  it("fails closed on invalid SHA-256 hash", () => {
    expect(parseUploadResponse({ ...validBase, sha256: "not-a-hash" }, VALID_UUID_2)).toBeNull();
    expect(parseUploadResponse({ ...validBase, sha256: VALID_SHA256.slice(0, 63) }, VALID_UUID_2)).toBeNull();
  });

  it("fails closed on zero, negative, or non-safe integer numbers", () => {
    // revision
    expect(parseUploadResponse({ ...validBase, revision: 0 }, VALID_UUID_2)).toBeNull();
    expect(parseUploadResponse({ ...validBase, revision: -1 }, VALID_UUID_2)).toBeNull();
    expect(parseUploadResponse({ ...validBase, revision: 1.5 }, VALID_UUID_2)).toBeNull();

    // size_bytes
    expect(parseUploadResponse({ ...validBase, size_bytes: 0 }, VALID_UUID_2)).toBeNull();
    expect(parseUploadResponse({ ...validBase, size_bytes: -10 }, VALID_UUID_2)).toBeNull();
    expect(parseUploadResponse({ ...validBase, size_bytes: 1024.2 }, VALID_UUID_2)).toBeNull();

    // page_count
    expect(parseUploadResponse({ ...validBase, page_count: 0 }, VALID_UUID_2)).toBeNull();
    expect(parseUploadResponse({ ...validBase, page_count: -5 }, VALID_UUID_2)).toBeNull();
    expect(parseUploadResponse({ ...validBase, page_count: 2.1 }, VALID_UUID_2)).toBeNull();
  });

  it("fails closed on empty or oversized logical_id", () => {
    expect(parseUploadResponse({ ...validBase, logical_id: "" }, VALID_UUID_2)).toBeNull();
    expect(parseUploadResponse({ ...validBase, logical_id: "a".repeat(129) }, VALID_UUID_2)).toBeNull();
  });

  it("counts logical IDs in Unicode code points like the API", () => {
    const accepted = "😀".repeat(128);
    expect(parseUploadResponse({ ...validBase, logical_id: accepted }, VALID_UUID_2)?.logicalId).toBe(accepted);
    expect(parseUploadResponse({ ...validBase, logical_id: "😀".repeat(129) }, VALID_UUID_2)).toBeNull();
  });
});

describe("uploadDocument", () => {
  const mockPdfBlob = new Blob(["%PDF-1.4 mock content"], { type: "application/pdf" });

  it("sends request with correct contract: URL, method, Bearer token, multipart body, Idempotency-Key", async () => {
    let capturedUrl = "";
    let capturedInit: RequestInit | undefined;

    const mockFetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      capturedUrl = String(url);
      capturedInit = init;
      return new Response(
        JSON.stringify({
          id: VALID_UUID_1,
          document_version_id: VALID_UUID_1,
          project_id: VALID_UUID_2,
          resource_id: VALID_UUID_3,
          artifact_id: VALID_UUID_4,
          logical_id: "spec-doc",
          revision: 2,
          sha256: VALID_SHA256,
          size_bytes: 1024,
          page_count: 5,
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      );
    });

    const result = await uploadDocument({
      projectId: VALID_UUID_2,
      token: MOCK_TOKEN,
      file: mockPdfBlob,
      logicalId: "spec-doc",
      idempotencyKey: "idem-key-123",
      fetchFn: mockFetch as unknown as typeof fetch,
    });

    expect(capturedUrl).toBe(`/api/v1/projects/${VALID_UUID_2}/documents`);
    expect(capturedInit?.method).toBe("POST");
    expect((capturedInit?.headers as Record<string, string>)?.Authorization).toBe(`Bearer ${MOCK_TOKEN}`);
    expect((capturedInit?.headers as Record<string, string>)?.["Idempotency-Key"]).toBe("idem-key-123");
    expect(capturedInit?.body).toBeInstanceOf(FormData);

    const formData = capturedInit?.body as FormData;
    expect(formData.get("file")).toBeTruthy();
    expect(formData.get("logical_id")).toBe("spec-doc");

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data.id).toBe(VALID_UUID_1);
      expect(result.data.pageCount).toBe(5);
      expect(result.data.revision).toBe(2);
    }
  });

  it("fails closed when response project_id does not match request project_id", async () => {
    const mockFetch = vi.fn(async () =>
      new Response(
        JSON.stringify({
          id: VALID_UUID_1,
          document_version_id: VALID_UUID_1,
          project_id: VALID_UUID_1, // Mismatch: requested VALID_UUID_2
          resource_id: VALID_UUID_3,
          artifact_id: VALID_UUID_4,
          logical_id: "spec-doc",
          revision: 1,
          sha256: VALID_SHA256,
          size_bytes: 1024,
          page_count: 1,
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      ),
    );

    const result = await uploadDocument({
      projectId: VALID_UUID_2,
      token: MOCK_TOKEN,
      file: mockPdfBlob,
      fetchFn: mockFetch as unknown as typeof fetch,
    });

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe("invalid_response_shape");
    }
  });

  it("rejects invalid UUID projectId before any fetch", async () => {
    const mockFetch = vi.fn();
    const result = await uploadDocument({
      projectId: "bad-id",
      token: MOCK_TOKEN,
      file: mockPdfBlob,
      fetchFn: mockFetch,
    });
    expect(mockFetch).not.toHaveBeenCalled();
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe("bad_request");
    }
  });

  it("rejects missing token before any fetch", async () => {
    const mockFetch = vi.fn();
    const result = await uploadDocument({
      projectId: VALID_UUID_1,
      token: "   ",
      file: mockPdfBlob,
      fetchFn: mockFetch,
    });
    expect(mockFetch).not.toHaveBeenCalled();
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.isUnauthorized).toBe(true);
    }
  });

  it("rejects empty file before fetch", async () => {
    const mockFetch = vi.fn();
    const emptyBlob = new Blob([], { type: "application/pdf" });
    const result = await uploadDocument({
      projectId: VALID_UUID_1,
      token: MOCK_TOKEN,
      file: emptyBlob,
      fetchFn: mockFetch,
    });
    expect(mockFetch).not.toHaveBeenCalled();
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe("empty_file");
    }
  });

  it("handles 403 permission denied from backend", async () => {
    const mockFetch = vi.fn(async () =>
      new Response(
        JSON.stringify({
          error: {
            code: "permission_denied",
            message: "permission denied for actor",
            retryable: false,
          },
        }),
        { status: 403, headers: { "Content-Type": "application/json" } },
      ),
    );

    const result = await uploadDocument({
      projectId: VALID_UUID_1,
      token: MOCK_TOKEN,
      file: mockPdfBlob,
      fetchFn: mockFetch as unknown as typeof fetch,
    });

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.status).toBe(403);
      expect(result.error.isPermissionDenied).toBe(true);
      expect(result.error.message).toBe("permission denied for actor");
    }
  });

  it("handles 413 payload too large and 422 rejected", async () => {
    const mockFetch = vi.fn(async () =>
      new Response(
        JSON.stringify({
          error: {
            code: "payload_too_large",
            message: "PDF file exceeds size limit",
          },
        }),
        { status: 413, headers: { "Content-Type": "application/json" } },
      ),
    );

    const result = await uploadDocument({
      projectId: VALID_UUID_1,
      token: MOCK_TOKEN,
      file: mockPdfBlob,
      fetchFn: mockFetch as unknown as typeof fetch,
    });

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.status).toBe(413);
      expect(result.error.code).toBe("payload_too_large");
    }
  });

  it("handles network failure / connection timeout safely", async () => {
    const mockFetch = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });

    const result = await uploadDocument({
      projectId: VALID_UUID_1,
      token: MOCK_TOKEN,
      file: mockPdfBlob,
      fetchFn: mockFetch as unknown as typeof fetch,
    });

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe("network_error");
      expect(result.error.message).toContain("Failed to fetch");
    }
  });
});

describe("downloadDocument", () => {
  it("sends request with correct contract and verifies application/pdf Content-Type and %PDF- signature", async () => {
    let capturedUrl = "";
    let capturedInit: RequestInit | undefined;

    const mockFetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      capturedUrl = String(url);
      capturedInit = init;
      return new Response("%PDF-1.4 dummy bytes", {
        status: 200,
        headers: {
          "Content-Type": "application/pdf",
          "Content-Disposition": `attachment; filename="${VALID_UUID_1}.pdf"`,
        },
      });
    });

    const saveSpy = vi.fn();

    const result = await downloadDocument({
      documentId: VALID_UUID_1,
      token: MOCK_TOKEN,
      fetchFn: mockFetch as unknown as typeof fetch,
      triggerSave: saveSpy,
    });

    expect(capturedUrl).toBe(`/api/v1/documents/${VALID_UUID_1}/download`);
    expect(capturedInit?.method).toBe("GET");
    expect((capturedInit?.headers as Record<string, string>)?.Authorization).toBe(`Bearer ${MOCK_TOKEN}`);
    expect((capturedInit?.headers as Record<string, string>)?.Accept).toBe("application/pdf");

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.data.filename).toBe(`${VALID_UUID_1}.pdf`);
      expect(result.data.sizeBytes).toBeGreaterThan(0);
      expect(saveSpy).toHaveBeenCalledTimes(1);
      expect(saveSpy.mock.calls[0][1]).toBe(`${VALID_UUID_1}.pdf`);
      expect(saveSpy.mock.calls[0][0].size).toBe(20);
    }
  });

  it("rejects HTTP 200 HTML without saving (requires application/pdf)", async () => {
    const saveSpy = vi.fn();
    const mockFetch = vi.fn(async () =>
      new Response("<html><body>Login Page</body></html>", {
        status: 200,
        headers: { "Content-Type": "text/html; charset=utf-8" },
      }),
    );

    const result = await downloadDocument({
      documentId: VALID_UUID_1,
      token: MOCK_TOKEN,
      fetchFn: mockFetch as unknown as typeof fetch,
      triggerSave: saveSpy,
    });

    expect(saveSpy).not.toHaveBeenCalled();
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe("invalid_media_type");
      expect(result.error.message).toContain("text/html");
    }
  });

  it("rejects text/plain even when parameter contains application/pdf (exact token, not includes)", async () => {
    const saveSpy = vi.fn();
    const mockFetch = vi.fn(async () =>
      new Response("%PDF-1.4 binary content", {
        status: 200,
        headers: { "Content-Type": 'text/plain; format="application/pdf"' },
      }),
    );

    const result = await downloadDocument({
      documentId: VALID_UUID_1,
      token: MOCK_TOKEN,
      fetchFn: mockFetch as unknown as typeof fetch,
      triggerSave: saveSpy,
    });

    expect(saveSpy).not.toHaveBeenCalled();
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe("invalid_media_type");
      expect(result.error.message).toContain("text/plain");
    }
  });

  it("accepts valid PDF with application/pdf parameters and leading whitespace before %PDF-", async () => {
    const saveSpy = vi.fn();
    const bodyContent = "   \r\n\t%PDF-1.4 content";
    const mockFetch = vi.fn(async () =>
      new Response(bodyContent, {
        status: 200,
        headers: {
          "Content-Type": "application/pdf; charset=binary",
          "Content-Disposition": `attachment; filename="${VALID_UUID_1}.pdf"`,
        },
      }),
    );

    const result = await downloadDocument({
      documentId: VALID_UUID_1,
      token: MOCK_TOKEN,
      fetchFn: mockFetch as unknown as typeof fetch,
      triggerSave: saveSpy,
    });

    expect(result.ok).toBe(true);
    expect(saveSpy).toHaveBeenCalledTimes(1);
    if (result.ok) {
      expect(result.data.sizeBytes).toBe(bodyContent.length);
    }
  });

  it("rejects HTTP 200 with application/pdf header but invalid signature (not %PDF-)", async () => {
    const saveSpy = vi.fn();
    const mockFetch = vi.fn(async () =>
      new Response("<html><body>Fake Content-Type</body></html>", {
        status: 200,
        headers: { "Content-Type": "application/pdf" },
      }),
    );

    const result = await downloadDocument({
      documentId: VALID_UUID_1,
      token: MOCK_TOKEN,
      fetchFn: mockFetch as unknown as typeof fetch,
      triggerSave: saveSpy,
    });

    expect(saveSpy).not.toHaveBeenCalled();
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe("invalid_pdf_signature");
      expect(result.error.message).toContain("%PDF-");
    }
  });

  it("rejects invalid documentId before fetch", async () => {
    const mockFetch = vi.fn();
    const result = await downloadDocument({
      documentId: "not-a-uuid",
      token: MOCK_TOKEN,
      fetchFn: mockFetch,
    });
    expect(mockFetch).not.toHaveBeenCalled();
    expect(result.ok).toBe(false);
  });

  it("rejects missing token before fetch", async () => {
    const mockFetch = vi.fn();
    const result = await downloadDocument({
      documentId: VALID_UUID_1,
      token: "",
      fetchFn: mockFetch,
    });
    expect(mockFetch).not.toHaveBeenCalled();
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.isUnauthorized).toBe(true);
    }
  });

  it("handles 404 not found", async () => {
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

    const result = await downloadDocument({
      documentId: VALID_UUID_1,
      token: MOCK_TOKEN,
      fetchFn: mockFetch as unknown as typeof fetch,
    });

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.status).toBe(404);
      expect(result.error.code).toBe("not_found");
    }
  });

  it("handles 403 permission denied", async () => {
    const mockFetch = vi.fn(async () =>
      new Response(
        JSON.stringify({
          error: {
            code: "permission_denied",
            message: "observer requires explicit grant",
          },
        }),
        { status: 403, headers: { "Content-Type": "application/json" } },
      ),
    );

    const result = await downloadDocument({
      documentId: VALID_UUID_1,
      token: MOCK_TOKEN,
      fetchFn: mockFetch as unknown as typeof fetch,
    });

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.status).toBe(403);
      expect(result.error.isPermissionDenied).toBe(true);
    }
  });

  it("handles 0-byte response as error", async () => {
    const mockFetch = vi.fn(async () =>
      new Response("", {
        status: 200,
        headers: { "Content-Type": "application/pdf" },
      }),
    );

    const result = await downloadDocument({
      documentId: VALID_UUID_1,
      token: MOCK_TOKEN,
      fetchFn: mockFetch as unknown as typeof fetch,
    });

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe("empty_file");
    }
  });
});
