import React, { useEffect, useRef, useState } from "react";
import {
  downloadDocument,
  uploadDocument,
  validateUuid,
  type ApiErrorDetail,
  type UploadSuccess,
} from "./documents";

export type DocumentOperatorProps = {
  fetchFn?: typeof fetch;
  triggerSave?: (blob: Blob, filename: string) => void;
  generateIdempotencyKey?: () => string;
};

const defaultIdempotencyGenerator = (): string => {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `idem-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
};

export function DocumentOperator({
  fetchFn = fetch,
  triggerSave,
  generateIdempotencyKey = defaultIdempotencyGenerator,
}: DocumentOperatorProps) {
  // Security invariant: Token is held strictly in component memory and never persisted or logged.
  const [token, setToken] = useState("");

  // Upload state
  const [projectId, setProjectId] = useState("");
  const [logicalId, setLogicalId] = useState("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadError, setUploadError] = useState<ApiErrorDetail | null>(null);
  const [uploadResult, setUploadResult] = useState<UploadSuccess | null>(null);

  // Stable idempotency key tracking across ambiguous outcomes (HTTP 5xx, malformed 2xx, network failure)
  const lastIdempotencySlotRef = useRef<{
    key: string;
    projectId: string;
    file: File | Blob;
    logicalId: string;
  } | null>(null);

  // Active request cancellation controllers
  const uploadAbortControllerRef = useRef<AbortController | null>(null);
  const downloadAbortControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => {
      uploadAbortControllerRef.current?.abort();
      downloadAbortControllerRef.current?.abort();
    };
  }, []);

  // Download state
  const [downloadDocId, setDownloadDocId] = useState("");
  const [isDownloading, setIsDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<ApiErrorDetail | null>(null);
  const [downloadSuccessInfo, setDownloadSuccessInfo] = useState<{
    filename: string;
    sizeBytes: number;
  } | null>(null);

  const handleUploadSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (isUploading) return;

    setUploadError(null);
    setUploadResult(null);

    const trimmedToken = token.trim();
    if (!trimmedToken) {
      setUploadError({
        message: "認證失敗：未提供 Token，請於上方輸入 Bearer Token",
        status: 401,
        code: "unauthorized",
        isUnauthorized: true,
      });
      return;
    }

    const trimmedProjectId = projectId.trim();
    if (!trimmedProjectId) {
      setUploadError({
        message: "請輸入 Project ID（UUID）",
        status: 400,
        code: "bad_request",
      });
      return;
    }

    if (!validateUuid(trimmedProjectId)) {
      setUploadError({
        message: "Project ID 格式錯誤，須為標準 UUID 格式",
        status: 400,
        code: "bad_request",
      });
      return;
    }

    if (!selectedFile) {
      setUploadError({
        message: "請選擇要上傳的 PDF 檔案",
        status: 400,
        code: "missing_file",
      });
      return;
    }

    // Track File object identity strictly (rotates on file change, preserves key for retries of same selection)
    const effectiveLogicalId = logicalId.trim() || "default";
    const effectiveProjectId = trimmedProjectId.toLowerCase();

    let idempotencyKey: string;
    if (
      lastIdempotencySlotRef.current &&
      lastIdempotencySlotRef.current.projectId === effectiveProjectId &&
      lastIdempotencySlotRef.current.file === selectedFile &&
      lastIdempotencySlotRef.current.logicalId === effectiveLogicalId
    ) {
      // Retain the existing key across ambiguous outcomes / retry of same selection
      idempotencyKey = lastIdempotencySlotRef.current.key;
    } else {
      // Changed identity (different File object, project, or logical ID): generate a fresh key
      idempotencyKey = generateIdempotencyKey();
      lastIdempotencySlotRef.current = {
        key: idempotencyKey,
        projectId: effectiveProjectId,
        file: selectedFile,
        logicalId: effectiveLogicalId,
      };
    }

    const controller = new AbortController();
    uploadAbortControllerRef.current = controller;

    setIsUploading(true);
    try {
      const res = await uploadDocument({
        projectId: effectiveProjectId,
        token: trimmedToken,
        file: selectedFile,
        logicalId: logicalId.trim() || undefined,
        idempotencyKey,
        fetchFn,
        signal: controller.signal,
      });

      if (res.ok) {
        // Confirmed result: clear retained key slot so subsequent uploads rotate key
        lastIdempotencySlotRef.current = null;
        setUploadResult(res.data);
      } else {
        // Preserve key for ALL ambiguous outcomes:
        // - user cancel / timeout
        // - network failure
        // - any HTTP 5xx (500, 502, 503, 504)
        // - malformed / failed 2xx (e.g. 201 with invalid response shape)
        // - HTTP 408 Request Timeout
        // A rejection of this retry does not establish the outcome of an earlier
        // ambiguous attempt. Keep the key until success or the inputs change.
        setUploadError(res.error);
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setUploadError({
        message: `上傳失敗（${msg}）`,
        code: "upload_error",
      });
    } finally {
      setIsUploading(false);
      uploadAbortControllerRef.current = null;
    }
  };

  const handleDownloadSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (isDownloading) return;

    setDownloadError(null);
    setDownloadSuccessInfo(null);

    const trimmedToken = token.trim();
    if (!trimmedToken) {
      setDownloadError({
        message: "認證失敗：未提供 Token，請於上方輸入 Bearer Token",
        status: 401,
        code: "unauthorized",
        isUnauthorized: true,
      });
      return;
    }

    const trimmedDocId = downloadDocId.trim();
    if (!trimmedDocId) {
      setDownloadError({
        message: "請輸入 Document ID（UUID）",
        status: 400,
        code: "bad_request",
      });
      return;
    }

    if (!validateUuid(trimmedDocId)) {
      setDownloadError({
        message: "Document ID 格式錯誤，須為標準 UUID 格式",
        status: 400,
        code: "bad_request",
      });
      return;
    }

    const controller = new AbortController();
    downloadAbortControllerRef.current = controller;

    setIsDownloading(true);
    try {
      const res = await downloadDocument({
        documentId: trimmedDocId,
        token: trimmedToken,
        fetchFn,
        triggerSave,
        signal: controller.signal,
      });

      if (res.ok) {
        setDownloadSuccessInfo({
          filename: res.data.filename,
          sizeBytes: res.data.sizeBytes,
        });
      } else {
        setDownloadError(res.error);
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setDownloadError({
        message: `下載失敗（${msg}）`,
        code: "download_error",
      });
    } finally {
      setIsDownloading(false);
      downloadAbortControllerRef.current = null;
    }
  };

  return (
    <section aria-label="文件操作員介面" style={{ marginTop: "1.5rem" }}>
      <h2>文件操作員介面</h2>

      {/* Auth Token Input */}
      <div style={{ marginBottom: "1rem", padding: "0.75rem", border: "1px solid #ccc" }}>
        <label htmlFor="auth-token" style={{ display: "block", fontWeight: "bold" }}>
          認證 Token (Bearer Token)
        </label>
        <input
          id="auth-token"
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="lab_act_<actor_uuid>_<hmac_sha256>"
          style={{ width: "100%", maxWidth: "500px", padding: "0.25rem" }}
          autoComplete="off"
        />
        <p style={{ fontSize: "0.85rem", color: "#555", marginTop: "0.25rem" }}>
          此 Token 僅暫存於本機記憶體中，絕不上傳 Storage、Cookie 亦不記錄於 Log。
        </p>
      </div>

      {/* Upload Section */}
      <div style={{ marginBottom: "1.5rem", padding: "1rem", border: "1px solid #ddd" }}>
        <h3>上傳文件 (Authenticated PDF Upload)</h3>
        <form onSubmit={handleUploadSubmit} noValidate>
          <div style={{ marginBottom: "0.5rem" }}>
            <label htmlFor="upload-project-id" style={{ display: "block" }}>
              Project ID (UUID) *
            </label>
            <input
              id="upload-project-id"
              type="text"
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
              placeholder="00000000-0000-0000-0000-000000000000"
              style={{ width: "100%", maxWidth: "400px" }}
              disabled={isUploading}
            />
          </div>

          <div style={{ marginBottom: "0.5rem" }}>
            <label htmlFor="upload-logical-id" style={{ display: "block" }}>
              Logical ID (選填，預設 default)
            </label>
            <input
              id="upload-logical-id"
              type="text"
              value={logicalId}
              onChange={(e) => setLogicalId(e.target.value)}
              placeholder="default"
              style={{ width: "100%", maxWidth: "400px" }}
              disabled={isUploading}
            />
          </div>

          <div style={{ marginBottom: "0.75rem" }}>
            <label htmlFor="upload-file" style={{ display: "block" }}>
              PDF 檔案 *
            </label>
            <input
              id="upload-file"
              type="file"
              accept="application/pdf,.pdf"
              onChange={(e) => setSelectedFile(e.target.files?.[0] ?? null)}
              disabled={isUploading}
            />
          </div>

          <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
            <button
              type="submit"
              disabled={isUploading}
              style={{ padding: "0.4rem 1rem", cursor: isUploading ? "not-allowed" : "pointer" }}
            >
              {isUploading ? "上傳中…" : "上傳 PDF"}
            </button>
            {isUploading && (
              <button
                type="button"
                onClick={() => uploadAbortControllerRef.current?.abort()}
                style={{
                  padding: "0.4rem 1rem",
                  cursor: "pointer",
                  backgroundColor: "#fff0f0",
                  borderColor: "#f99",
                }}
              >
                取消上傳
              </button>
            )}
          </div>
        </form>

        {/* Upload State Display */}
        {isUploading && (
          <p role="status" style={{ marginTop: "0.5rem", color: "#0066cc" }}>
            文件上傳中，請稍候…
          </p>
        )}

        {uploadError && (
          <div
            role="alert"
            data-testid="upload-error"
            style={{
              marginTop: "0.75rem",
              padding: "0.5rem",
              backgroundColor: "#fee",
              border: "1px solid #f99",
            }}
          >
            <strong>
              {uploadError.isPermissionDenied
                ? "權限不足 (403 Permission Denied)"
                : uploadError.isUnauthorized
                  ? "認證失敗 (401 Unauthorized)"
                  : "上傳失敗"}
              ：
            </strong>{" "}
            {uploadError.message}
            {uploadError.code && <span style={{ fontSize: "0.85rem" }}> ({uploadError.code})</span>}
          </div>
        )}

        {uploadResult && (
          <div
            data-testid="upload-success"
            style={{
              marginTop: "0.75rem",
              padding: "0.5rem",
              backgroundColor: "#efe",
              border: "1px solid #9c9",
            }}
          >
            <p style={{ fontWeight: "bold", margin: "0 0 0.5rem 0", color: "#060" }}>
              文件上傳成功！
              {uploadResult.replayed && "（冪等請求已重播）"}
            </p>
            <ul style={{ margin: 0, paddingLeft: "1.2rem", fontSize: "0.9rem" }}>
              <li>Document Version ID: {uploadResult.documentVersionId}</li>
              <li>Logical ID: {uploadResult.logicalId}</li>
              <li>Revision: {uploadResult.revision}</li>
              <li>頁數: {uploadResult.pageCount}</li>
              <li>檔案大小: {uploadResult.sizeBytes} 位元組</li>
              <li>SHA-256: {uploadResult.sha256}</li>
              {uploadResult.flagged && (
                <li style={{ color: "#d90" }}>標記狀態：已標記為 Flagged</li>
              )}
              {uploadResult.warnings && uploadResult.warnings.length > 0 && (
                <li style={{ color: "#d90" }}>
                  警示訊息: {uploadResult.warnings.join(", ")}
                </li>
              )}
            </ul>
            <button
              type="button"
              onClick={() => setDownloadDocId(uploadResult.documentVersionId)}
              style={{ marginTop: "0.5rem", fontSize: "0.85rem" }}
            >
              帶入此 Document ID 至下載欄位
            </button>
          </div>
        )}

        {!isUploading && !uploadError && !uploadResult && (
          <p data-testid="upload-empty-state" style={{ marginTop: "0.5rem", color: "#666" }}>
            尚未上傳任何文件。
          </p>
        )}
      </div>

      {/* Download Section */}
      <div style={{ marginBottom: "1.5rem", padding: "1rem", border: "1px solid #ddd" }}>
        <h3>下載文件 (Authorized PDF Download)</h3>
        <form onSubmit={handleDownloadSubmit} noValidate>
          <div style={{ marginBottom: "0.5rem" }}>
            <label htmlFor="download-document-id" style={{ display: "block" }}>
              Document ID (UUID) *
            </label>
            <input
              id="download-document-id"
              type="text"
              value={downloadDocId}
              onChange={(e) => setDownloadDocId(e.target.value)}
              placeholder="00000000-0000-0000-0000-000000000000"
              style={{ width: "100%", maxWidth: "400px" }}
              disabled={isDownloading}
            />
          </div>

          <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
            <button
              type="submit"
              disabled={isDownloading}
              style={{ padding: "0.4rem 1rem", cursor: isDownloading ? "not-allowed" : "pointer" }}
            >
              {isDownloading ? "下載中…" : "下載 PDF"}
            </button>
            {isDownloading && (
              <button
                type="button"
                onClick={() => downloadAbortControllerRef.current?.abort()}
                style={{
                  padding: "0.4rem 1rem",
                  cursor: "pointer",
                  backgroundColor: "#fff0f0",
                  borderColor: "#f99",
                }}
              >
                取消下載
              </button>
            )}
          </div>
        </form>

        {/* Download State Display */}
        {isDownloading && (
          <p role="status" style={{ marginTop: "0.5rem", color: "#0066cc" }}>
            文件下載中，請稍候…
          </p>
        )}

        {downloadError && (
          <div
            role="alert"
            data-testid="download-error"
            style={{
              marginTop: "0.75rem",
              padding: "0.5rem",
              backgroundColor: "#fee",
              border: "1px solid #f99",
            }}
          >
            <strong>
              {downloadError.isPermissionDenied
                ? "權限不足 (403 Permission Denied)"
                : downloadError.isUnauthorized
                  ? "認證失敗 (401 Unauthorized)"
                  : downloadError.status === 404
                    ? "文件不存在 (404 Not Found)"
                    : "下載失敗"}
              ：
            </strong>{" "}
            {downloadError.message}
            {downloadError.code && <span style={{ fontSize: "0.85rem" }}> ({downloadError.code})</span>}
          </div>
        )}

        {downloadSuccessInfo && (
          <div
            data-testid="download-success"
            style={{
              marginTop: "0.75rem",
              padding: "0.5rem",
              backgroundColor: "#efe",
              border: "1px solid #9c9",
            }}
          >
            <p style={{ fontWeight: "bold", margin: 0, color: "#060" }}>
              文件下載成功！檔名：{downloadSuccessInfo.filename}（{downloadSuccessInfo.sizeBytes} 位元組）
            </p>
          </div>
        )}

        {!isDownloading && !downloadError && !downloadSuccessInfo && (
          <p data-testid="download-empty-state" style={{ marginTop: "0.5rem", color: "#666" }}>
            尚未下載任何文件。
          </p>
        )}
      </div>

      {/* Limits & Feature Availability Notice */}
      <section aria-label="系統限制與功能說明" style={{ padding: "0.75rem", backgroundColor: "#f9f9f9" }}>
        <h4>系統限制與功能說明</h4>
        <ul>
          <li>
            <strong>檔案容量上限</strong>：依據規範，單一 PDF 檔案上限為 512 MiB；錯誤回應上限為 64 KiB。
          </li>
          <li>
            <strong>傳輸時限與取消</strong>：上傳與下載具備逾時中斷保護與串流保護，並支援使用者操作取消。
          </li>
          <li>
            <strong>後端服務可用性</strong>：文件上傳與授權下載需依賴後端文件服務部署支援；若後端尚未啟用該服務，相關 API 請求將回傳 404。
          </li>
          <li>
            <strong>尚未接入的功能</strong>：登入與憑證發行、非同步任務進度追蹤、題庫工作台、評分與比較檢視畫面等皆還沒有實作。
          </li>
          <li>
            <strong>功能範圍</strong>：本介面僅提供單一文件之上傳與下載操作員介面，不包含自動化工作流或後續評測功能。
          </li>
        </ul>
      </section>
    </section>
  );
}
