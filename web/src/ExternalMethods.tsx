import { REPORT_HINT, downgradeNotes } from "./scoreReason";
import { useCallback, useEffect, useState } from "react";
import { browserSave } from "./BenchmarkDownload";
import {
  downloadQuestionsPackage,
  getJobStatus,
  getRunEstimate,
  getRunResults,
  getRunReviews,
  recordRunReviews,
  getSubmissionDetails,
  getSubmissionFormat,
  listUploads,
  startJudgeRun,
  uploadAnswers,
  type RunEstimate,
  type RunResultsResponse,
  type RunReviews,
  type SubmissionDetailItem,
  type SubmissionFormatDoc,
  type UploadProblem,
  type UploadReport,
  type UploadedMethod,
} from "./workflowApi";

type Common = { fetchFn?: typeof fetch; token: string; projectId: string; logicalId: string; triggerSave?: (b: Blob, n: string) => void };

const PROBLEM_TEXT: Record<string, string> = {
  invalid_json: "這一行不是合法的 JSON",
  not_an_object: "這一行不是 JSON 物件",
  unknown_keys: "有不認得的欄位",
  bad_question_id: "question_id 缺少或不是文字",
  unknown_question: "題目檔裡沒有這個 question_id",
  duplicate_question: "同一題出現第二次（只採用第一行）",
  bad_status: "status 必須是 answered 或 abstained",
  bad_answer: "answered 需要非空的 answer",
  answer_too_long: "answer 超過 4000 字",
  too_many_sources: "sources 超過 10 筆",
  bad_source: "sources 的格式不對（每筆要有 page 和 text）",
  bad_page: "page 必須是整數",
  page_out_of_range: "page 超出這份 PDF 的頁數（頁碼從 1 開始）",
  bad_source_text: "source 的 text 必須是文字",
  source_text_too_long: "source 的 text 超過 2000 字",
  source_text_too_short: "source 的 text 太短（至少 6 個非空白字元）",
  reason_on_answered: "answered 不該有 reason",
  reason_missing: "abstained 需要 reason",
  reason_too_long: "reason 超過 2000 字",
  abstained_with_answer_or_sources: "abstained 不該有 answer 或 sources",
  header_not_first: "標頭行（method_name…）必須在最前面",
  duplicate_header: "標頭行出現兩次",
  header_invalid: "標頭行的值都必須是文字",
};

const METRICS: [string, string][] = [
  ["recall_at_k", "檢索命中"], ["complete_at_k", "完整命中"], ["localization_precision", "定位精準度"],
  ["correctness", "答案正確性"], ["citation_precision", "引用精準度"], ["citation_completeness", "引用完整度"], ["faithfulness", "忠實度"],
];

function QuoteBadge({ found, quality, coverage }: { found?: boolean; quality?: string; coverage?: number }) {
  if (found) return <span className="tone-good">原文裡找得到</span>;
  if (quality === "page_without_text") return <span className="tone-wait">這一頁系統沒有解析出文字，無法確認</span>;
  return <span className="tone-bad" data-testid="quote-not-found">在這一頁找不到這段引文{coverage !== undefined ? `（只有 ${(coverage * 100).toFixed(0)}% 的字詞出現在該頁）` : ""}</span>;
}

/** Step 8: the questions-only file for the developer, and what to send back. */
export function DeveloperPackage({ fetchFn = fetch, token, projectId, logicalId, triggerSave = browserSave, onDownloaded }: Common & { onDownloaded?: () => void }) {
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [doc, setDoc] = useState<SubmissionFormatDoc | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void getSubmissionFormat({ token, fetchFn, signal: controller.signal }).then((r) => {
      if (!controller.signal.aborted && r.ok) setDoc(r.data);
    });
    return () => controller.abort();
  }, [token, fetchFn]);

  const download = async (format: "json" | "jsonl") => {
    setMsg(null);
    const res = await downloadQuestionsPackage({ projectId, logicalId, token, fetchFn, format });
    if (res.ok) {
      triggerSave(res.blob, res.filename);
      setMsg({ ok: true, text: `已下載 ${res.filename}（只有題目，沒有答案）` });
      onDownloaded?.();
    } else {
      setMsg({ ok: false, text: res.error.message });
    }
  };

  const saveFormat = () => {
    if (doc) triggerSave(new Blob([JSON.stringify(doc, null, 2)], { type: "application/json" }), "answers-file-format.json");
  };

  return (
    <div data-testid="developer-package">
      <p className="small">
        把題目檔交給開發人員。檔案裡<strong>只有題目文字</strong>（題號、順序、題目、語言），沒有標準答案、要點或證據，也看不出題目是怎麼出的。
        開發人員要對<strong>同一份 PDF</strong>回答每一題，並附上他引用的原文段落（頁碼＋原文），存成 JSONL 再回傳給你。
      </p>
      <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap", alignItems: "center" }}>
        <button type="button" onClick={() => download("jsonl")}
          style={{ padding: "0.5rem 1.1rem", borderRadius: "8px", border: "1px solid var(--primary)", background: "var(--primary)", color: "#fff", fontWeight: 600, cursor: "pointer" }}>
          下載題目檔（JSONL）
        </button>
        <button type="button" className="secondary" onClick={() => download("json")}
          style={{ padding: "0.5rem 1.1rem", borderRadius: "8px", border: "1px solid var(--primary)", cursor: "pointer" }}>
          下載題目檔（JSON）
        </button>
        <button type="button" className="secondary" onClick={saveFormat} disabled={!doc}
          style={{ padding: "0.5rem 1.1rem", borderRadius: "8px", border: "1px solid var(--primary)", cursor: "pointer" }}>
          下載答案檔格式說明（含 JSON schema）
        </button>
      </div>
      {msg && (msg.ok ? <p role="status" className="small">{msg.text}</p> : <div role="alert" className="box box-error">{msg.text}</div>)}
      <details style={{ marginTop: "0.5rem" }} open>
        <summary className="small" style={{ cursor: "pointer" }}>開發人員要回傳的答案檔長什麼樣子</summary>
        {doc ? (
          <div className="small" data-testid="format-doc">
            <ul style={{ margin: "0.25rem 0", paddingLeft: "1.2rem" }}>{doc.description.map((d) => <li key={d}>{d}</li>)}</ul>
            <p style={{ margin: "0.25rem 0" }}>範例（三行，第一行是選用的標頭）：</p>
            <pre style={{ overflowX: "auto", background: "#eef1f7", padding: "0.5rem", borderRadius: "8px", whiteSpace: "pre-wrap" }}>{doc.example_lines.join("\n")}</pre>
          </div>
        ) : <p className="muted small">讀取格式說明中…</p>}
      </details>
    </div>
  );
}

/** Step 9: upload the developer's answers, see the validation report, then the scores, the judge and the comparison. */
export function DeveloperUploads({ fetchFn = fetch, token, projectId, logicalId, onUploaded }: Common & { onUploaded?: () => void }) {
  const [name, setName] = useState("");
  const [version, setVersion] = useState("");
  const [notes, setNotes] = useState("");
  const [fileText, setFileText] = useState<string | null>(null);
  const [fileName, setFileName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [problems, setProblems] = useState<UploadReport | null>(null);
  const [last, setLast] = useState<UploadReport | null>(null);
  const [uploads, setUploads] = useState<UploadedMethod[]>([]);
  const [results, setResults] = useState<Record<string, RunResultsResponse>>({});
  const [reviews, setReviews] = useState<Record<string, RunReviews>>({});
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    void listUploads({ projectId, logicalId, token, fetchFn, signal: controller.signal }).then(async (res) => {
      if (controller.signal.aborted || !res.ok) return;
      setUploads(res.data.items);
      const next: Record<string, RunResultsResponse> = {};
      const nextReviews: Record<string, RunReviews> = {};
      for (const u of res.data.items) {
        const r = await getRunResults({ runId: u.run_id, token, fetchFn, signal: controller.signal });
        if (controller.signal.aborted) return;
        if (r.ok) next[u.run_id] = r.data;
        const rv = await getRunReviews({ runId: u.run_id, projectId, token, fetchFn, signal: controller.signal });
        if (controller.signal.aborted) return;
        if (rv.ok) nextReviews[u.run_id] = rv.data;          // gold-side: a role that may not read it simply gets none
      }
      setResults(next);
      setReviews(nextReviews);
    });
    return () => controller.abort();
  }, [projectId, logicalId, token, fetchFn, reload]);

  const pick = async (file: File | null) => {
    setFileText(null);
    setFileName(file?.name ?? "");
    if (file) setFileText(await file.text());
  };

  const send = async (acceptPartial: boolean) => {
    if (fileText === null || !name.trim() || busy) return;
    setBusy(true);
    setError(null);
    const res = await uploadAnswers({ projectId, logicalId, token, fetchFn, methodName: name, methodVersion: version, notes, fileContent: fileText, acceptPartial });
    setBusy(false);
    if (res.ok) {
      setProblems(null);
      setLast(res.data.report);
      setReload((n) => n + 1);
      onUploaded?.();
    } else {
      setLast(null);
      setProblems(res.report ?? null);
      setError(res.message);
    }
  };

  const rows = (list: UploadProblem[]) => list.map((p) => (
    <tr key={`${p.line}-${p.code}`}><td>第 {p.line} 行</td><td>{PROBLEM_TEXT[p.code] ?? p.code}</td></tr>
  ));

  return (
    <div data-testid="developer-uploads">
      <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", marginBottom: "0.5rem" }}>
        <div>
          <label htmlFor="dev-method-name" style={{ display: "block" }}>方法名稱 *</label>
          <input id="dev-method-name" type="text" value={name} maxLength={128} onChange={(e) => setName(e.target.value)} style={{ width: "220px" }} />
        </div>
        <div>
          <label htmlFor="dev-method-version" style={{ display: "block" }}>版本（選填）</label>
          <input id="dev-method-version" type="text" value={version} maxLength={128} onChange={(e) => setVersion(e.target.value)} style={{ width: "120px" }} />
        </div>
        <div>
          <label htmlFor="dev-method-notes" style={{ display: "block" }}>備註（選填）</label>
          <input id="dev-method-notes" type="text" value={notes} maxLength={2000} onChange={(e) => setNotes(e.target.value)} style={{ width: "260px" }} />
        </div>
      </div>
      <div style={{ marginBottom: "0.5rem" }}>
        <label htmlFor="dev-answers-file" style={{ display: "block" }}>開發人員回傳的答案檔（JSONL）</label>
        <input id="dev-answers-file" type="file" accept=".jsonl,.json,.txt,application/json,application/x-ndjson" onChange={(e) => void pick(e.target.files?.[0] ?? null)} />
        {fileName && <span className="muted small"> {fileName}</span>}
      </div>
      <button type="button" disabled={fileText === null || !name.trim() || busy} onClick={() => void send(false)}
        style={{ padding: "0.5rem 1.1rem", borderRadius: "8px", border: "1px solid var(--primary)", background: "var(--primary)", color: "#fff", fontWeight: 600, cursor: "pointer" }}>
        {busy ? "檢查並評分中…" : "上傳並評分"}
      </button>
      <span className="muted small"> 這一步不會呼叫模型、不花錢（答案品質的評分要另外按「用評分模型評分」）。</span>

      {error && <div role="alert" className="box box-error" data-testid="upload-error"><strong>沒有匯入：</strong> {error}</div>}
      {problems && (
        <div data-testid="upload-problems" style={{ marginTop: "0.5rem" }}>
          <p className="small">檢查報告：共 {problems.lines} 行，答案 {problems.answered} 題、棄答 {problems.abstained} 題、題目檔裡有 {problems.not_submitted} 題沒有可用的答案；
            <strong>有 {problems.problem_count} 行有問題</strong>：</p>
          <div className="table-wrap"><table className="data-table"><thead><tr><th>位置</th><th>問題</th></tr></thead><tbody>{rows(problems.problems)}</tbody></table></div>
          <p className="small">修正檔案後重新上傳，或只匯入沒有問題的行（有問題的行、沒有答案的題目會記成「未提交」，不會猜）：</p>
          <button type="button" className="secondary" disabled={busy || problems.answered + problems.abstained === 0} onClick={() => void send(true)}
            style={{ padding: "0.4rem 0.9rem", borderRadius: "8px", border: "1px solid var(--primary)", cursor: "pointer" }}>
            只匯入沒有問題的行
          </button>
        </div>
      )}
      {last && (
        <div data-testid="upload-report" className="box" style={{ background: "#e6f4ea", border: "1px solid #ceead6" }}>
          <p style={{ margin: 0 }}>已匯入並評分：答案 {last.answered} 題、棄答 {last.abstained} 題、<strong>未提交 {last.not_submitted} 題</strong>。</p>
          {last.source_checks && (
            <p style={{ margin: "0.25rem 0 0 0" }} data-testid="source-checks">
              引用檢查：{last.source_checks.total} 段引文，{last.source_checks.found} 段在原文裡找得到；
              {last.source_checks.not_found > 0 && <strong className="tone-bad"> {last.source_checks.not_found} 段在該頁找不到（可能是捏造或抄錯）</strong>}
              {last.source_checks.page_without_text > 0 && <span className="tone-wait"> {last.source_checks.page_without_text} 段所在的頁面系統沒有解析出文字</span>}
              {last.source_checks.not_found === 0 && last.source_checks.page_without_text === 0 && "，沒有找不到的。"}
            </p>
          )}
        </div>
      )}

      <h4 style={{ margin: "1rem 0 0.25rem" }}>已上傳的方法</h4>
      {uploads.length === 0 ? <p className="muted small">還沒有上傳任何答案檔。</p> : (
        <>
          <ComparisonTable uploads={uploads} results={results} reviews={reviews} />
          {uploads.map((u) => (
            <UploadCard key={u.run_id} u={u} result={results[u.run_id]} reviews={reviews[u.run_id]} common={{ fetchFn, token, projectId, logicalId }} onChanged={() => setReload((n) => n + 1)} />
          ))}
        </>
      )}
    </div>
  );
}

function meanText(r: RunResultsResponse | undefined, metric: string): { text: string; tone: string } {
  const m = r?.metrics.find((x) => x.metric_name === metric);
  if (!m) return { text: "—", tone: "none" };
  if (m.scored > 0 && m.mean !== null) return { text: m.mean.toFixed(1), tone: "good" };
  if (m.pending > 0) return { text: "尚未評分", tone: "wait" };
  if (m.error > 0) return { text: "錯誤", tone: "bad" };
  return { text: "—", tone: "none" };
}

/** Every uploaded method side by side, one column each, one row per metric: no total and no ranking, as in SPEC 9.1. */
function ComparisonTable({ uploads, results, reviews }: { uploads: UploadedMethod[]; results: Record<string, RunResultsResponse>; reviews: Record<string, RunReviews> }) {
  const anyReviewed = uploads.some((u) => (reviews[u.run_id]?.summary.questions_reviewed ?? 0) > 0);
  const graders = new Set(uploads.map((u) => u.grader_revision));
  return (
    <div data-testid="comparison">
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>指標（0–100）</th>
              {uploads.map((u) => <th key={u.run_id}>{u.method_name}{u.method_version ? ` ${u.method_version}` : ""}<div className="muted small">{u.judged ? "已用評分模型評分" : "還沒評分模型評分"}</div></th>)}
            </tr>
          </thead>
          <tbody>
            {METRICS.map(([key, label]) => (
              <tr key={key}>
                <td>{label}</td>
                {uploads.map((u) => { const c = meanText(results[u.run_id], key); return <td key={u.run_id} className={`tone-${c.tone}`}>{c.text}</td>; })}
              </tr>
            ))}
            {anyReviewed && (
              <tr data-testid="reviewed-correctness-row">
                <td>答案正確性（覆核後）<div className="muted small">覆核過的題用覆核值，其餘用評分模型的值</div></td>
                {uploads.map((u) => <td key={u.run_id}><ReviewedMean summary={reviews[u.run_id]?.summary} /></td>)}
              </tr>
            )}
            <tr>
              <td>未提交的題數</td>
              {uploads.map((u) => <td key={u.run_id}>{u.counts.not_submitted}</td>)}
            </tr>
            <tr>
              <td>沒被採信的引文（已從評分排除）</td>
              {uploads.map((u) => {
                const bad = u.source_checks ? u.source_checks.total - u.source_checks.found : 0;
                return <td key={u.run_id} className={bad > 0 ? "tone-bad" : undefined}>{u.source_checks ? `${bad} / ${u.source_checks.total}` : "—"}</td>;
              })}
            </tr>
          </tbody>
        </table>
      </div>
      <p className="muted small">
        每個指標各自比較，不加總、不排名。{graders.size > 1 ? "這些方法的評分狀態不同（有的已用評分模型評分、有的還沒），不同狀態的欄位不能直接比較。" : ""}
        在引用的頁面找不到的引文不會被評分模型看到（它只看系統自己解析出的原文），所以不會替答案加分；每個方法被排除了幾段，在上面那一列。
        答案品質的分數由同一家模型評出、沒有用人工標註校準，都是暫定的。
        {anyReviewed && "「覆核後」是另外一列：評分模型的分數沒有被改動；覆核值的來源（人工或模型協助）標在數字下面，模型協助的覆核不是人工覆核。"}
      </p>
    </div>
  );
}

const KIND_TEXT: Record<string, string> = { human: "人工覆核", model_assisted: "模型協助覆核" };

function ReviewedMean({ summary }: { summary?: RunReviews["summary"] }) {
  if (!summary || summary.questions_reviewed === 0 || summary.reviewed_mean === null) return <span className="tone-none">—</span>;
  const parts = [summary.reviewed_by.human > 0 ? `人工 ${summary.reviewed_by.human}` : "", summary.reviewed_by.model_assisted > 0 ? `模型協助 ${summary.reviewed_by.model_assisted}` : ""].filter(Boolean);
  return (
    <>
      <span className="tone-good">{summary.reviewed_mean.toFixed(1)}</span>
      <div className="muted small">覆核 {summary.questions_reviewed} 題（{parts.join("、")}）{summary.judge_mean !== null ? `；評分模型 ${summary.judge_mean.toFixed(1)}` : ""}</div>
    </>
  );
}

/** The reviewed correctness of one question next to the judge's value, and a small form so the owner can record or change it (Issue #218). */
function ReviewCell({ d, review, runId, common, onChanged }: { d: SubmissionDetailItem; review?: RunReviews["items"][number]; runId: string; common: Common; onChanged: () => void }) {
  const { fetchFn = fetch, token, projectId } = common;
  const [value, setValue] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const parsed = value.trim() === "" ? NaN : Number(value);
  const valid = Number.isFinite(parsed) && parsed >= 0 && parsed <= 100;
  const save = async () => {
    if (!valid || busy) return;
    setBusy(true);
    setErr(null);
    const res = await recordRunReviews({ runId, projectId, token, fetchFn, reviews: [{ question_id: d.question_id, value: parsed, reviewer_kind: "human", reviewer_label: "owner", ...(note.trim() ? { note: note.trim() } : {}) }] });
    setBusy(false);
    if (res.ok) { setValue(""); setNote(""); onChanged(); } else setErr(res.error.message);
  };
  return (
    <div data-testid={`review-${d.ordinal}`} style={{ marginTop: "0.25rem" }}>
      {review && (
        <div className="tone-good" data-testid={`review-value-${d.ordinal}`}>
          覆核後正確性：{review.value.toFixed(0)}
          <span className="muted"> （{KIND_TEXT[review.reviewer_kind]}，{review.reviewer_label}{review.versions > 1 ? `，第 ${review.versions} 次` : ""}）</span>
          {review.note && <div className="muted">{review.note}</div>}
        </div>
      )}
      <div style={{ display: "flex", gap: "0.25rem", alignItems: "center", flexWrap: "wrap" }}>
        <input type="number" min={0} max={100} step={1} value={value} aria-label={`第 ${d.ordinal + 1} 題的覆核分數（0–100）`} placeholder="覆核 0–100" onChange={(e) => setValue(e.target.value)} style={{ width: "6rem" }} />
        <input type="text" value={note} maxLength={500} aria-label={`第 ${d.ordinal + 1} 題的覆核備註`} placeholder="備註（選填）" onChange={(e) => setNote(e.target.value)} style={{ width: "9rem" }} />
        <button type="button" className="secondary" disabled={!valid || busy} onClick={() => void save()} style={{ padding: "0.2rem 0.6rem", borderRadius: "6px", border: "1px solid var(--primary)", cursor: "pointer" }}>
          記錄覆核
        </button>
      </div>
      {err && <div role="alert" className="tone-bad small">{err}</div>}
    </div>
  );
}

function UploadCard({ u, result, reviews, common, onChanged }: { u: UploadedMethod; result?: RunResultsResponse; reviews?: RunReviews; common: Common; onChanged: () => void }) {
  const { fetchFn = fetch, token, projectId } = common;
  const [est, setEst] = useState<RunEstimate | null>(null);
  const [judging, setJudging] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [detail, setDetail] = useState<SubmissionDetailItem[] | null>(null);
  const [showDetail, setShowDetail] = useState(false);

  useEffect(() => {
    if (u.judged) return;
    const controller = new AbortController();
    void getRunEstimate({ projectId, logicalId: common.logicalId, phase: "judge", runId: u.run_id, token, fetchFn, signal: controller.signal }).then((r) => {
      if (!controller.signal.aborted && r.ok) setEst(r.data);
    });
    return () => controller.abort();
  }, [u.run_id, u.judged, projectId, common.logicalId, token, fetchFn]);

  const judge = useCallback(async () => {
    setJudging(true);
    setMsg(null);
    const started = await startJudgeRun({ projectId, runId: u.run_id, token, fetchFn });
    if (!started.ok) {
      setJudging(false);
      setMsg(started.error.message);
      return;
    }
    const deadline = Date.now() + 600_000;
    while (Date.now() < deadline) {
      const job = await getJobStatus({ jobId: started.data.job_id, token, fetchFn });
      if (!job.ok) { setMsg(job.error.message); break; }
      if (["succeeded", "failed", "cancelled"].includes(job.data.status)) {
        if (job.data.status !== "succeeded") setMsg("評分沒有全部成功（例如預算用完）：沒評到的題目是「錯誤」並附原因，不是 0 分。");
        break;
      }
      await new Promise((r) => setTimeout(r, 3000));
    }
    setJudging(false);
    onChanged();
  }, [projectId, u.run_id, token, fetchFn, onChanged]);

  const toggle = async () => {
    const next = !showDetail;
    setShowDetail(next);
    if (next && detail === null) {
      const r = await getSubmissionDetails({ runId: u.run_id, projectId, token, fetchFn });
      if (r.ok) setDetail(r.data.items);
      else setMsg(r.error.message);
    }
  };

  const answered = u.counts.answered;
  return (
    <div className="box" style={{ border: "1px solid var(--border)", marginTop: "0.5rem" }} data-testid={`upload-${u.method_name}`}>
      <p style={{ margin: 0 }}>
        <strong>{u.method_name}</strong>{u.method_version ? ` ${u.method_version}` : ""}
        <span className="muted small"> ｜ {new Date(u.uploaded_at).toLocaleString()} ｜ 答案 {answered}、棄答 {u.counts.abstained}、未提交 {u.counts.not_submitted}
          {u.imported_only_valid_lines ? "｜只匯入了沒有問題的行" : ""} ｜ sha256 {u.file_sha256.slice(0, 12)}…</span>
      </p>
      {u.source_checks && u.source_checks.total - u.source_checks.found > 0 && (
        <p className="tone-bad small" style={{ margin: "0.25rem 0" }} data-testid="not-found-note">
          有 {u.source_checks.total - u.source_checks.found} 段引文沒有被採信（在它引用的那一頁找不到，或那一頁系統沒有解析出文字）：它們已從評分模型排除，
          不會替答案加分、反而算在引用精準度的分母裡；它們在檢索分數上只是「整頁命中」，不是找到了那段文字。
        </p>
      )}
      <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", flexWrap: "wrap", marginTop: "0.25rem" }}>
        <button type="button" disabled={judging || answered === 0} onClick={() => void judge()}
          style={{ padding: "0.4rem 0.9rem", borderRadius: "8px", border: "1px solid var(--primary)", background: "var(--primary)", color: "#fff", cursor: "pointer" }}>
          {judging ? "評分中…" : u.judged ? "再評分一次" : "用評分模型評分"}
        </button>
        <span className="small" style={{ color: "#c5221f", fontWeight: 700 }}>會呼叫真實模型、會花錢</span>
        {est && !u.judged && <span className="small" data-testid="judge-cost">最多約 {est.estimated_max_usd} 美元（{est.calls} 次呼叫）</span>}
        <button type="button" className="secondary" onClick={() => void toggle()}
          style={{ padding: "0.4rem 0.9rem", borderRadius: "8px", border: "1px solid var(--primary)", cursor: "pointer" }}>
          {showDetail ? "收起每一題" : "看每一題"}
        </button>
      </div>
      {result?.judge && (
        <p className="muted small" data-testid="judge-limit">
          限制：{result.judge.limitation}
          {result.source_checks && result.source_checks.excluded_from_judge > 0 && (
            <strong className="tone-bad" data-testid="judge-excluded"> ｜ {result.source_checks.excluded_from_judge} 段引文在引用的頁面找不到，已從評分排除（共 {result.source_checks.total} 段）</strong>
          )}
        </p>
      )}
      {msg && <div role="alert" className="box box-error">{msg}</div>}
      {showDetail && detail && (
        <div className="table-wrap" data-testid="upload-detail">
          <table className="data-table">
            <thead><tr><th>#</th><th>題目</th><th>他的答案與引用</th><th>標準答案（只有你看得到）</th><th>分數</th></tr></thead>
            <tbody>
              {detail.map((d) => (
                <tr key={d.question_id}>
                  <td>{d.ordinal + 1}</td>
                  <td>{d.question_text}</td>
                  <td>
                    {d.failure_reason === "not_submitted" ? <span className="tone-wait">這一題沒有答案（未提交）</span>
                      : d.status === "abstained" ? <span>棄答：{d.their_reason}</span> : <div>{d.their_answer}</div>}
                    {d.their_sources.map((s, i) => (
                      <div key={i} className="small" style={{ marginTop: "0.15rem" }}>
                        <span className="muted">第 {s.page} 頁：</span>{s.text} <QuoteBadge found={s.text_found} quality={s.match_quality} coverage={s.quote_coverage} />
                      </div>
                    ))}
                  </td>
                  <td className="small">{d.gold.reference_answer ?? "—"}</td>
                  <td className="small">
                    {["recall_at_k", "correctness"].map((k) => {
                      const sc = d.scores[k];
                      return <div key={k}>{k === "recall_at_k" ? "檢索命中" : "正確性"}：{sc ? (sc.status === "scored" && sc.value !== null ? `${sc.value.toFixed(0)}` : sc.status === "pending" ? "尚未評分" : sc.reason_codes[0] ?? sc.status) : "—"}</div>;
                    })}
                    <ReviewCell d={d} review={reviews?.items.find((r) => r.question_id === d.question_id)} runId={u.run_id} common={common} onChanged={onChanged} />
                    {downgradeNotes(d.scores.correctness?.reason_codes).length > 0 && (
                      <div data-testid={`downgrade-${d.ordinal}`} className="tone-wait" style={{ marginTop: "0.25rem" }}>
                        {downgradeNotes(d.scores.correctness?.reason_codes).map((line) => <div key={line}>{line}</div>)}
                        <div className="muted">{REPORT_HINT}</div>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
