import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { DeveloperPackage, DeveloperUploads } from "./ExternalMethods";
import type { UploadedMethod } from "./workflowApi";
import { Workflow } from "./Workflow";

afterEach(cleanup);

const P = "11111111-1111-1111-1111-111111111111";
const RUN1 = "55555555-5555-5555-5555-555555555551";
const RUN2 = "55555555-5555-5555-5555-555555555552";
const JOB = "77777777-7777-7777-7777-777777777777";
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const REPORT = { file_sha256: "a".repeat(64), lines: 20, questions_in_benchmark: 18, answered: 15, abstained: 1, not_submitted: 2, problems: [], problem_count: 0,
  source_checks: { total: 15, found: 12, not_found: 2, page_without_text: 1 } };
const FORMAT = { format: "lab-submission-1", description: ["每行一個 JSON 物件", "page 從 1 開始"], example_lines: ['{"method_name": "my-rag"}', '{"question_id": "q-1", "status": "answered"}'],
  line_schema: { type: "object" }, header_schema: { type: "object" } };

function upload(over: Partial<UploadedMethod> = {}, judged = false): UploadedMethod {
  return { run_id: RUN1, method_name: "alpha", method_version: "1", notes: null, file_sha256: "b".repeat(64), uploaded_at: "2026-11-15T10:00:00Z", imported_only_valid_lines: false,
    run_status: "completed_with_errors", evaluation_id: "e1", grader_revision: judged ? "rule-based-scorer-1+llm-judge-1" : "rule-based-scorer-1", judged,
    counts: { answered: 15, abstained: 1, not_submitted: 2, problem_count: 0 }, source_checks: { total: 15, found: 12, not_found: 2, page_without_text: 1 }, ...over };
}
const RUNS: UploadedMethod[] = [upload()];

function results(mean: number | null, judged: boolean) {
  const pending = (name: string) => ({ metric_name: name, scored: 0, not_applicable: 0, pending: 16, error: 2, mean: null });
  return { run_id: RUN1, evaluation: { id: "e1", grader_revision: "x", status: "provisional" }, provisional: true,
    metrics: [{ metric_name: "recall_at_k", scored: 18, not_applicable: 0, pending: 0, error: 0, mean: 80 },
      judged ? { metric_name: "correctness", scored: 16, not_applicable: 0, pending: 0, error: 2, mean } : pending("correctness")],
    judge: judged ? { judged_questions: 16, questions_with_a_contradicted_fact: 0, model: "m", prompt_revision: "judge-v1", limitation: "same provider/model family; uncalibrated; provisional" } : null };
}

type Calls = { url: string; method: string; body?: string; auth: string | null }[];
function backend(opts: { uploads?: unknown[]; uploadReply?: (body: Record<string, unknown>) => Response; judgedRuns?: string[] } = {}) {
  const calls: Calls = [];
  const f = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const u = String(url);
    const method = init?.method || "GET";
    calls.push({ url: u, method, body: init?.body as string | undefined, auth: new Headers(init?.headers).get("Authorization") });
    if (u.includes("/questions/export")) {
      const jsonl = u.includes("format=jsonl");
      return new Response(jsonl ? '{"type":"benchmark"}\n' : "{}", { status: 200, headers: { "Content-Disposition": `attachment; filename="bench-r1-questions.${jsonl ? "jsonl" : "json"}"` } });
    }
    if (u.endsWith("/api/v1/submission-format")) return json(FORMAT);
    if (u.includes("/submissions?") && method === "POST") return opts.uploadReply ? opts.uploadReply(JSON.parse(String(init?.body))) : json({ run_id: RUN1, evaluation_id: "e1", method_name: "alpha", method_revision: 1, status: "completed", evaluation_status: "provisional", report: REPORT, limits: [] }, 201);
    if (u.includes("/submissions?") && method === "GET") return json({ benchmark_status: "frozen", items: opts.uploads ?? RUNS });
    if (u.includes("/judge-runs") && method === "POST") return json({ run_id: RUN1, job_id: JOB, status: "queued", answered_submissions: 15 }, 202);
    if (u.includes(`/api/v1/jobs/${JOB}`)) return json({ job_id: JOB, status: "succeeded", stage: "done", attempt: 1 });
    if (u.includes("/estimate?")) return json({ phase: "judge", calls: 15, estimated_max_micros: 30000, estimated_max_usd: "0.0300", currency: "USD", ceiling_usd: "5.0000" });
    if (u.includes("/submission-details")) {
      return json({ run_id: RUN1, method_name: "alpha", items: [
        { question_id: "q-1", ordinal: 0, question_text: "年費多少？", status: "answered", failure_reason: null, their_answer: "120 EUR", their_reason: null,
          their_sources: [{ page: 3, text: "年費為 120 EUR", match_quality: "exact_in_unit", text_found: true, quote_coverage: 1 },
                          { page: 2, text: "完全捏造的一句話", match_quality: "not_found", text_found: false, quote_coverage: 0.25 }],
          scores: { recall_at_k: { status: "scored", value: 100, reason_codes: [] }, correctness: { status: "scored", value: 0, reason_codes: ["judge_overruled_by_number_check", "fact_downgraded:f1:number_absent"] } },
          gold: { reference_answer: "120 EUR 五月到期", required_facts: [{ statement: "120 EUR" }] } },
        { question_id: "q-2", ordinal: 1, question_text: "到期日？", status: "abstained", failure_reason: "not_submitted", their_answer: null, their_reason: "method_failure:not_submitted",
          their_sources: [], scores: {}, gold: { reference_answer: "五月", required_facts: [] } }] });
    }
    for (const run of [RUN1, RUN2]) {
      if (u.includes(`/api/v1/runs/${run}/results`)) {
        const judged = (opts.judgedRuns ?? []).includes(run) || calls.some((c) => c.url.includes("/judge-runs"));
        return json({ ...results(run === RUN1 ? 75 : 60, judged), run_id: run,
          source_checks: { total: 15, found: 12, not_found: 2, page_without_text: 1, excluded_from_judge: 3, page_only_retrieval_credit: 3, note: "x" } });
      }
    }
    return json({ error: { message: "not found" } }, 404);
  });
  return { f: f as unknown as typeof fetch, calls };
}

const common = (f: typeof fetch) => ({ fetchFn: f, token: "tok", projectId: P, logicalId: "bench" });

describe("DeveloperPackage (step 8)", () => {
  it("downloads the questions-only file through the authorised API and says it holds no answers; the format description and example are shown", async () => {
    const { f, calls } = backend();
    const saved: string[] = [];
    const done = vi.fn();
    render(<DeveloperPackage {...common(f)} triggerSave={(_b, n) => saved.push(n)} onDownloaded={done} />);
    await waitFor(() => expect(screen.getByTestId("format-doc")).toBeTruthy());
    expect(screen.getByTestId("developer-package").textContent).toContain("只有題目文字");
    expect(screen.getByTestId("format-doc").textContent).toContain("page 從 1 開始");
    expect(screen.getByTestId("format-doc").textContent).toContain('"method_name": "my-rag"');
    fireEvent.click(screen.getByRole("button", { name: "下載題目檔（JSONL）" }));
    await waitFor(() => expect(saved).toEqual(["bench-r1-questions.jsonl"]));
    const get = calls.find((c) => c.url.includes("/questions/export"))!;
    expect(get.url).toContain(`/api/v1/benchmarks/bench/revisions/1/questions/export?project_id=${P}&format=jsonl`);
    expect(get.auth).toBe("Bearer tok");
    expect(done).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "下載答案檔格式說明（含 JSON schema）" }));
    expect(saved[1]).toBe("answers-file-format.json");
  });

  it("a refused download is an error and saves nothing", async () => {
    const f = vi.fn(async (url: RequestInfo | URL) => String(url).includes("/questions/export") ? json({ error: { message: "the questions package is only available once the benchmark is frozen" } }, 409)
      : json(FORMAT)) as unknown as typeof fetch;
    const saved: string[] = [];
    render(<DeveloperPackage {...common(f)} triggerSave={(_b, n) => saved.push(n)} />);
    fireEvent.click(await screen.findByRole("button", { name: "下載題目檔（JSON）" }));
    expect((await screen.findByRole("alert")).textContent).toContain("only available once");
    expect(saved).toEqual([]);
  });
});

async function pickFile(text: string) {
  const file = new File([text], "answers.jsonl", { type: "application/x-ndjson" });
  fireEvent.change(screen.getByLabelText(/開發人員回傳的答案檔/), { target: { files: [file] } });
  await waitFor(() => expect((screen.getByRole("button", { name: "上傳並評分" }) as HTMLButtonElement).disabled).toBe(true)); // still no name
}

describe("DeveloperUploads (step 9)", () => {
  it("needs a method name and a file, uploads the file text, and reports what was imported and which quotes were not found", async () => {
    const { f, calls } = backend({ uploads: [] });
    const done = vi.fn();
    render(<DeveloperUploads {...common(f)} onUploaded={done} />);
    expect(screen.getByText("還沒有上傳任何答案檔。")).toBeTruthy();
    await pickFile('{"question_id": "q-1"}\n');
    fireEvent.change(screen.getByLabelText(/方法名稱/), { target: { value: "alpha" } });
    fireEvent.change(screen.getByLabelText(/版本/), { target: { value: "1" } });
    await waitFor(() => expect((screen.getByRole("button", { name: "上傳並評分" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "上傳並評分" }));
    await waitFor(() => expect(screen.getByTestId("upload-report")).toBeTruthy());
    const post = calls.find((c) => c.method === "POST")!;
    expect(JSON.parse(post.body!)).toEqual({ method_name: "alpha", file_content: '{"question_id": "q-1"}\n', accept_partial: false, method_version: "1" });
    expect(post.auth).toBe("Bearer tok");
    const text = screen.getByTestId("upload-report").textContent ?? "";
    expect(text).toContain("未提交 2 題");
    expect(screen.getByTestId("source-checks").textContent).toContain("2 段在該頁找不到");
    expect(screen.getByTestId("source-checks").textContent).toContain("1 段所在的頁面系統沒有解析出文字");
    expect(done).toHaveBeenCalled();
  });

  it("a file with problems lists them by line in words, imports nothing, and only imports the valid lines when the owner says so", async () => {
    const problemReport = { ...REPORT, problems: [{ line: 3, code: "invalid_json" }, { line: 7, code: "page_out_of_range" }, { line: 9, code: "unknown_question" }], problem_count: 3 };
    let calls = 0;
    const { f, calls: log } = backend({ uploadReply: (body) => {
      calls += 1;
      return body.accept_partial ? json({ run_id: RUN1, evaluation_id: "e", method_name: "alpha", method_revision: 1, status: "completed_with_errors", evaluation_status: "provisional", report: problemReport, limits: [] }, 201)
        : json({ error: { code: "submission_rejected", message: "the upload was not imported: the_file_has_problems_nothing_was_imported", details: { report: problemReport } } }, 422);
    } });
    render(<DeveloperUploads {...common(f)} />);
    await pickFile("x\n");
    fireEvent.change(screen.getByLabelText(/方法名稱/), { target: { value: "alpha" } });
    await waitFor(() => expect((screen.getByRole("button", { name: "上傳並評分" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "上傳並評分" }));
    await waitFor(() => expect(screen.getByTestId("upload-problems")).toBeTruthy());
    const t = screen.getByTestId("upload-problems").textContent ?? "";
    for (const part of ["第 3 行", "這一行不是合法的 JSON", "第 7 行", "超出這份 PDF 的頁數", "第 9 行", "題目檔裡沒有這個 question_id", "有 3 行有問題"]) expect(t).toContain(part);
    expect(screen.getByTestId("upload-error").textContent).toContain("沒有匯入");
    expect(screen.queryByTestId("upload-report")).toBeNull();
    expect(calls).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "只匯入沒有問題的行" }));
    await waitFor(() => expect(screen.getByTestId("upload-report")).toBeTruthy());
    expect(JSON.parse(log.filter((c) => c.method === "POST")[1].body!).accept_partial).toBe(true);
    expect(screen.queryByTestId("upload-problems")).toBeNull();
  });

  it("two uploaded methods are compared side by side per metric, with no total, and a note when their scoring state differs", async () => {
    const uploads = [upload({ run_id: RUN1, method_name: "alpha" }, true), upload({ run_id: RUN2, method_name: "beta", counts: { answered: 18, abstained: 0, not_submitted: 0, problem_count: 0 }, source_checks: { total: 18, found: 18, not_found: 0, page_without_text: 0 } })];
    const { f } = backend({ uploads, judgedRuns: [RUN1] });
    render(<DeveloperUploads {...common(f)} />);
    await waitFor(() => expect(screen.getByTestId("comparison")).toBeTruthy());
    await waitFor(() => expect(screen.getByTestId("comparison").textContent).toContain("75.0"));
    const table = screen.getByTestId("comparison").textContent ?? "";
    expect(table).toContain("alpha 1");
    expect(table).toContain("beta 1");
    expect(table).toContain("已用評分模型評分");
    expect(table).toContain("還沒評分模型評分");
    expect(table).toContain("尚未評分");                     // beta has no judge scores: pending, never a number
    expect(table).toContain("3 / 15");                       // alpha: 2 not found + 1 on a page without text = 3 sources the judge never sees
    expect(table).toContain("0 / 18");
    expect(table).toContain("沒被採信的引文（已從評分排除）");
    expect(table).toContain("不會被評分模型看到");
    expect(table).toContain("不加總、不排名");
    expect(table).toContain("不同狀態的欄位不能直接比較");
    expect(table).not.toMatch(/總分|排名第/);
  });

  it("a method can be judged from its card: the worst-case cost is shown, the limitation is stated, and each question shows their quotes with the fabricated one flagged", async () => {
    const { f, calls } = backend({ uploads: [upload()] });
    render(<DeveloperUploads {...common(f)} />);
    await waitFor(() => expect(screen.getByTestId("judge-cost").textContent).toContain("0.0300 美元"));
    expect(screen.getAllByText("會呼叫真實模型、會花錢").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "看每一題" }));
    await waitFor(() => expect(screen.getByTestId("upload-detail")).toBeTruthy());
    const d = screen.getByTestId("upload-detail").textContent ?? "";
    for (const part of ["120 EUR", "年費為 120 EUR", "原文裡找得到", "完全捏造的一句話", "在這一頁找不到這段引文", "只有 25% 的字詞出現在該頁", "這一題沒有答案（未提交）", "120 EUR 五月到期"]) expect(d).toContain(part);
    expect(screen.getAllByTestId("quote-not-found").length).toBe(1);
    // a score that is lower than the judge's own verdict says why, in words, with the request to report it (#197)
    expect(screen.getByTestId("downgrade-0").textContent).toContain("要點 1：評分模型判定有說到，但程式檢查「找不到要點裡的數字」而降級");
    expect(screen.getByTestId("downgrade-0").textContent).toContain("請把這一題回報");
    expect(screen.queryByTestId("downgrade-1")).toBeNull();                                  // a question without a downgrade shows no note
    fireEvent.click(screen.getByRole("button", { name: "用評分模型評分" }));
    await waitFor(() => expect(screen.getByTestId("judge-limit")).toBeTruthy(), { timeout: 8000 });
    expect(screen.getByTestId("judge-limit").textContent).toContain("uncalibrated");
    expect(screen.getByTestId("judge-excluded").textContent).toContain("3 段引文在引用的頁面找不到，已從評分排除（共 15 段）");
    expect(screen.getByTestId("not-found-note").textContent).toContain("已從評分模型排除");
    expect(screen.getByTestId("not-found-note").textContent).toContain("整頁命中");
    expect(JSON.parse(calls.find((c) => c.url.includes("/judge-runs"))!.body!)).toEqual({ model: "claude-haiku-4-5-20251001" });   // no price: derived
  }, 20000);

  it("a judged run with error rows offers to judge ONLY those again, and sends the flag; without errors there is no such button", async () => {
    const { f, calls } = backend({ uploads: [upload({}, true)], judgedRuns: [RUN1] });
    render(<DeveloperUploads {...common(f)} />);
    const button = await screen.findByTestId("rejudge-errors");
    expect(button.textContent).toContain("只重評出錯的題（2 題）");          // results() gives the judged correctness row 2 errors
    fireEvent.click(button);
    await waitFor(() => expect(calls.some((c) => c.url.includes("/judge-runs"))).toBe(true));
    expect(JSON.parse(calls.find((c) => c.url.includes("/judge-runs"))!.body!)).toEqual({ model: "claude-haiku-4-5-20251001", judge_errors_only: true });
  }, 20000);

  it("a run that was never judged has no 'judge only the errors again' button", async () => {
    const { f } = backend({ uploads: [upload({}, false)] });
    render(<DeveloperUploads {...common(f)} />);
    await waitFor(() => expect(screen.getByTestId("judge-cost")).toBeTruthy());
    expect(screen.queryByTestId("rejudge-errors")).toBeNull();
  });
});

describe("Workflow: the real flow comes first, the system's own methods are an optional section", () => {
  it("shows step 8 and 9 for developers and puts the built-in methods in a labelled reference section", () => {
    render(<Workflow fetchFn={backend().f} />);
    expect(screen.getByText("步驟 8: 給開發人員（下載題目檔）")).toBeTruthy();
    expect(screen.getByText("步驟 9: 上傳開發人員的答案")).toBeTruthy();
    const ref = screen.getByTestId("reference-baselines");
    expect(ref.textContent).toContain("選用：參考 baseline（系統自己的示範方法）");
    expect(ref.textContent).toContain("系統內建的示範");
    expect(ref.textContent).toContain("詞彙 baseline");
    const labels = Array.from(screen.getByRole("list", { name: "進度" }).querySelectorAll("li")).map((li) => li.textContent);
    expect(labels.some((l) => l?.includes("給開發人員"))).toBe(true);
    expect(labels.some((l) => l?.includes("上傳答案"))).toBe(true);
    expect(screen.getByText("凍結後才能下載")).toBeTruthy();
  });
});
