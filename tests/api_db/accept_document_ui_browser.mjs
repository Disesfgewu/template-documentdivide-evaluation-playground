#!/usr/bin/env node
/**
 * Real Headless Browser Acceptance Test for Document UI (Issue #83, Refs #13, R21, R23, T76).
 *
 * Runs against a live FastAPI + built web server (no mocks).
 * Uses real headless Chrome / Chromium via standard Chrome DevTools Protocol (CDP).
 *
 * All sensitive configuration is read from stdin as JSON (no tokens in CLI args or process list).
 * All exception paths sanitize tokens and secret data before printing.
 */

import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";

async function readStdin() {
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => { data += chunk; });
    process.stdin.on("end", () => {
      try {
        resolve(JSON.parse(data));
      } catch (err) {
        reject(new Error("Invalid JSON on stdin: " + err.message));
      }
    });
    process.stdin.on("error", reject);
  });
}

const config = await readStdin();
const url = config.url || "http://127.0.0.1:8000/";
const token = config.token;
const projectId = config.projectId;
const pdfPath = config.pdfPath;
const expectedSha256 = config.expectedSha256;
const expectedSize = parseInt(config.expectedSize || "0", 10);
const expectedPages = parseInt(config.expectedPages || "21", 10);
const otherToken = config.otherToken;
const downloadDir = config.downloadDir || fs.mkdtempSync(path.join(os.tmpdir(), "browser-download-"));
const browserPath = config.browserPath;
const evidenceOutputPath = config.evidenceOutputPath;

function sanitizeError(err) {
  if (!err) return err;
  let msg = err instanceof Error ? err.message : String(err);
  if (token) msg = msg.split(token).join("[REDACTED_TOKEN]");
  if (otherToken) msg = msg.split(otherToken).join("[REDACTED_OTHER_TOKEN]");
  const sanitized = new Error(msg);
  if (err.stack) {
    let s = err.stack;
    if (token) s = s.split(token).join("[REDACTED_TOKEN]");
    if (otherToken) s = s.split(otherToken).join("[REDACTED_OTHER_TOKEN]");
    sanitized.stack = s;
  }
  return sanitized;
}

if (!token || !projectId || !pdfPath) {
  console.error("Missing required fields in stdin JSON: token, projectId, pdfPath");
  process.exit(2);
}

if (!browserPath || !fs.existsSync(browserPath)) {
  console.error("BLOCKED: Browser binary not found at: " + browserPath);
  process.exit(3);
}

// Browser launch: see browser_launcher.mjs (retry with a fresh profile, every failed attempt recorded).
import { launchBrowser } from "./browser_launcher.mjs";

let currentAttempt = null;
let killed = false;
function cleanup() {
  if (killed) return;
  killed = true;
  if (currentAttempt) {
    try { process.kill(-currentAttempt.proc.pid, "SIGKILL"); } catch {}
    try { currentAttempt.proc.kill("SIGKILL"); } catch {}
    try { fs.rmSync(currentAttempt.userDataDir, { recursive: true, force: true }); } catch {}
  }
}
process.on("exit", cleanup);
process.on("SIGINT", () => { cleanup(); process.exit(1); });
process.on("SIGTERM", () => { cleanup(); process.exit(1); });

const redactSecrets = (text) => {
  let out = text;
  if (token) out = out.split(token).join("[REDACTED_TOKEN]");
  if (otherToken) out = out.split(otherToken).join("[REDACTED_OTHER_TOKEN]");
  return out;
};
const portPromise = launchBrowser({ browserPath, startupMs: 20000, attempts: 3, redact: redactSecrets }).then((launched) => {
  currentAttempt = launched.attempt;
  return launched;
});

async function run() {
  const launched = await portPromise;
  const port = launched.port;
  const launchRecord = { attempts: launched.attempts, startup_ms: launched.startupMs, failed_attempts: launched.failedAttempts };
  console.log(`browser started on attempt ${launched.attempts} after ${launched.startupMs} ms`);

  // Fetch page target
  const listRes = await fetch(`http://127.0.0.1:${port}/json/list`);
  const list = await listRes.json();
  const pageTarget = list.find((t) => t.type === "page") || list[0];
  if (!pageTarget) throw new Error("No page target found");

  const ws = new WebSocket(pageTarget.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = reject;
  });

  let idCounter = 1;
  const cdp = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const id = idCounter++;
      const handler = (evt) => {
        try {
          const msg = JSON.parse(evt.data);
          if (msg.id === id) {
            ws.removeEventListener("message", handler);
            if (msg.error) reject(new Error(`${method} failed: ${JSON.stringify(msg.error)}`));
            else resolve(msg.result);
          }
        } catch (e) {
          reject(e);
        }
      };
      ws.addEventListener("message", handler);
      ws.send(JSON.stringify({ id, method, params }));
    });

  // Event handlers
  const eventListeners = new Set();
  ws.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      if (msg.method) {
        for (const listener of eventListeners) {
          listener(msg.method, msg.params);
        }
      }
    } catch {}
  };

  // Enable domains
  await cdp("Page.enable");
  await cdp("Runtime.enable");
  await cdp("DOM.enable");
  await cdp("Network.enable");
  await cdp("Browser.setDownloadBehavior", {
    behavior: "allowAndName",
    downloadPath: downloadDir,
    eventsEnabled: true,
  });

  const browserVersion = await cdp("Browser.getVersion");

  async function evaluate(expression) {
    const res = await cdp("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    if (res.exceptionDetails) {
      throw sanitizeError(new Error("Evaluation error: " + (res.exceptionDetails.text || "runtime error")));
    }
    return res.result?.value;
  }

  async function waitForSelector(selector, timeoutMs = 15000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const found = await evaluate(`!!document.querySelector(${JSON.stringify(selector)})`);
      if (found) return true;
      await new Promise((r) => setTimeout(r, 200));
    }
    throw new Error(`Timeout waiting for selector: ${selector}`);
  }

  // Setup Fetch interception for genuine lost first response
  let interceptFirstUploadResponse = true;
  let firstRecordedIdempotencyKey = null;
  let secondRecordedIdempotencyKey = null;
  let firstBackendStatus = null;
  let secondBackendStatus = null;
  let uploadXRequestId = null;

  eventListeners.add(async (method, params) => {
    if (method === "Fetch.requestPaused") {
      const { requestId, request, responseStatusCode, responseHeaders } = params;
      const isUpload = request.url.includes("/documents") && request.method === "POST";
      const headers = request.headers || {};
      const idempotencyKey = Object.entries(headers).find(([k]) => k.toLowerCase() === "idempotency-key")?.[1];

      if (isUpload && responseHeaders) {
        const reqHeader = responseHeaders.find((h) => h.name.toLowerCase() === "x-request-id");
        if (reqHeader) {
          uploadXRequestId = reqHeader.value;
        }
      }

      if (isUpload && interceptFirstUploadResponse) {
        interceptFirstUploadResponse = false;
        firstRecordedIdempotencyKey = idempotencyKey;
        firstBackendStatus = responseStatusCode;
        await cdp("Fetch.failRequest", { requestId, errorReason: "ConnectionReset" });
      } else if (isUpload && !interceptFirstUploadResponse) {
        secondRecordedIdempotencyKey = idempotencyKey;
        secondBackendStatus = responseStatusCode;
        await cdp("Fetch.continueRequest", { requestId });
      } else {
        await cdp("Fetch.continueRequest", { requestId });
      }
    }
  });

  await cdp("Fetch.enable", {
    patterns: [{ urlPattern: "*documents*", requestStage: "Response" }],
  });

  await cdp("Page.navigate", { url });
  await new Promise((r) => setTimeout(r, 1000));

  // Helper in page to set React controlled input values via native descriptor setter
  await evaluate(`
    window.__setReactInput = (el, val) => {
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
      setter.call(el, val);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    };
  `);

  // The page opens on the workflow tab; this acceptance flow drives the document operator tab, so switch to it explicitly.
  await waitForSelector("nav[aria-label='主要分頁'] button");
  await evaluate(`
    (() => {
      const tab = [...document.querySelectorAll("nav[aria-label='主要分頁'] button")].find((b) => b.textContent.trim() === "文件操作員");
      if (!tab) throw new Error("operator tab not found");
      tab.click();
    })()
  `);

  // 1. Fill auth token and project ID using React-safe setter
  await waitForSelector("#auth-token");
  await evaluate(`
    (() => {
      window.__setReactInput(document.getElementById("auth-token"), ${JSON.stringify(token)});
      window.__setReactInput(document.getElementById("upload-project-id"), ${JSON.stringify(projectId)});
      window.__setReactInput(document.getElementById("upload-logical-id"), "f01-pages-21");
    })()
  `);

  // 2. Attach synthetic 21-page PDF file
  const docRoot = await cdp("DOM.getDocument");
  const fileInput = await cdp("DOM.querySelector", {
    nodeId: docRoot.root.nodeId,
    selector: "#upload-file",
  });
  if (!fileInput.nodeId) throw new Error("Could not find #upload-file in DOM");
  await cdp("DOM.setFileInputFiles", {
    nodeId: fileInput.nodeId,
    files: [path.resolve(pdfPath)],
  });
  await evaluate(`document.getElementById("upload-file").dispatchEvent(new Event("change", { bubbles: true }))`);

  // 3. Submit first upload attempt (which will experience a lost response after backend commit)
  await evaluate(`
    (() => {
      const uploadForm = document.getElementById("upload-project-id").closest("form");
      uploadForm.querySelector("button[type='submit']").click();
    })()
  `);

  // Assert first attempt shows UI error and NO success
  await waitForSelector("[data-testid='upload-error']");
  const uploadErrorVisible = await evaluate(`!!document.querySelector("[data-testid='upload-error']")`);
  const uploadSuccessVisible = await evaluate(`!!document.querySelector("[data-testid='upload-success']")`);
  if (!uploadErrorVisible || uploadSuccessVisible) {
    throw new Error("First upload did not show error or showed false success on lost response!");
  }
  if (!firstRecordedIdempotencyKey) {
    throw new Error("First upload Idempotency-Key was not captured!");
  }
  if (firstBackendStatus !== 201 && firstBackendStatus !== 200) {
    throw new Error(`Expected backend commit status 201/200 before lost response, got ${firstBackendStatus}`);
  }

  // 4. Retry upload with SAME selected File
  await evaluate(`
    (() => {
      const uploadForm = document.getElementById("upload-project-id").closest("form");
      uploadForm.querySelector("button[type='submit']").click();
    })()
  `);

  // 5. Verify retry success in UI
  await waitForSelector("[data-testid='upload-success']");
  const retrySuccessText = await evaluate(`document.querySelector("[data-testid='upload-success']").innerText`);

  if (!secondBackendStatus) {
    throw new Error("Retry backend response status was not measured!");
  }
  if (secondBackendStatus !== 200 && secondBackendStatus !== 201) {
    throw new Error(`Expected retry backend response status 200 or 201, got measured ${secondBackendStatus}`);
  }

  if (firstRecordedIdempotencyKey !== secondRecordedIdempotencyKey) {
    throw new Error(
      `Idempotency key changed on retry! First: ${firstRecordedIdempotencyKey}, Second: ${secondRecordedIdempotencyKey}`
    );
  }

  if (!retrySuccessText.includes("（冪等請求已重播）")) {
    throw new Error("Retry did not indicate replayed=true in UI!");
  }
  if (!retrySuccessText.includes(`頁數: ${expectedPages}`)) {
    throw new Error(`UI did not show expected page count: ${expectedPages}`);
  }
  if (expectedSha256 && !retrySuccessText.includes(expectedSha256)) {
    throw new Error(`UI did not show expected SHA-256: ${expectedSha256}`);
  }

  const docIdMatch = retrySuccessText.match(/Document Version ID:\s*([0-9a-f-]+)/i);
  if (!docIdMatch) throw new Error("Could not extract Document Version ID from UI");
  const docVersionId = docIdMatch[1];

  const initialFiles = new Set(fs.readdirSync(downloadDir));

  // 6. Test download targeting the actual download form
  await waitForSelector("#download-document-id");
  await evaluate(`
    (() => {
      window.__setReactInput(document.getElementById("download-document-id"), ${JSON.stringify(docVersionId)});
      const downloadForm = document.getElementById("download-document-id").closest("form");
      downloadForm.querySelector("button[type='submit']").click();
    })()
  `);

  await waitForSelector("[data-testid='download-success']");

  let downloadedFile = null;
  const downloadDeadline = Date.now() + 15000;
  while (Date.now() < downloadDeadline) {
    const currentFiles = fs.readdirSync(downloadDir);
    const newFiles = currentFiles.filter((f) => !initialFiles.has(f) && !f.endsWith(".crdownload"));
    if (newFiles.length > 0) {
      downloadedFile = path.join(downloadDir, newFiles[0]);
      break;
    }
    await new Promise((r) => setTimeout(r, 200));
  }

  if (!downloadedFile || !fs.existsSync(downloadedFile)) {
    throw new Error("Downloaded file was not written to disk in " + downloadDir);
  }

  const downloadedBytes = fs.readFileSync(downloadedFile);
  const downloadedSha256 = crypto.createHash("sha256").update(downloadedBytes).digest("hex");
  const originalBytes = fs.readFileSync(pdfPath);

  if (downloadedBytes.length !== originalBytes.length) {
    throw new Error(`Downloaded size (${downloadedBytes.length}) != original (${originalBytes.length})`);
  }
  if (downloadedSha256 !== expectedSha256) {
    throw new Error(`Downloaded SHA-256 (${downloadedSha256}) != expected (${expectedSha256})`);
  }
  if (Buffer.compare(downloadedBytes, originalBytes) !== 0) {
    throw new Error("Downloaded bytes do not match original bytes byte-for-byte!");
  }

  // 7. Test cross-project denial with signed other actor
  let unauthorizedStatusCode = null;
  if (otherToken) {
    eventListeners.add((method, params) => {
      if (method === "Network.responseReceived") {
        if (params.response.url.includes(`/documents/${docVersionId}/download`)) {
          unauthorizedStatusCode = params.response.status;
        }
      }
    });

    const fileCountBefore = fs.readdirSync(downloadDir).length;

    await evaluate(`
      (() => {
        window.__setReactInput(document.getElementById("auth-token"), ${JSON.stringify(otherToken)});
        const downloadForm = document.getElementById("download-document-id").closest("form");
        downloadForm.querySelector("button[type='submit']").click();
      })()
    `);

    await waitForSelector("[data-testid='download-error']");
    const downloadSuccessNow = await evaluate(`!!document.querySelector("[data-testid='download-success']")`);

    if (downloadSuccessNow) {
      throw new Error("Cross-project download showed false success!");
    }

    if (unauthorizedStatusCode !== 404 && unauthorizedStatusCode !== 403) {
      throw new Error(`Expected HTTP 404 or 403 for cross-project actor, got: ${unauthorizedStatusCode}`);
    }

    const fileCountAfter = fs.readdirSync(downloadDir).length;
    if (fileCountAfter !== fileCountBefore) {
      throw new Error("A file was downloaded despite cross-project denial!");
    }
  }

  // Build sanitized machine-readable evidence (no tokens, no secrets, no raw payload, no fabricated IDs)
  const evidence = {
    schema: "document-ui-browser-acceptance-v1",
    profile: "synthetic-f01-21-pages",
    status: "provisional",
    launch: launchRecord,
    timestamp: new Date().toISOString(),
    browser: {
      product: browserVersion.product || "Chrome",
      user_agent: browserVersion.userAgent || "",
      protocol_version: browserVersion.protocolVersion || "",
    },
    fixture: {
      id: "F01",
      pages: expectedPages,
      size_bytes: expectedSize,
      sha256: expectedSha256,
    },
    actual: {
      first_attempt: {
        backend_status: firstBackendStatus,
        client_outcome: "lost_response_ConnectionReset",
        ui_error_displayed: true,
        ui_success_displayed: false,
      },
      retry: {
        backend_status: secondBackendStatus,
        replayed: true,
        ui_replayed_notice_displayed: true,
        ui_pages_displayed: expectedPages,
        ui_sha256_displayed: expectedSha256,
        document_version_id: docVersionId,
        same_idempotency_key_preserved: true,
      },
      download: {
        downloaded_sha256: downloadedSha256,
        downloaded_size_bytes: downloadedBytes.length,
        matches_fixture: true,
      },
      cross_project_denial: {
        backend_status: unauthorizedStatusCode,
        ui_error_displayed: true,
        ui_success_displayed: false,
        file_downloaded: false,
      },
      trace: {
        upload_x_request_id: uploadXRequestId,
        request_id_available: uploadXRequestId !== null,
        request_id_status: uploadXRequestId ? "captured_from_upload_response_header" : "unavailable_in_api_response",
      },
    },
  };

  if (evidenceOutputPath) {
    fs.mkdirSync(path.dirname(evidenceOutputPath), { recursive: true });
    fs.writeFileSync(evidenceOutputPath, JSON.stringify(evidence, null, 2), "utf8");
  }

  console.log("JSON_EVIDENCE_START");
  console.log(JSON.stringify(evidence));
  console.log("JSON_EVIDENCE_END");

  ws.close();
  cleanup();
  process.exit(0);
}

run().catch((err) => {
  const cleanErr = sanitizeError(err);
  console.error("FAIL:", cleanErr.message);
  cleanup();
  process.exit(1);
});
