import { useState } from "react";
import { downloadBenchmark } from "./workflowApi";

type Props = {
  fetchFn?: typeof fetch;
  token: string;
  projectId: string;
  logicalId: string;
  /** Hands the file to the browser; replaced in tests. */
  triggerSave?: (blob: Blob, filename: string) => void;
};

export function browserSave(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);          // the object URL is gone as soon as the download was handed over
}

/** Download the whole benchmark (questions, reference answers, facts, evidence, automated verdicts, exclusions) to check the format. */
export function BenchmarkDownload({ fetchFn = fetch, token, projectId, logicalId, triggerSave = browserSave }: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  const run = async (format: "json" | "jsonl") => {
    setBusy(format);
    setMessage(null);
    const res = await downloadBenchmark({ projectId, logicalId, token, fetchFn, format });
    setBusy(null);
    if (res.ok) {
      triggerSave(res.blob, res.filename);
      setMessage({ ok: true, text: `已下載 ${res.filename}` });
    } else {
      setMessage({ ok: false, text: res.error.message });
    }
  };

  return (
    <div data-testid="benchmark-download" style={{ margin: "0.5rem 0" }}>
      <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap", alignItems: "center" }}>
        <button type="button" className="secondary" disabled={busy !== null} onClick={() => run("json")}
          style={{ padding: "0.35rem 0.8rem", borderRadius: "8px", border: "1px solid var(--primary)", cursor: "pointer" }}>
          {busy === "json" ? "下載中…" : "下載題庫 JSON"}
        </button>
        <button type="button" className="secondary" disabled={busy !== null} onClick={() => run("jsonl")}
          style={{ padding: "0.35rem 0.8rem", borderRadius: "8px", border: "1px solid var(--primary)", cursor: "pointer" }}>
          {busy === "jsonl" ? "下載中…" : "下載題庫 JSONL"}
        </button>
        <span className="muted small">內含標準答案、要點與證據原文（gold），只有 owner／maintainer 下載得到，不要給參與者。</span>
      </div>
      {message && (message.ok ? <p role="status" className="small">{message.text}</p> : <div role="alert" className="box box-error">{message.text}</div>)}
    </div>
  );
}
