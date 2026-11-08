#!/usr/bin/env node
/**
 * Real Headless Browser Acceptance Test for Document UI (Issue #83, Refs #13, R21, R23, T76).
 *
 * Runs against a live FastAPI + built web server (no mocks).
 * Uses real headless Chrome / Chromium via standard Chrome DevTools Protocol (CDP).
 *
 * All sensitive configuration is read from stdin as JSON (no tokens in CLI args or process list).
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

if (!token || !projectId || !pdfPath) {
  console.error("Missing required fields in stdin JSON: token, projectId, pdfPath");
  process.exit(2);
}

if (!browserPath || !fs.existsSync(browserPath)) {
  console.error("BLOCKED: Browser binary not found at: " + browserPath);
  process.exit(3);
}

console.log(`Starting headless browser: ${browserPath}`);
const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "chrome-ui-accept-"));

const browserProc = spawn(browserPath, [
  "--headless=new",
  "--remote-debugging-port=0",
  "--remote-debugging-address=127.0.0.1",
  "--disable-gpu",
  "--no-sandbox",
  `--user-data-dir=${userDataDir}`,
]);

let killed = false;
function cleanup() {
  if (killed) return;
  killed = true;
  try { browserProc.kill(); } catch {}
  try { fs.rmSync(userDataDir, { recursive: true, force: true }); } catch {}
}
process.on("exit", cleanup);
process.on("SIGINT", () => { cleanup(); process.exit(1); });
process.on("SIGTERM", () => { cleanup(); process.exit(1); });

const portPromise = new Promise((resolve, reject) => {
  const timeout = setTimeout(() => reject(new Error("Browser startup timeout")), 15000);
  browserProc.stderr.on("data", (data) => {
    const m = data.toString().match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)\//);
    if (m) {
      clearTimeout(timeout);
      resolve(m[1]);
    }
  });
  browserProc.on("error", (err) => {
    clearTimeout(timeout);
    reject(err);
  });
});

async function run() {
  const port = await portPromise;
  console.log(`Browser CDP listening on port ${port}`);

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

  // Helpers for React controlled inputs and DOM evaluation
  async function evaluate(expression) {
    const res = await cdp("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    if (res.exceptionDetails) {
      throw new Error("Evaluation error: " + JSON.stringify(res.exceptionDetails));
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

  eventListeners.add(async (method, params) => {
    if (method === "Fetch.requestPaused") {
      const { requestId, request, responseStatusCode } = params;
      const isUpload = request.url.includes("/documents") && request.method === "POST";
      const headers = request.headers || {};
      const idempotencyKey = Object.entries(headers).find(([k]) => k.toLowerCase() === "idempotency-key")?.[1];

      if (isUpload && interceptFirstUploadResponse) {
        // First upload request reached Response stage: backend has committed the transaction!
        interceptFirstUploadResponse = false;
        firstRecordedIdempotencyKey = idempotencyKey;
        firstBackendStatus = responseStatusCode;
        console.log(`Intercepted first upload response (backend status: ${responseStatusCode}, idempotency-key: ${idempotencyKey}); failing response to simulate lost response...`);
        // Fail network response at the client side after successful backend commit
        await cdp("Fetch.failRequest", { requestId, errorReason: "ConnectionReset" });
      } else if (isUpload && !interceptFirstUploadResponse) {
        // Second upload request (the retry)
        secondRecordedIdempotencyKey = idempotencyKey;
        console.log(`Intercepted retry upload request (idempotency-key: ${idempotencyKey}); continuing request...`);
        await cdp("Fetch.continueRequest", { requestId });
      } else {
        await cdp("Fetch.continueRequest", { requestId });
      }
    }
  });

  await cdp("Fetch.enable", {
    patterns: [{ urlPattern: "*documents*", requestStage: "Response" }],
  });

  console.log(`Navigating to ${url}...`);
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

  // 1. Fill auth token and project ID using React-safe setter
  console.log("Entering auth token, project ID, and logical ID...");
  await waitForSelector("#auth-token");
  await evaluate(`
    (() => {
      window.__setReactInput(document.getElementById("auth-token"), ${JSON.stringify(token)});
      window.__setReactInput(document.getElementById("upload-project-id"), ${JSON.stringify(projectId)});
      window.__setReactInput(document.getElementById("upload-logical-id"), "f01-pages-21");
    })()
  `);

  // 2. Attach synthetic 21-page PDF file
  console.log(`Attaching synthetic PDF fixture: ${pdfPath}...`);
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
  console.log("Submitting first upload form (response will be lost after commit)...");
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
  console.log("Verified: First attempt showed upload error with no false success in UI.");
  if (!firstRecordedIdempotencyKey) {
    throw new Error("First upload Idempotency-Key was not captured!");
  }
  if (firstBackendStatus !== 201 && firstBackendStatus !== 200) {
    throw new Error(`Expected backend commit status 201/200 before lost response, got ${firstBackendStatus}`);
  }

  // 4. Retry upload with SAME selected File (simulating operator retry after ambiguous network failure)
  console.log("Retrying upload with same selected File...");
  await evaluate(`
    (() => {
      const uploadForm = document.getElementById("upload-project-id").closest("form");
      uploadForm.querySelector("button[type='submit']").click();
    })()
  `);

  // 5. Verify retry success in UI
  await waitForSelector("[data-testid='upload-success']");
  const retrySuccessText = await evaluate(`document.querySelector("[data-testid='upload-success']").innerText`);
  console.log("Retry Upload Success Result:\n" + retrySuccessText);

  // Assert same idempotency key was used across both attempts
  if (firstRecordedIdempotencyKey !== secondRecordedIdempotencyKey) {
    throw new Error(
      `Idempotency key changed on retry! First: ${firstRecordedIdempotencyKey}, Second: ${secondRecordedIdempotencyKey}`
    );
  }
  console.log(`Verified: Same Idempotency-Key (${firstRecordedIdempotencyKey}) sent on both requests.`);

  // Assert replayed=true is shown in UI
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
  console.log(`Uploaded Document Version ID: ${docVersionId}`);

  // Track downloads in downloadDir
  const initialFiles = new Set(fs.readdirSync(downloadDir));

  // 6. Test download targeting the actual download form
  console.log("Entering Document ID in download form...");
  await waitForSelector("#download-document-id");
  await evaluate(`
    (() => {
      window.__setReactInput(document.getElementById("download-document-id"), ${JSON.stringify(docVersionId)});
      const downloadForm = document.getElementById("download-document-id").closest("form");
      downloadForm.querySelector("button[type='submit']").click();
    })()
  `);

  await waitForSelector("[data-testid='download-success']");
  console.log("Waiting for downloaded file to be written to disk...");

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
  console.log(`Verified: Downloaded file byte-identical and SHA-256 verified (${downloadedSha256}).`);

  // 7. Test cross-project denial with signed other actor
  if (otherToken) {
    console.log("Testing cross-project access denial with non-member actor token...");
    let unauthorizedStatusCode = null;

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
    const downloadErrorText = await evaluate(`document.querySelector("[data-testid='download-error']").innerText`);
    const downloadSuccessNow = await evaluate(`!!document.querySelector("[data-testid='download-success']")`);

    if (downloadSuccessNow) {
      throw new Error("Cross-project download showed false success!");
    }
    console.log("Cross-project denial UI error text:\n" + downloadErrorText);

    // Backend returns 404 (or 403) for non-member actor
    if (unauthorizedStatusCode !== 404 && unauthorizedStatusCode !== 403) {
      throw new Error(`Expected HTTP 404 or 403 for cross-project actor, got: ${unauthorizedStatusCode}`);
    }
    console.log(`Verified: Backend returned HTTP ${unauthorizedStatusCode} and UI presented truthful error.`);

    const fileCountAfter = fs.readdirSync(downloadDir).length;
    if (fileCountAfter !== fileCountBefore) {
      throw new Error("A file was downloaded despite cross-project denial!");
    }
  }

  console.log("SUCCESS: Real browser acceptance test passed all checks!");
  ws.close();
  cleanup();
  process.exit(0);
}

run().catch((err) => {
  console.error("FAIL:", err);
  cleanup();
  process.exit(1);
});
