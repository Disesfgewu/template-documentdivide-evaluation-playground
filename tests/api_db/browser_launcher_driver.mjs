// Test driver for browser_launcher.mjs: reads {browserPath, startupMs, attempts, tmpDir} as JSON on stdin and prints
// the launch result (without the process handles); the browser is stopped before exit.
import fs from "node:fs";
import { launchBrowser, stopAttempt } from "./browser_launcher.mjs";

const config = JSON.parse(fs.readFileSync(0, "utf8"));
try {
  const result = await launchBrowser(config);
  await stopAttempt(result.attempt);
  console.log(JSON.stringify({ ok: true, port: result.port, attempts: result.attempts, failedAttempts: result.failedAttempts }));
} catch (error) {
  console.log(JSON.stringify({ ok: false, message: String(error.message).slice(0, 300), failedAttempts: error.failedAttempts }));
}
