import { useEffect, useState } from "react";
import { fetchApiStatus, type ApiStatus } from "./status";
import { DocumentOperator } from "./DocumentOperator";

export function App({
  load = fetchApiStatus,
  fetchFn = fetch,
  triggerSave,
}: {
  load?: () => Promise<ApiStatus>;
  fetchFn?: typeof fetch;
  triggerSave?: (blob: Blob, filename: string) => void;
}) {
  const [status, setStatus] = useState<ApiStatus>({ kind: "loading" });

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
    <main style={{ maxWidth: "800px", margin: "0 auto", padding: "1rem", fontFamily: "sans-serif" }}>
      <h1>document-seperater-playground LAB</h1>

      <section aria-label="系統狀態">
        <h2>系統狀態</h2>
        <StatusView status={status} />
      </section>

      <section aria-label="尚未接入的功能">
        <h2>尚未接入的功能</h2>
        <p>
          登入與憑證發行、非同步任務進度、題庫工作台、方法接入、評分、比較與 trace 畫面都還沒有實作。
          本頁不顯示任何假資料或假進度。文件上傳與下載功能需待後端文件服務就緒。
        </p>
      </section>

      <DocumentOperator fetchFn={fetchFn} triggerSave={triggerSave} />
    </main>
  );
}

function StatusView({ status }: { status: ApiStatus }) {
  switch (status.kind) {
    case "loading":
      return <p role="status">檢查中…</p>;
    case "error":
      return <p role="alert">{status.message}</p>;
    case "ready":
    case "not_ready":
      return (
        <div>
          <p role="status">
            {status.kind === "ready" ? "API 就緒" : "API 未就緒"}（provider：{status.providerMode}
            {status.providerMode === "mock" ? "，回應為本機假資料，非真模型" : ""}）
          </p>
          <ul>
            {Object.entries(status.checks).map(([name, c]) => (
              <li key={name}>
                {name}：{c.ok ? "正常" : "失敗"} — {c.detail}
              </li>
            ))}
          </ul>
        </div>
      );
  }
}
