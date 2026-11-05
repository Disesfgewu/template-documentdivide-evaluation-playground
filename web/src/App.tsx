import { useEffect, useState } from "react";
import { fetchApiStatus, type ApiStatus } from "./status";

export function App({ load = fetchApiStatus }: { load?: () => Promise<ApiStatus> }) {
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
    <main>
      <h1>document-seperater-playground LAB</h1>
      <section aria-label="系統狀態">
        <h2>系統狀態</h2>
        <StatusView status={status} />
      </section>
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
