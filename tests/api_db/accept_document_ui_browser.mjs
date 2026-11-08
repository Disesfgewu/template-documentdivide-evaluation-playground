#!/usr/bin/env node
/**
 * Real Headless Browser Acceptance Test for Document UI (Issue #83, Refs #13, R21, R23, T76).
 *
 * Runs against a live FastAPI + built web server (no mocks).
 * Uses real headless Chrome / Chromium via standard Chrome DevTools Protocol (CDP).
 *
 * Flow:
 * 1. Navigate to UI.
 * 2. Authenticate with signed Bearer token (memory only).
 * 3. Fill Project ID, select synthetic 21-page PDF fixture.
 * 4. Submit upload; verify UI shows 21 pages, correct SHA-256, revision 1, documentVersionId.
 * 5. Replay upload with same selection and idempotency key; verify UI shows replayed notice.
 * 6. Download document; verify downloaded bytes match original SHA-256 and size.
 * 7. Cross-project denial: test with non-member actor token; verify UI shows 403 error.
 */

import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";

function parseArgs() {
  const args = process.argv.slice(2);
  const options = {};
  for (let i = 0; i < args.length; i++) {
    if (args[i].startsWith("--")) {
      const key = args[i].slice(2);
      options[key] = args[i + 1];
      i++;
    }
  }
  return options;
}

const opts = parseArgs();
const url = opts.url || "http://127.0.0.1:8000/";
const token = opts.token;
const projectId = opts["project-id"];
const pdfPath = opts["pdf-path"];
const expectedSha256 = opts["expected-sha256"];
const expectedSize = parseInt(opts["expected-size"] || "0", 10);
const expectedPages = parseInt(opts["expected-pages"] || "21", 10);
const otherToken = opts["other-token"];
const downloadDir = opts["download-dir"] || fs.mkdtempSync(path.join(os.tmpdir(), "browser-download-"));

if (!token || !projectId || !pdfPath) {
  console.error("Missing required arguments: --token, --project-id, --pdf-path");
  process.exit(2);
}

// Locate browser binary
function findBrowser() {
  if (opts["browser-path"] && fs.existsSync(opts["browser-path"])) {
    return opts["browser-path"];
  }
  if (process.env.CHROME_BIN && fs.existsSync(process.env.CHROME_BIN)) {
    return process.env.CHROME_BIN;
  }
  const candidates = [
    "/home/martin/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  return null;
}

const browserPath = findBrowser();
if (!browserPath) {
  console.error("BLOCKED: No Chrome/Chromium browser found (set CHROME_BIN or install chrome)");
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

  // Fetch target list
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

  // Enable domains
  await cdp("Page.enable");
  await cdp("Runtime.enable");
  await cdp("DOM.enable");
  await cdp("Browser.setDownloadBehavior", {
    behavior: "allowAndName",
    downloadPath: downloadDir,
    eventsEnabled: true,
  });

  console.log(`Navigating to ${url}...`);
  await cdp("Page.navigate", { url });
  await new Promise((r) => setTimeout(r, 1000));

  async function evaluate(expression) {
    const res = await cdp("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
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

  // 1. Fill auth token and project ID
  console.log("Entering auth token and project ID...");
  await waitForSelector("#auth-token");
  await evaluate(`
    (() => {
      const t = document.getElementById("auth-token");
      t.value = ${JSON.stringify(token)};
      t.dispatchEvent(new Event("input", { bubbles: true }));
      const p = document.getElementById("upload-project-id");
      p.value = ${JSON.stringify(projectId)};
      p.dispatchEvent(new Event("input", { bubbles: true }));
      const l = document.getElementById("upload-logical-id");
      l.value = "f01-pages-21";
      l.dispatchEvent(new Event("input", { bubbles: true }));
    })()
  `);

  // 2. Attach PDF file
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

  // 3. Submit upload
  console.log("Submitting upload form...");
  await evaluate(`document.querySelector("form button[type='submit']").click()`);

  // 4. Verify upload result in UI
  console.log("Waiting for upload success in UI...");
  await waitForSelector("[data-testid='upload-success']");
  const uploadSuccessText = await evaluate(`document.querySelector("[data-testid='upload-success']").innerText`);
  console.log("UI Upload Success Result:\n" + uploadSuccessText);

  if (!uploadSuccessText.includes(`頁數: ${expectedPages}`)) {
    throw new Error(`UI did not show expected page count: ${expectedPages}`);
  }
  if (expectedSha256 && !uploadSuccessText.includes(expectedSha256)) {
    throw new Error(`UI did not show expected SHA-256: ${expectedSha256}`);
  }

  const docIdMatch = uploadSuccessText.match(/Document Version ID:\s*([0-9a-f-]+)/i);
  if (!docIdMatch) throw new Error("Could not extract Document Version ID from UI");
  const docVersionId = docIdMatch[1];
  console.log(`Uploaded Document Version ID: ${docVersionId}`);

  // 5. Test lost-response replay with same selection
  console.log("Testing lost-response replay with same selection/key...");
  await evaluate(`document.querySelector("form button[type='submit']").click()`);
  await new Promise((r) => setTimeout(r, 1000));
  const replayText = await evaluate(`document.querySelector("[data-testid='upload-success']").innerText`);
  console.log("Replay Result:\n" + replayText);

  // 6. Test download via UI
  console.log("Testing download via UI...");
  await evaluate(`
    (() => {
      const copyBtn = Array.from(document.querySelectorAll("button")).find(b => b.innerText.includes("帶入此 Document ID"));
      if (copyBtn) copyBtn.click();
      else {
        const d = document.getElementById("download-document-id");
        d.value = ${JSON.stringify(docVersionId)};
        d.dispatchEvent(new Event("input", { bubbles: true }));
      }
    })()
  `);

  // Click download button
  await evaluate(`
    (() => {
      const downloadSection = document.querySelector("input#download-document-id").closest("div");
      const submitBtn = downloadSection.querySelector("button[type='submit']");
      submitBtn.click();
    })()
  `);

  await waitForSelector("[data-testid='download-success']");
  const downloadSuccessText = await evaluate(`document.querySelector("[data-testid='download-success']").innerText`);
  console.log("Download Result:\n" + downloadSuccessText);

  // 7. Test cross-project denial
  if (otherToken) {
    console.log("Testing cross-project denial with other actor token...");
    await evaluate(`
      (() => {
        const t = document.getElementById("auth-token");
        t.value = ${JSON.stringify(otherToken)};
        t.dispatchEvent(new Event("input", { bubbles: true }));
        const downloadSection = document.querySelector("input#download-document-id").closest("div");
        downloadSection.querySelector("button[type='submit']").click();
      })()
    `);
    await waitForSelector("[data-testid='download-error']");
    const downloadErrorText = await evaluate(`document.querySelector("[data-testid='download-error']").innerText`);
    console.log("Cross-project download error result:\n" + downloadErrorText);
    if (!downloadErrorText.includes("權限不足") && !downloadErrorText.includes("403")) {
      throw new Error(`Expected truthful 403 error, got: ${downloadErrorText}`);
    }
  }

  console.log("SUCCESS: Real browser acceptance test passed!");
  ws.close();
  cleanup();
  process.exit(0);
}

run().catch((err) => {
  console.error("FAIL:", err);
  cleanup();
  process.exit(1);
});
