import { useEffect, useState } from "react";
import { fetchApiStatus, type ApiStatus } from "./status";
import { DocumentOperator } from "./DocumentOperator";
import { Workflow } from "./Workflow";

export function App({
  load = fetchApiStatus,
  fetchFn = fetch,
  triggerSave,
  initialTab = "workflow",
}: {
  load?: () => Promise<ApiStatus>;
  fetchFn?: typeof fetch;
  triggerSave?: (blob: Blob, filename: string) => void;
  initialTab?: "operator" | "workflow";
}) {
  const [status, setStatus] = useState<ApiStatus>({ kind: "loading" });
  const [activeTab, setActiveTab] = useState<"operator" | "workflow">(initialTab);

  useEffect(() => {
    let alive = true;
    load()
      .catch((): ApiStatus => ({ kind: "error", message: "讀取狀態時發生錯誤" }))
      .then((s) => {
        if (alive) setStatus(s);
      });
    return () => {
      alive = false;
    };
  }, [load]);

  return (
    <main className="app">
      <header className="app-header">
        <h1>文件問答評測 Playground</h1>
        <p className="sub">上傳一份 PDF，自動出題、驗題，再看不同方法答得怎麼樣。</p>
        <StatusView status={status} />
      </header>

      <nav className="tabs" aria-label="主要分頁">
        <button type="button" className="tab" aria-pressed={activeTab === "workflow"} onClick={() => setActiveTab("workflow")}>
          工作流程
        </button>
        <button type="button" className="tab" aria-pressed={activeTab === "operator"} onClick={() => setActiveTab("operator")}>
          文件操作員
        </button>
      </nav>

      {activeTab === "operator" ? (
        <DocumentOperator fetchFn={fetchFn} triggerSave={triggerSave} />
      ) : (
        <Workflow fetchFn={fetchFn} />
      )}

      <section aria-label="尚未接入的功能" className="scope-note">
        <strong>目前範圍：</strong>
        只處理文字型 PDF；分數由程式與同一家模型產生，屬暫定結果，人工審查尚未進行。
        方法比較、執行軌跡（trace）與 PDF 高亮畫面還沒有實作。畫面不會顯示任何假資料或假進度。
      </section>
    </main>
  );
}

function StatusView({ status }: { status: ApiStatus }) {
  switch (status.kind) {
    case "loading":
      return <p role="status" className="status-pill">檢查中…</p>;
    case "error":
      return <p role="alert" className="status-pill bad">{status.message}</p>;
    case "ready":
    case "not_ready":
      return (
        <div>
          <p role="status" className={`status-pill ${status.kind === "ready" ? "ok" : "bad"}`}>
            {status.kind === "ready" ? "API 就緒" : "API 未就緒"}（provider：{status.providerMode}
            {status.providerMode === "mock" ? "，回應為本機假資料，非真模型" : ""}）
          </p>
          <details className="status-detail">
            <summary>各項檢查</summary>
            <ul>
              {Object.entries(status.checks).map(([name, c]) => (
                <li key={name}>
                  {name}：{c.ok ? "正常" : "失敗"} — {c.detail}
                </li>
              ))}
            </ul>
          </details>
        </div>
      );
  }
}
