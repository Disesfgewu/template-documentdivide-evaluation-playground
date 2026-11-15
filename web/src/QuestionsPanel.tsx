import { useEffect, useState } from "react";
import { getRunResults, listQuestions, type PublicQuestion, type ResultItem } from "./workflowApi";

const MAX_PAGES = 5; // 5 x 100 questions: a hard bound on how much the page loads

type Props = {
  fetchFn?: typeof fetch;
  token: string;
  projectId: string;
  logicalId: string;
  /** Bump to reload (after generation or validation finishes). */
  reloadKey: string;
  /** When a method run exists, the per-question scores are shown next to each question. */
  runId?: string | null;
  evaluationId?: string | null;
};

type Load =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; questions: PublicQuestion[]; truncated: boolean };

function scoreCell(item: ResultItem | undefined): { text: string; tone: "good" | "bad" | "wait" | "none" } {
  if (!item) return { text: "—", tone: "none" };
  if (item.status === "scored" && item.raw_value !== null) {
    const v = item.raw_value;
    return { text: `${v.toFixed(0)} 分`, tone: v >= 100 ? "good" : v > 0 ? "wait" : "bad" };
  }
  if (item.status === "pending") return { text: "尚未評分", tone: "wait" };
  if (item.status === "not_applicable") return { text: "不適用", tone: "none" };
  return { text: `錯誤：${item.reason_codes[0] ?? "—"}`, tone: "bad" };
}

export function QuestionsPanel({ fetchFn = fetch, token, projectId, logicalId, reloadKey, runId, evaluationId }: Props) {
  const [load, setLoad] = useState<Load>({ kind: "idle" });
  const [byMetric, setByMetric] = useState<Record<string, Record<string, ResultItem>>>({});

  useEffect(() => {
    if (!reloadKey || !logicalId) return;
    const controller = new AbortController();
    setLoad({ kind: "loading" });
    (async () => {
      const all: PublicQuestion[] = [];
      let cursor: string | null = null;
      let truncated = false;
      for (let page = 0; page < MAX_PAGES; page++) {
        const res = await listQuestions({ projectId, logicalId, token, cursor, fetchFn, signal: controller.signal });
        if (controller.signal.aborted) return;
        if (!res.ok) {
          setLoad({ kind: "error", message: res.error.message });
          return;
        }
        all.push(...res.data.items);
        cursor = res.data.next_cursor;
        if (!cursor) break;
        if (page === MAX_PAGES - 1) truncated = true;
      }
      setLoad({ kind: "ready", questions: all, truncated });
    })().catch(() => {
      if (!controller.signal.aborted) setLoad({ kind: "error", message: "讀取題目時發生錯誤" });
    });
    return () => controller.abort();
  }, [reloadKey, logicalId, projectId, token, fetchFn]);

  useEffect(() => {
    if (!runId) {
      setByMetric({});
      return;
    }
    const controller = new AbortController();
    (async () => {
      const next: Record<string, Record<string, ResultItem>> = {};
      for (const metric of ["recall_at_k", "correctness"]) {
        const res = await getRunResults({
          runId, evaluationId: evaluationId || undefined, metricName: metric, limit: 200, token, fetchFn, signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        if (res.ok) next[metric] = Object.fromEntries((res.data.items ?? []).map((i) => [i.question_id, i]));
      }
      setByMetric(next);
    })().catch(() => undefined);
    return () => controller.abort();
  }, [runId, evaluationId, token, fetchFn, reloadKey]);

  if (load.kind === "idle") return <p className="muted">題目生成後，這裡會列出每一題。</p>;
  if (load.kind === "loading") return <p role="status">讀取題目中…</p>;
  if (load.kind === "error") return <div role="alert" className="box box-error"><strong>讀取題目失敗：</strong> {load.message}</div>;
  if (load.questions.length === 0) return <p className="muted">目前沒有題目。</p>;

  const showScores = Boolean(runId) && Object.keys(byMetric).length > 0;
  return (
    <div data-testid="questions-list">
      <p className="muted small">
        共 {load.questions.length} 題{load.truncated ? "（只顯示前 500 題）" : ""}。只顯示題目文字，標準答案與證據不會出現在這裡。
        {showScores ? " 右側是這次方法在每一題的得分。" : ""}
      </p>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th style={{ width: "3rem" }}>#</th>
              <th>題目</th>
              {showScores && <th>證據命中</th>}
              {showScores && <th>答案正確性</th>}
            </tr>
          </thead>
          <tbody>
            {load.questions.map((q, i) => {
              const recall = scoreCell(byMetric["recall_at_k"]?.[q.id]);
              const corr = scoreCell(byMetric["correctness"]?.[q.id]);
              return (
                <tr key={q.id}>
                  <td>{i + 1}</td>
                  <td>{q.question_text}</td>
                  {showScores && <td className={`tone-${recall.tone}`}>{recall.text}</td>}
                  {showScores && <td className={`tone-${corr.tone}`}>{corr.text}</td>}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
