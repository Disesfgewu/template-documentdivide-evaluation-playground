import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QuestionReview } from "./QuestionReview";

afterEach(cleanup);

const PROJECT = "00000000-0000-4000-8000-000000000001";
const Q1 = "00000000-0000-4000-8000-0000000000a1";
const Q2 = "00000000-0000-4000-8000-0000000000a2";
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function review(excluded2: boolean, status = "validating") {
  const active = excluded2 ? 1 : 2;
  return {
    benchmark_revision_id: "b", status, total: 2, active, excluded: 2 - active,
    validation_of_active: { pass: 1, fail: excluded2 ? 0 : 1, needs_review: 0, unvalidated: 0 },
    can_freeze: excluded2, note: "x",
    items: [
      { id: Q1, ordinal: 0, quota_bucket: "general", bucket: "general", language: "zh", question_text: "第一題？", verdict: "pass", verdict_reason: "ok", excluded: false, exclusion_reason: null,
        reference_answer: "120 EUR", required_facts: [{ fact_id: "f1", statement: "年費是 120 EUR", weight: 1 }], conditions: [],
        evidence_sets: [{ set_id: "s1", units: [{ evidence_id: "e1", pages: [2], text: "Gold 方案年費 120 EUR，五月到期。" }] }], distractors: [],
        validation: { verdict: "pass", reason: "ok", code_checks: { passed: true, numbers_in_answer: 1, numbers_missing_from_evidence: 0 }, llm_checks: null } },
      { id: Q2, ordinal: 1, quota_bucket: "confusable", bucket: "confusable", question_text: "第二題？",
        reference_answer: "60 EUR", required_facts: [{ fact_id: "f2", statement: "年費是 60 EUR", weight: 1 }],
        evidence_sets: [{ set_id: "s2", units: [{ evidence_id: "e2", pages: [1, 3], text: "Silver 方案年費 60 EUR" }] }],
        distractors: [{ evidence_id: "e3", pages: [2], text: "Gold 方案年費 120 EUR", confusion_reason: "也是年費", disambiguating_condition: "Silver" }],
        validation: { verdict: "fail", reason: "validator: not supported", code_checks: { passed: false, numbers_in_answer: 1, numbers_missing_from_evidence: 1 }, llm_checks: null }, verdict: "fail", verdict_reason: "validator: not supported", excluded: excluded2,
        exclusion_reason: excluded2 ? "自動驗題沒有通過，這一題不納入凍結" : null },
    ],
  };
}

describe("QuestionReview", () => {
  it("shows each verdict, says the freeze is blocked, and excluding the failed question records the reason and unblocks it", async () => {
    let excluded = false;
    const calls: { url: string; body?: string }[] = [];
    const f = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const u = String(url);
      calls.push({ url: u, body: init?.body as string | undefined });
      if (u.includes("/exclusion")) {
        excluded = JSON.parse(String(init?.body)).excluded;
        return json({ question_id: Q2, excluded, active_questions: excluded ? 1 : 2 });
      }
      return json(review(excluded));
    });
    render(<QuestionReview fetchFn={f as unknown as typeof fetch} token="t" projectId={PROJECT} logicalId="bench" reloadKey="k" />);
    await waitFor(() => expect(screen.getByTestId("review-summary")).toBeTruthy());
    expect(screen.getByTestId("review-summary").textContent).toContain("還不能凍結");
    expect(screen.getByTestId("review-row-1").textContent).toContain("未通過");
    expect(screen.getByTestId("review-row-1").textContent).toContain("validator: not supported");
    expect(screen.getByTestId("review-row-0").textContent).toContain("通過");

    fireEvent.click(screen.getByTestId("review-row-1").querySelector("button")!);
    await waitFor(() => expect(screen.getByTestId("review-summary").textContent).toContain("現在可以凍結"));
    const post = calls.find((c) => c.url.includes("/exclusion"))!;
    expect(post.url).toContain(`/questions/${Q2}/exclusion`);
    expect(JSON.parse(post.body!)).toEqual({ excluded: true, reason: "自動驗題沒有通過，這一題不納入凍結" });
    expect(screen.getByTestId("review-row-1").textContent).toContain("已排除");
    expect(screen.getByTestId("review-row-1").querySelector("button")!.textContent).toBe("恢復");     // restore is one click away
  });

  it("will not send an exclusion without a reason", async () => {
    const f = vi.fn(async (_url: RequestInfo | URL) => json(review(false)));
    render(<QuestionReview fetchFn={f as unknown as typeof fetch} token="t" projectId={PROJECT} logicalId="bench" reloadKey="k" />);
    await waitFor(() => expect(screen.getByTestId("review-summary")).toBeTruthy());
    fireEvent.change(screen.getByLabelText(/理由/), { target: { value: "   " } });
    fireEvent.click(screen.getByTestId("review-row-1").querySelector("button")!);
    expect((await screen.findByRole("alert")).textContent).toContain("請先寫下理由");
    expect(f.mock.calls.every((c) => !String(c[0]).includes("/exclusion"))).toBe(true);
  });

  it("offers no exclude or restore buttons once the benchmark is frozen", async () => {
    const f = vi.fn(async () => json(review(true, "frozen")));
    render(<QuestionReview fetchFn={f as unknown as typeof fetch} token="t" projectId={PROJECT} logicalId="bench" reloadKey="k" />);
    await waitFor(() => expect(screen.getByTestId("review-summary")).toBeTruthy());
    expect(screen.queryByRole("button", { name: "排除" })).toBeNull();               // no exclude / restore once frozen ...
    expect(screen.queryByRole("button", { name: "恢復" })).toBeNull();
    expect(screen.getByRole("button", { name: "下載題庫 JSON" })).toBeTruthy();       // ... but the download stays
  });

  it("a refused request (not the owner) is shown as an error, not as an empty list", async () => {
    const f = vi.fn(async () => json({ error: { message: "permission denied" } }, 403));
    render(<QuestionReview fetchFn={f as unknown as typeof fetch} token="t" projectId={PROJECT} logicalId="bench" reloadKey="k" />);
    expect((await screen.findByRole("alert")).textContent).toContain("讀取題目失敗");
  });
});

describe("QuestionReview: see and download what was generated", () => {
  const render2 = (f: typeof fetch, triggerSave?: (b: Blob, n: string) => void) =>
    render(<QuestionReview fetchFn={f} token="t" projectId={PROJECT} logicalId="bench" reloadKey="k" triggerSave={triggerSave} />);

  it("explains what the automated validation checks in one sentence", async () => {
    render2(vi.fn(async () => json(review(false))) as unknown as typeof fetch);
    await waitFor(() => expect(screen.getByTestId("validation-explained")).toBeTruthy());
    const text = screen.getByTestId("validation-explained").textContent ?? "";
    expect(text).toContain("程式比對答案、要點和數字");
    expect(text).toContain("光看那段原文，答不答得出來");
  });

  it("each question can be opened to see its type, reference answer, facts, source pages and text, distractor and the check result", async () => {
    render2(vi.fn(async () => json(review(false))) as unknown as typeof fetch);
    await waitFor(() => expect(screen.getByTestId("review-details-0")).toBeTruthy());
    const one = screen.getByTestId("review-details-0").textContent ?? "";
    for (const part of ["一般題", "120 EUR", "年費是 120 EUR", "第 2 頁", "Gold 方案年費 120 EUR，五月到期。", "程式比對通過"]) expect(one).toContain(part);
    const two = screen.getByTestId("review-details-1").textContent ?? "";
    for (const part of ["易混淆題", "第 1、3 頁", "容易混淆的另一段", "也是年費", "怎麼區分：Silver", "validator: not supported", "程式比對沒通過", "原文缺少 1 個"]) expect(two).toContain(part);
  });

  it("downloads the JSON and the JSONL through the authorised API and hands the file to the browser, without showing the token", async () => {
    const saved: { name: string; size: number; type: string }[] = [];
    const seen: { url: string; auth: string | null }[] = [];
    const f = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const u = String(url);
      if (u.includes("/export")) {
        seen.push({ url: u, auth: new Headers(init?.headers).get("Authorization") });
        const jsonl = u.includes("format=jsonl");
        return new Response(jsonl ? '{"type":"benchmark"}\n' : '{"items":[]}', {
          status: 200, headers: { "Content-Type": jsonl ? "application/x-ndjson" : "application/json",
                                  "Content-Disposition": `attachment; filename="bench-r1.${jsonl ? "jsonl" : "json"}"` } });
      }
      return json(review(false));
    });
    render2(f as unknown as typeof fetch, (blob, name) => saved.push({ name, size: blob.size, type: blob.type }));
    await waitFor(() => expect(screen.getByTestId("benchmark-download")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "下載題庫 JSON" }));
    await waitFor(() => expect(saved.length).toBe(1));
    fireEvent.click(screen.getByRole("button", { name: "下載題庫 JSONL" }));
    await waitFor(() => expect(saved.length).toBe(2));
    expect(saved.map((s) => s.name)).toEqual(["bench-r1.json", "bench-r1.jsonl"]);
    expect(seen[0].url).toContain(`/api/v1/benchmarks/bench/revisions/1/export?project_id=${PROJECT}&format=json`);
    expect(seen[1].url).toContain("format=jsonl");
    expect(seen.every((s) => s.auth === "Bearer t")).toBe(true);
    expect(screen.getByTestId("benchmark-download").textContent).toContain("已下載 bench-r1.jsonl");
    expect(screen.getByTestId("benchmark-download").textContent).toContain("gold");              // the page says the file holds the answers
  });

  it("a refused download is an error message and nothing is saved", async () => {
    const saved: string[] = [];
    const f = vi.fn(async (url: RequestInfo | URL) => String(url).includes("/export") ? json({ error: { message: "permission denied for t" } }, 403) : json(review(false)));
    render2(f as unknown as typeof fetch, (_b, name) => saved.push(name));
    await waitFor(() => expect(screen.getByTestId("benchmark-download")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "下載題庫 JSON" }));
    const alert = await screen.findByText(/下載失敗/);
    expect(alert.textContent).toContain("permission denied");
    expect(alert.textContent).not.toContain("denied for t");           // the token is redacted from the message
    expect(saved).toEqual([]);
  });
});
