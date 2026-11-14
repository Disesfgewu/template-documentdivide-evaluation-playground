import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Workflow } from "./Workflow";

afterEach(cleanup);

const U1 = "11111111-1111-1111-1111-111111111111";
const U2 = "22222222-2222-2222-2222-222222222222";
const U3 = "33333333-3333-3333-3333-333333333333";
const U4 = "44444444-4444-4444-4444-444444444444";
const RUN = "55555555-5555-5555-5555-555555555555";
const METHOD_JOB = "66666666-6666-6666-6666-666666666666";
const JUDGE_JOB = "77777777-7777-7777-7777-777777777777";
const SHA = "a".repeat(64);
const TOKEN = `lab_act_${U1}_${SHA}`;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

type Opts = { judgeStatus?: number; judgeJob?: string; judged?: boolean; submissions?: Record<string, unknown>; genJob?: Record<string, unknown> };

function backend(opts: Opts = {}) {
  const calls: { url: string; method: string; body?: string }[] = [];
  const f = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const u = String(url);
    const method = init?.method || "GET";
    calls.push({ url: u, method, body: init?.body as string | undefined });
    if (u.includes("/api/v1/documents?") && method === "POST") {
      return json({ id: U2, document_version_id: U2, project_id: U1, resource_id: U3, artifact_id: U4, logical_id: "d", revision: 1, sha256: SHA, size_bytes: 10, page_count: 6 }, 201);
    }
    if (u.includes("/parse") && method === "POST") return json({ job_id: U4, status: "queued" }, 202);
    if (u.includes(`/api/v1/jobs/${U4}`)) return json({ job_id: U4, status: "succeeded", stage: "done", attempt: 1 });
    if (u.includes(`/api/v1/jobs/${METHOD_JOB}`)) return json({ job_id: METHOD_JOB, status: "succeeded", stage: "done", attempt: 1 });
    if (u.includes(`/api/v1/jobs/${JUDGE_JOB}`)) return json({ job_id: JUDGE_JOB, status: opts.judgeJob ?? "succeeded", stage: "done", attempt: 1 });
    if (u.includes("/api/v1/benchmarks?") && method === "POST") {
      return json({ benchmark_revision_id: U1, logical_id: "b", revision: 1, status: "parsing", document_version_id: U2, n_target: 20, q_min: 2 }, 201);
    }
    if (u.includes("/plan") && method === "POST") {
      return json({ benchmark_revision_id: U1, logical_id: "b", revision: 1, status: "planning", parse_job_id: U4, pages: { usable: [0, 1], insufficient_material: {} }, compliance_projection: "partial" });
    }
    if (u.includes("/generate") && method === "POST") return json({ job_id: U2, status: "queued", phase: "generate" }, 202);
    if (u.includes("/estimate?")) {
      const q = new URL(u, "http://x").searchParams;
      const n = q.get("question_count") ? Number(q.get("question_count")) : 6;
      return json({ phase: q.get("phase"), calls: n, estimated_max_micros: 50_000, estimated_max_usd: "0.0500", currency: "USD", ceiling_usd: "5.0000",
        question_count: n, plan_total: 6, split: { general: n - 2 > 0 ? n - 2 : n, cross_page: n - 2 > 0 ? 1 : 0, confusable: n - 2 > 0 ? 1 : 0 } });
    }
    if (u.includes("/generate") && method === "GET") {
      return json({ benchmark_revision_id: U1, logical_id: "b", revision: 1, benchmark_status: "validating", general_target: 5, cross_page_target: 2, confusable_target: 2,
        questions: 6, by_bucket: { general: 4, cross_page: 1, confusable: 1 }, shortfall: 3, job: opts.genJob ?? { status: "succeeded", phase: "validate" },
        validation: { pass: 6, fail: 0, needs_review: 0, unvalidated: 0 } });
    }
    if (u.includes("/validate") && method === "POST") return json({ job_id: U3, status: "queued", phase: "validate" }, 202);
    if (u.includes("/freeze") && method === "POST") return json({ benchmark_revision_id: U1, logical_id: "b", revision: 1, status: "frozen", compliance: "partial", limits: ["PARTIAL"] });
    if (u.includes("/baseline-run") && method === "POST") return json({ run_id: RUN, evaluation_id: U4, method: "local-lexical-baseline", status: "completed", evaluation_status: "provisional", questions: 6, limits: [] }, 201);
    if (u.includes("/method-runs") && method === "POST") return json({ run_id: RUN, job_id: METHOD_JOB, status: "queued" }, 202);
    if (u.includes(`/api/v1/runs/${RUN}/judge-runs`) && method === "POST") {
      if (opts.judgeStatus && opts.judgeStatus !== 202) return json({ error: { code: "conflict", message: "the judge run cannot start: the_run_has_no_answered_submission_to_judge" } }, opts.judgeStatus);
      return json({ run_id: RUN, job_id: JUDGE_JOB, status: "queued", answered_submissions: 5 }, 202);
    }
    if (u.includes(`/api/v1/runs/${RUN}/results`)) {
      const judged = opts.judged ?? calls.some((c) => c.url.includes("/judge-runs"));
      return json({
        run_id: RUN, evaluation: { id: U4, grader_revision: judged ? "rule-based-scorer-1+llm-judge-1" : "rule-based-scorer-1", status: "provisional" }, provisional: true,
        metrics: [{ metric_name: "correctness", scored: judged ? 5 : 0, not_applicable: 0, pending: judged ? 0 : 5, error: 0, mean: judged ? 80 : null }],
        submissions: opts.submissions ?? { answered: 5, abstained_by_model: 0, method_failures: {} },
        excluded_questions: [{ ordinal: 3, reason: "不合格" }],
        judge: judged ? { judged_questions: 5, questions_with_a_contradicted_fact: 1, model: "claude-haiku-4-5-20251001", prompt_revision: "judge-v1",
          limitation: "Judged by the same provider/model family that generated the questions; uncalibrated; provisional." } : null,
      });
    }
    return json({ error: { message: "not found" } }, 404);
  });
  return { f: f as unknown as typeof fetch, calls };
}

async function runUntilFrozen(f: typeof fetch) {
  render(<Workflow fetchFn={f} />);
  fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: TOKEN } });
  fireEvent.change(screen.getByLabelText(/Project ID/i), { target: { value: U1 } });
  fireEvent.change(screen.getByLabelText(/選擇 PDF 檔案/i), { target: { files: [new File(["%PDF-1.4"], "a.pdf", { type: "application/pdf" })] } });
  fireEvent.click(screen.getByRole("button", { name: "上傳 PDF" }));
  await waitFor(() => expect(screen.getByTestId("upload-result")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "開始解析" }));
  await waitFor(() => expect(screen.getByTestId("step3-status").textContent).toBe("succeeded"));
  fireEvent.change(screen.getByLabelText(/Benchmark Logical ID/i), { target: { value: "b" } });
  fireEvent.click(screen.getByRole("button", { name: "建立 Benchmark 與素材計畫" }));
  await waitFor(() => expect(screen.getByText("素材計畫建立成功！")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "開始生成題目" }));
  await waitFor(() => expect(screen.getByText("題目生成完成！")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "開始自動驗題" }));
  await waitFor(() => expect(screen.getByText("驗證結果題數統計：")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "凍結 Benchmark" }));
  await waitFor(() => expect(screen.getByText(/Benchmark 成功凍結/i)).toBeTruthy());
}

const judgeButton = () => screen.getByRole("button", { name: "用評分模型評分" }) as HTMLButtonElement;

describe("Workflow: the judge step", () => {
  it("is disabled until a model answering run exists and says why; a lexical baseline cannot be judged", async () => {
    const { f, calls } = backend();
    await runUntilFrozen(f);
    expect(judgeButton().disabled).toBe(true);
    expect(screen.getByTestId("judge-help").textContent).toContain("先在步驟 8");
    fireEvent.click(screen.getByRole("button", { name: "詞彙 baseline" }));
    await waitFor(() => expect(screen.getByTestId("results-table")).toBeTruthy());
    expect(judgeButton().disabled).toBe(true);
    expect(screen.getByTestId("judge-help").textContent).toContain("沒有模型答案，不能評分");
    expect(calls.some((c) => c.url.includes("/judge-runs"))).toBe(false);
  });

  it("judges a finished model run, polls the job and shows the judged results with the limitation and the exclusion note", async () => {
    const { f, calls } = backend();
    await runUntilFrozen(f);
    fireEvent.click(screen.getByRole("button", { name: "模型回答方法" }));
    await waitFor(() => expect(screen.getByTestId("results-table")).toBeTruthy());
    expect(screen.queryByTestId("judge-summary")).toBeNull();                       // not judged yet: nothing is shown as judged
    await waitFor(() => expect(judgeButton().disabled).toBe(false));
    fireEvent.click(judgeButton());
    await waitFor(() => expect(screen.getByTestId("judge-summary")).toBeTruthy(), { timeout: 8000 });
    const post = calls.find((c) => c.url.includes("/judge-runs"))!;
    expect(post.url).toContain(`/api/v1/runs/${RUN}/judge-runs?project_id=${U1}`);
    expect(JSON.parse(post.body!)).toEqual({ model: "claude-haiku-4-5-20251001" });                 // no price: the API derives the cap
    const summary = screen.getByTestId("judge-summary").textContent ?? "";
    expect(summary).toContain("claude-haiku-4-5-20251001");
    expect(summary).toContain("評了 5 題");
    expect(summary).toContain("1 題有被判定為與標準答案相反");
    expect(summary).toContain("same provider/model family");
    expect(screen.getByTestId("excluded-note").textContent).toContain("已排除 1 題");
    expect(screen.getByTestId("results-table").textContent).toContain("80.00");
  }, 20000);

  it("a refused judge run (409) is shown as the backend's honest message, not as a success", async () => {
    const { f } = backend({ judgeStatus: 409 });
    await runUntilFrozen(f);
    fireEvent.click(screen.getByRole("button", { name: "模型回答方法" }));
    await waitFor(() => expect(judgeButton().disabled).toBe(false));
    fireEvent.click(judgeButton());
    const alert = await screen.findByText(/目前不能評分/);
    expect(alert.parentElement?.textContent).toContain("no_answered_submission");
    expect(screen.queryByTestId("judge-summary")).toBeNull();
  }, 20000);

  it("a judge job that fails still shows the evaluation it wrote, with the reason, and never as a success", async () => {
    const { f } = backend({ judgeJob: "failed", judged: true });
    await runUntilFrozen(f);
    fireEvent.click(screen.getByRole("button", { name: "模型回答方法" }));
    await waitFor(() => expect(judgeButton().disabled).toBe(false));
    fireEvent.click(judgeButton());
    expect((await screen.findByText(/評分任務沒有全部成功/, {}, { timeout: 8000 })).textContent).toContain("不是 0 分");
    expect(screen.queryByText("評分失敗：")).not.toBeNull();
  }, 20000);

  it("the token never appears in the page after a judge error", async () => {
    const { f } = backend({ judgeStatus: 409 });
    await runUntilFrozen(f);
    fireEvent.click(screen.getByRole("button", { name: "模型回答方法" }));
    await waitFor(() => expect(judgeButton().disabled).toBe(false));
    fireEvent.click(judgeButton());
    await screen.findByText(/目前不能評分/);
    expect(document.body.textContent).not.toContain(TOKEN);
  }, 20000);
});

describe("Workflow: how the submissions of a run are described", () => {
  it("a retrieval-only run says it produced no answers instead of blaming a model's abstention", async () => {
    const { f } = backend({ submissions: { answered: 0, abstained_by_model: 0, no_answer_retrieval_only: 9, method_failures: {} } });
    await runUntilFrozen(f);
    fireEvent.click(screen.getByRole("button", { name: "詞彙 baseline" }));
    await waitFor(() => expect(screen.getByTestId("no-answer-note")).toBeTruthy());
    expect(screen.getByTestId("no-answer-note").textContent).toContain("無答案");
    expect(screen.getByTestId("no-answer-note").textContent).toContain("9 筆");
    expect(screen.getByTestId("results-table").parentElement?.textContent).toContain("模型棄權 0 筆");
  }, 20000);

  it("counts failed SUBMISSIONS, not the number of failure codes", async () => {
    const { f } = backend({ submissions: { answered: 4, abstained_by_model: 1, no_answer_retrieval_only: 0, method_failures: { budget_exhausted: 3, provider_error: 2 } } });
    await runUntilFrozen(f);
    fireEvent.click(screen.getByRole("button", { name: "模型回答方法" }));
    await waitFor(() => expect(screen.getByTestId("results-table")).toBeTruthy());
    expect(screen.getByTestId("results-table").parentElement?.textContent).toContain("方法失敗 5 筆");
    expect(screen.queryByTestId("no-answer-note")).toBeNull();
  }, 20000);

  it("the results header says the unit of the means", async () => {
    const { f } = backend();
    await runUntilFrozen(f);
    fireEvent.click(screen.getByRole("button", { name: "詞彙 baseline" }));
    await waitFor(() => expect(screen.getByTestId("results-table")).toBeTruthy());
    expect(screen.getByTestId("results-table").textContent).toContain("0–100");
  }, 20000);
});

describe("Workflow: the owner sets HOW MANY questions, never a price", () => {
  async function toPlan(f: typeof fetch) {
    render(<Workflow fetchFn={f} />);
    fireEvent.change(screen.getByLabelText(/認證 Token/i), { target: { value: TOKEN } });
    fireEvent.change(screen.getByLabelText(/Project ID/i), { target: { value: U1 } });
    fireEvent.change(screen.getByLabelText(/選擇 PDF 檔案/i), { target: { files: [new File(["%PDF-1.4"], "a.pdf", { type: "application/pdf" })] } });
    fireEvent.click(screen.getByRole("button", { name: "上傳 PDF" }));
    await waitFor(() => expect(screen.getByTestId("upload-result")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "開始解析" }));
    await waitFor(() => expect(screen.getByTestId("step3-status").textContent).toBe("succeeded"));
    fireEvent.change(screen.getByLabelText(/Benchmark Logical ID/i), { target: { value: "b" } });
    fireEvent.click(screen.getByRole("button", { name: "建立 Benchmark 與素材計畫" }));
    await waitFor(() => expect(screen.getByText("素材計畫建立成功！")).toBeTruthy());
  }

  it("the count defaults to the plan's total, the split and the USD worst case are shown before starting, and no micro-USD field exists", async () => {
    const { f } = backend();
    await toPlan(f);
    const count = (await screen.findByLabelText("要出幾題")) as HTMLInputElement;
    await waitFor(() => expect(count.value).toBe("6"));
    expect(screen.getByText(/這份文件建議 6 題/)).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("gen-estimate").textContent).toContain("0.0500 美元"));
    expect(screen.getByTestId("gen-estimate").textContent).toContain("6 次呼叫");
    expect(screen.getByTestId("gen-split").textContent).toContain("一般");
    expect(document.body.textContent).not.toContain("micro-USD");
    expect(screen.queryByLabelText(/預算/)).toBeNull();
  }, 20000);

  it("asking for fewer questions re-estimates and sends question_count without any budget", async () => {
    const { f, calls } = backend();
    await toPlan(f);
    const count = (await screen.findByLabelText("要出幾題")) as HTMLInputElement;
    await waitFor(() => expect(count.value).toBe("6"));
    fireEvent.change(count, { target: { value: "3" } });
    await waitFor(() => expect(calls.some((c) => c.url.includes("/estimate?") && c.url.includes("question_count=3"))).toBe(true));
    await waitFor(() => expect(screen.getByTestId("gen-estimate").textContent).toContain("3 次呼叫"));
    fireEvent.click(screen.getByRole("button", { name: "開始生成題目" }));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/generate?") && c.method === "POST")).toBe(true));
    const post = calls.find((c) => c.url.includes("/generate?") && c.method === "POST")!;
    expect(JSON.parse(post.body!)).toEqual({ model: "claude-haiku-4-5-20251001", question_count: 3 });
  }, 20000);

  it("a count that is not a whole number blocks the button instead of sending something odd", async () => {
    const { f } = backend();
    await toPlan(f);
    const count = (await screen.findByLabelText("要出幾題")) as HTMLInputElement;
    await waitFor(() => expect(count.value).toBe("6"));
    fireEvent.change(count, { target: { value: "0" } });
    expect((screen.getByRole("button", { name: "開始生成題目" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/題數要是 1 以上的整數/)).toBeTruthy();
  }, 20000);

  it("a failed generation says why in words, not just 'failed'", async () => {
    const { f } = backend({ genJob: { status: "failed", phase: "generate", failure_reason: "budget_exhausted" } });
    await toPlan(f);
    await waitFor(() => expect((screen.getByLabelText("要出幾題") as HTMLInputElement).value).toBe("6"));
    fireEvent.click(screen.getByRole("button", { name: "開始生成題目" }));
    const alert = await screen.findByText(/預算不夠/, {}, { timeout: 8000 });
    expect(alert.parentElement?.textContent).toContain("請減少題數");
    expect(alert.parentElement?.textContent).not.toContain("結束狀態：failed");
  }, 20000);

  it("the validation, answering and judging steps show their worst case in USD and no price field", async () => {
    const { f } = backend();
    await runUntilFrozen(f);
    expect(screen.getByTestId("validate-explain").textContent).toContain("光看那段原文");
    await waitFor(() => expect(screen.getByTestId("method-estimate").textContent).toContain("0.0500 美元"));
    fireEvent.click(screen.getByRole("button", { name: "模型回答方法" }));
    await waitFor(() => expect(judgeButton().disabled).toBe(false));
    await waitFor(() => expect(screen.getByTestId("judge-estimate").textContent).toContain("美元"));
    expect(document.body.textContent).not.toContain("micro-USD");
  }, 20000);
});
