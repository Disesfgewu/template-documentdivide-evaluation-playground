import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Workflow } from "./Workflow";

afterEach(() => cleanup());

const PROJECT = "11111111-1111-4111-8111-111111111111";
const DOC = "22222222-2222-4222-8222-222222222222";
const DOC2 = "33333333-3333-4333-8333-333333333333";
const JOB = "44444444-4444-4444-8444-444444444444";

const doc = (id: string, name: string, benchmarks: number, parse: string | null) => ({
  id, document_version_id: id, project_id: PROJECT, logical_id: name, revision: 1, page_count: 21, size_bytes: 1000, sha256: "a".repeat(64),
  uploaded_at: "2026-11-15T00:00:00Z", latest_parse_job_id: parse ? JOB : null, latest_parse_status: parse, benchmarks,
});
const bench = (logical: string, status: string, questions: number, uploads = 0) => ({
  logical_id: logical, revision: 1, status, document_id: DOC, document_logical_id: "my-pdf", created_at: "2026-11-15T00:00:00Z",
  frozen_at: status === "frozen" ? "2026-11-15T01:00:00Z" : null, active_questions: questions, questions, uploads, runs: uploads, parse_job_id: JOB,
});

function progress(status: string, job: unknown, questions: number) {
  return { benchmark_revision_id: "x", logical_id: "b", revision: 1, benchmark_status: status, general_target: 10, cross_page_target: 5,
    confusable_target: 5, human_review: "skipped", limits: [], questions, by_bucket: { general: questions, cross_page: 0, confusable: 0 },
    shortfall: 0, job };
}

function server(benchmarks: unknown[], docs: unknown[], prog: Record<string, unknown>) {
  const calls: string[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    calls.push(url);
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    if (url.includes("/api/v1/workspace")) return json({ project_id: PROJECT, documents: docs, benchmarks, counts: { documents: docs.length, benchmarks: benchmarks.length } });
    const m = url.match(/benchmarks\/([^/]+)\/revisions\/1\/generate/);
    if (m) return json(prog[m[1]]);
    if (url.includes("/plan?")) return json({ benchmark_revision_id: "x", logical_id: "b", revision: 1, status: "planning", parse_job_id: JOB, pages: { usable: [0, 1], insufficient_material: {} } });
    if (url.includes("/submissions")) return json({ items: [], benchmark_status: "frozen" });
    if (url.includes("/estimate")) return json({ error: { code: "not_found", message: "none" } }, 404);
    return json({ error: { code: "not_found", message: "unexpected " + url } }, 404);
  });
  return { fn: fn as unknown as typeof fetch, calls };
}

const open = (fn: typeof fetch) => render(<Workflow fetchFn={fn} devToken="lab_act_dev" devProjectId={PROJECT} />);
const stepper = () => Array.from(screen.getByRole("list", { name: "進度" }).querySelectorAll("li")).map((li) => [li.textContent?.replace(/^[✓\d]+/, ""), li.className]);
const stepClass = (label: string) => String(stepper().find(([t]) => t === label)?.[1]);

describe("Workflow resume (Issue #194)", () => {
  it("an empty project says so and offers nothing to resume", async () => {
    const s = server([], [], {});
    open(s.fn);
    await waitFor(() => expect(screen.getByTestId("resume-empty")).toBeTruthy());
  });

  it("lists the existing benchmarks and documents with their state, counts and uploads", async () => {
    const s = server([bench("bench-a", "frozen", 17, 2)], [doc(DOC, "my-pdf", 1, "succeeded"), doc(DOC2, "unused-pdf", 0, null)], {});
    open(s.fn);
    const row = await screen.findByTestId("resume-bench-bench-a");
    expect(row.textContent).toContain("已凍結");
    expect(row.textContent).toContain("17");
    expect(row.textContent).toContain("my-pdf");
    expect(screen.getByTestId(`resume-doc-${DOC2}`).textContent).toContain("未解析");
    expect(screen.queryByTestId(`resume-doc-${DOC}`)).toBeNull();            // that document already has a benchmark
  });

  it("a frozen benchmark opens with every step up to the freeze done and the developer steps open", async () => {
    const s = server([bench("bench-a", "frozen", 17, 1)], [doc(DOC, "my-pdf", 1, "succeeded")], { "bench-a": progress("frozen", { job_id: JOB, phase: "validate", status: "succeeded", attempt: 1, model: "m", max_output_tokens: 500 }, 17) });
    open(s.fn);
    fireEvent.click(await screen.findByRole("button", { name: "接著做 bench-a" }));
    await waitFor(() => expect(screen.getByTestId("resume-note").textContent).toContain("題庫已凍結"));
    for (const label of ["上傳", "解析", "建題庫", "出題", "驗題", "凍結"]) expect(stepClass(label), label).toContain("stepper-success");
    expect(screen.getByTestId("step2-doc-id").textContent).toBe(DOC);
    expect(screen.getByTestId("step2-page-count").textContent).toBe("21");
  });

  it("a validating benchmark with a running validate job opens at validation, in flight, and says to refresh", async () => {
    const s = server([bench("bench-v", "validating", 12)], [doc(DOC, "my-pdf", 1, "succeeded")], { "bench-v": progress("validating", { job_id: JOB, phase: "validate", status: "running", attempt: 1, model: "m", max_output_tokens: 500 }, 12) });
    open(s.fn);
    fireEvent.click(await screen.findByRole("button", { name: "接著做 bench-v" }));
    await waitFor(() => expect(screen.getByTestId("resume-note").textContent).toContain("按「重新整理狀態」"));
    expect(stepClass("出題")).toContain("stepper-success");
    expect(stepClass("驗題")).toContain("stepper-running");
    expect(stepClass("凍結")).toContain("stepper-idle");
    expect(screen.getByRole("button", { name: "接著做 bench-v" }).textContent).toBe("重新整理狀態");
  });

  it("a planning benchmark opens at the generation step", async () => {
    const s = server([bench("bench-p", "planning", 0)], [doc(DOC, "my-pdf", 1, "succeeded")], { "bench-p": progress("planning", null, 0) });
    open(s.fn);
    fireEvent.click(await screen.findByRole("button", { name: "接著做 bench-p" }));
    await waitFor(() => expect(screen.getByTestId("resume-note").textContent).toContain("開始生成題目"));
    expect(stepClass("建題庫")).toContain("stepper-success");
    expect(stepClass("出題")).toContain("stepper-idle");
    expect(screen.getByTestId("next-hint").textContent).toContain("步驟 5");
  });

  it("a failed generation is shown as a failure the owner can retry, not as success", async () => {
    const s = server([bench("bench-f", "generating", 0)], [doc(DOC, "my-pdf", 1, "succeeded")], { "bench-f": progress("generating", { job_id: JOB, phase: "generate", status: "failed", failure_reason: "budget_exhausted", attempt: 1, model: "m", max_output_tokens: 500 }, 0) });
    open(s.fn);
    fireEvent.click(await screen.findByRole("button", { name: "接著做 bench-f" }));
    await waitFor(() => expect(stepClass("出題")).toContain("stepper-error"));
  });

  it("a status this page cannot continue says the data is kept and does not mark steps done", async () => {
    const s = server([bench("bench-x", "paused_budget", 3)], [doc(DOC, "my-pdf", 1, "succeeded")], { "bench-x": progress("paused_budget", null, 3) });
    open(s.fn);
    fireEvent.click(await screen.findByRole("button", { name: "接著做 bench-x" }));
    await waitFor(() => expect(screen.getByTestId("resume-note").textContent).toContain("沒有被刪除"));
    expect(stepClass("建題庫")).toContain("stepper-idle");
  });

  it("a document without a benchmark reopens at the parse or the plan step", async () => {
    const s = server([], [doc(DOC, "parsed-pdf", 0, "succeeded"), doc(DOC2, "raw-pdf", 0, null)], {});
    open(s.fn);
    fireEvent.click(await screen.findByRole("button", { name: "用文件 parsed-pdf 接著做" }));
    expect(screen.getByTestId("resume-note").textContent).toContain("步驟 4");
    expect(stepClass("解析")).toContain("stepper-success");
    fireEvent.click(screen.getByRole("button", { name: "用文件 raw-pdf 接著做" }));
    expect(screen.getByTestId("resume-note").textContent).toContain("步驟 3");
    expect(stepClass("解析")).toContain("stepper-idle");
  });

  it("a failing workspace call is shown without hiding the page", async () => {
    const fn = vi.fn(async () => new Response(JSON.stringify({ error: { code: "internal_error", message: "boom" } }), { status: 500, headers: { "Content-Type": "application/json" } })) as unknown as typeof fetch;
    open(fn);
    await waitFor(() => expect(screen.getByTestId("resume-error").textContent).toContain("boom"));
    expect(screen.getByRole("button", { name: "上傳 PDF" })).toBeTruthy();
  });
});
