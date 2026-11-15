// Headless browser launcher for the real-browser acceptance test (Issue #145, Refs #7).
//
// Launches the browser with a FRESH profile directory and waits for Chromium's "DevTools listening on ws://..."
// stderr line. A launch that dies or stays silent is killed (its whole process group), its profile is removed after
// the process has really closed, and the launch is retried (up to `attempts`). EVERY failed attempt is recorded in the
// result, also when a later attempt succeeds, so a retried flake is visible in the evidence instead of being absorbed.
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const TAIL = 4000;                 // a failed attempt keeps at most this much of the browser's own log (Issue #145, PM-DECISION)
const KEEP = 65536;                // the rolling buffer: a verbose log never grows without bound

// Diagnostics must not leak the environment: the browser's own log may print absolute paths (binary, home, runner temp). The
// temp profile of THIS attempt is kept (as <profile>); every other absolute path is reduced to <path>.
export function scrubLog(text, userDataDir) {
  return text.split(userDataDir).join("<profile>")
    .replace(/(?<![\w:\/.])\/(?:[\w.+@-]+\/)+[\w.+@-]*/g, "<path>");
}

function startOnce({ browserPath, startupMs, tmpDir, redact }) {
  const userDataDir = fs.mkdtempSync(path.join(tmpDir, "chrome-ui-accept-"));
  // detached: the browser leads its own process group, so its helper processes can be killed with it.
  const proc = spawn(browserPath, [
    "--headless=new",
    "--remote-debugging-port=0",
    "--remote-debugging-address=127.0.0.1",
    "--disable-gpu",
    "--no-sandbox",
    "--enable-logging=stderr",       // the browser's own log goes to stderr, where a failed attempt's tail is kept
    "--v=1",
    `--user-data-dir=${userDataDir}`,
  ], { detached: true });
  const started = Date.now();
  let stderrText = "";
  const attempt = { proc, userDataDir, closed: false };
  attempt.closedPromise = new Promise((resolve) => {
    proc.on("close", (code, signal) => { attempt.closed = true; attempt.exit = { code, signal, after_ms: Date.now() - started }; resolve(); });
    proc.on("error", (err) => { attempt.spawnError = String(err.message || err); attempt.closed = true; resolve(); });
  });
  attempt.ready = new Promise((resolve, reject) => {
    let settled = false;
    const finish = (fn, value) => { if (!settled) { settled = true; clearTimeout(timer); fn(value); } };
    const timer = setTimeout(() => finish(reject, "no DevTools line within " + startupMs + " ms"), startupMs);
    proc.stderr.on("data", (data) => {
      stderrText = (stderrText + data.toString()).slice(-KEEP);
      const m = stderrText.match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)\//);
      if (m) finish(resolve, { port: m[1], startupMs: Date.now() - started });
    });
    proc.on("close", () => finish(reject, "browser exited before reporting DevTools"));
    proc.on("error", (err) => finish(reject, "spawn error: " + (err.message || err)));
  });
  attempt.describe = (reason) => ({
    reason,
    elapsed_ms: Date.now() - started,
    exit: attempt.exit || null,
    spawn_error: attempt.spawnError || null,
    stderr_tail: redact(scrubLog(stderrText.slice(-TAIL), userDataDir)),
  });
  return attempt;
}

export async function stopAttempt(attempt) {
  try { process.kill(-attempt.proc.pid, "SIGKILL"); } catch {}      // the whole group
  try { attempt.proc.kill("SIGKILL"); } catch {}
  let timer;
  await Promise.race([attempt.closedPromise, new Promise((r) => { timer = setTimeout(r, 5000); })]);   // wait for the real close
  clearTimeout(timer);                                                                                // do not keep node alive
  for (let i = 0; i < 10; i += 1) {
    try { fs.rmSync(attempt.userDataDir, { recursive: true, force: true }); return; } catch {}
    await new Promise((r) => setTimeout(r, 200));
  }
}

export async function launchBrowser({ browserPath, startupMs = 20000, attempts = 3, tmpDir = os.tmpdir(), redact = (s) => s }) {
  const failed = [];
  for (let n = 1; n <= attempts; n += 1) {
    const attempt = startOnce({ browserPath, startupMs, tmpDir, redact });
    try {
      const ready = await attempt.ready;
      return { ...ready, attempts: n, failedAttempts: failed, attempt };
    } catch (reason) {
      failed.push(attempt.describe(String(reason)));
      await stopAttempt(attempt);
    }
  }
  const error = new Error("Browser startup failed after " + attempts + " attempts: " + JSON.stringify(failed));
  error.failedAttempts = failed;
  throw error;
}
