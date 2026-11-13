import { useCallback, useEffect, useState } from "react";
import { getQuestionReview, setQuestionExcluded, type ReviewItem, type ReviewResponse } from "./workflowApi";

type Props = {
  fetchFn?: typeof fetch;
  token: string;
  projectId: string;
  logicalId: string;
  /** Bump to reload (after generation or validation finishes). */
  reloadKey: string;
};

const VERDICT: Record<string, { label: string; tone: "good" | "bad" | "wait" | "none" }> = {
  pass: { label: "通過", tone: "good" },
  fail: { label: "未通過", tone: "bad" },
  needs_review: { label: "需人工看", tone: "wait" },
  unvalidated: { label: "還沒驗", tone: "none" },
};

const DEFAULT_REASON = "自動驗題沒有通過，這一題不納入凍結";

/** Owner view while the benchmark is still 'validating': every question with its automated verdict, and a way to exclude one
 *  (a recorded decision with a reason; nothing is deleted) so the freeze can go ahead without it. */
export function QuestionReview({ fetchFn = fetch, token, projectId, logicalId, reloadKey }: Props) {
  const [data, setData] = useState<ReviewResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [reason, setReason] = useState(DEFAULT_REASON);
  const [counter, setCounter] = useState(0);

  useEffect(() => {
    if (!reloadKey || !logicalId) return;
    const controller = new AbortController();
    getQuestionReview({ projectId, logicalId, token, fetchFn, signal: controller.signal }).then((res) => {
      if (controller.signal.aborted) return;
      if (res.ok) {
        setData(res.data);
        setError(null);
      } else {
        setError(res.error.message);
      }
    });
    return () => controller.abort();
  }, [reloadKey, counter, logicalId, projectId, token, fetchFn]);

  const act = useCallback(
    async (item: ReviewItem, excluded: boolean) => {
      if (!reason.trim()) {
        setError("請先寫下理由（會和這個決定一起記錄下來）");
        return;
      }
      setBusy(item.id);
      const res = await setQuestionExcluded({ projectId, logicalId, token, fetchFn, questionId: item.id, excluded, reason });
      setBusy(null);
      if (res.ok) {
        setError(null);
        setCounter((c) => c + 1);
      } else {
        setError(res.error.message);
      }
    },
    [projectId, logicalId, token, fetchFn, reason],
  );

  if (error && !data) return <div role="alert" className="box box-error"><strong>讀取題目失敗：</strong> {error}</div>;
  if (!data) return <p role="status">讀取題目中…</p>;

  const v = data.validation_of_active;
  return (
    <div data-testid="question-review">
      <p className="small" data-testid="review-summary">
        共 {data.total} 題，其中 {data.excluded} 題已排除；未排除的 {data.active} 題裡：通過 {v.pass}、未通過 {v.fail}、需人工 {v.needs_review}、還沒驗 {v.unvalidated}。{" "}
        <strong>{data.can_freeze ? "現在可以凍結。" : "還不能凍結：每一題未排除的題目都要通過驗題，而且至少要剩一題。"}</strong>
      </p>
      <p className="muted small">
        驗題是同一個模型做的自動檢查，不是人工審查。排除一題會連同理由一起記錄，不會刪掉任何東西；排除的題目不會被凍結、作答或計分，凍結之前都可以恢復。
      </p>
      {data.status === "validating" && (
        <div style={{ margin: "0.5rem 0" }}>
          <label htmlFor="exclusion-reason" style={{ display: "block" }}>排除／恢復的理由（每次操作都會記錄）</label>
          <input
            id="exclusion-reason"
            type="text"
            value={reason}
            maxLength={500}
            onChange={(e) => setReason(e.target.value)}
            style={{ width: "100%", maxWidth: "520px" }}
          />
        </div>
      )}
      {error && <div role="alert" className="box box-error">{error}</div>}
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th style={{ width: "3rem" }}>#</th>
              <th>題目</th>
              <th>驗題結論</th>
              <th>狀態</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((q) => {
              const verdict = VERDICT[q.verdict] ?? { label: q.verdict, tone: "none" as const };
              return (
                <tr key={q.id} style={q.excluded ? { opacity: 0.6 } : undefined} data-testid={`review-row-${q.ordinal}`}>
                  <td>{q.ordinal + 1}</td>
                  <td>{q.question_text}</td>
                  <td className={`tone-${verdict.tone}`}>
                    {verdict.label}
                    {q.verdict_reason && <div className="muted small">{q.verdict_reason}</div>}
                  </td>
                  <td>
                    {q.excluded ? (
                      <>
                        <div className="tone-wait">已排除</div>
                        {q.exclusion_reason && <div className="muted small">{q.exclusion_reason}</div>}
                      </>
                    ) : (
                      "納入"
                    )}
                    {data.status === "validating" && (
                      <button
                        type="button"
                        className="secondary"
                        disabled={busy !== null}
                        onClick={() => act(q, !q.excluded)}
                        style={{ marginTop: "0.25rem", padding: "0.2rem 0.6rem", borderRadius: "6px", border: "1px solid var(--primary)", cursor: "pointer" }}
                      >
                        {busy === q.id ? "處理中…" : q.excluded ? "恢復" : "排除"}
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
