import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QuestionsPanel } from "./QuestionsPanel";

afterEach(cleanup);

const PROJECT = "00000000-0000-4000-8000-000000000001";
const RUN = "00000000-0000-4000-8000-000000000002";
const Q1 = "00000000-0000-4000-8000-0000000000a1";
const Q2 = "00000000-0000-4000-8000-0000000000a2";
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function fetchMock(extra?: (url: string) => Response | undefined) {
  return vi.fn(async (url: RequestInfo | URL) => {
    const u = String(url);
    const custom = extra?.(u);
    if (custom) return custom;
    if (u.includes("/questions")) {
      if (u.includes("cursor=c2")) return json({ benchmark_revision_id: "b", status: "frozen", items: [{ id: Q2, question_text: "第二題：費用何時到期？" }], next_cursor: null });
      return json({ benchmark_revision_id: "b", status: "frozen", items: [{ id: Q1, question_text: "第一題：費用是多少？" }], next_cursor: "c2" });
    }
    if (u.includes("metric_name=recall_at_k")) {
      return json({ metrics: [], items: [
        { question_id: Q1, ordinal: 1, quota_bucket: "general", metric_name: "recall_at_k", status: "scored", raw_value: 100, reason_codes: [] },
        { question_id: Q2, ordinal: 2, quota_bucket: "general", metric_name: "recall_at_k", status: "scored", raw_value: 0, reason_codes: [] }] });
    }
    if (u.includes("metric_name=correctness")) {
      return json({ metrics: [], items: [
        { question_id: Q1, ordinal: 1, quota_bucket: "general", metric_name: "correctness", status: "pending", raw_value: null, reason_codes: ["semantic_judge_not_run"] },
        { question_id: Q2, ordinal: 2, quota_bucket: "general", metric_name: "correctness", status: "error", raw_value: null, reason_codes: ["budget_exhausted"] }] });
    }
    return json({ error: { message: "not found" } }, 404);
  });
}

describe("QuestionsPanel", () => {
  it("lists every question across pages and shows nothing but the public text", async () => {
    const f = fetchMock();
    render(<QuestionsPanel fetchFn={f as unknown as typeof fetch} token="t" projectId={PROJECT} logicalId="bench" reloadKey="k1" />);
    await waitFor(() => expect(screen.getByTestId("questions-list")).toBeTruthy());
    expect(screen.getByText("第一題：費用是多少？")).toBeTruthy();
    expect(screen.getByText("第二題：費用何時到期？")).toBeTruthy();
    expect(screen.getByTestId("questions-list").textContent).toContain("共 2 題");
    expect(screen.queryByText("證據命中")).toBeNull();                        // no run: no score columns
    expect(f.mock.calls.every((c) => !String(c[0]).includes("/runs/"))).toBe(true);
  });

  it("shows per-question scores for a run: a real score, not yet scored, and an error code, never a made-up number", async () => {
    render(<QuestionsPanel fetchFn={fetchMock() as unknown as typeof fetch} token="t" projectId={PROJECT} logicalId="bench" reloadKey="k1" runId={RUN} />);
    await waitFor(() => expect(screen.getByText("證據命中")).toBeTruthy());
    const rows = screen.getAllByRole("row");
    expect(rows[1].textContent).toContain("100 分");
    expect(rows[1].textContent).toContain("尚未評分");
    expect(rows[2].textContent).toContain("0 分");
    expect(rows[2].textContent).toContain("錯誤：budget_exhausted");
  });

  it("a failing request is an error message, not an empty list", async () => {
    const f = fetchMock((u) => (u.includes("/questions") ? json({ error: { message: "nope" } }, 500) : undefined));
    render(<QuestionsPanel fetchFn={f as unknown as typeof fetch} token="t" projectId={PROJECT} logicalId="bench" reloadKey="k1" />);
    expect((await screen.findByRole("alert")).textContent).toContain("讀取題目失敗");
  });

  it("does not call the API before there is anything to load", () => {
    const f = fetchMock();
    render(<QuestionsPanel fetchFn={f as unknown as typeof fetch} token="t" projectId={PROJECT} logicalId="bench" reloadKey="" />);
    expect(f).not.toHaveBeenCalled();
    expect(screen.getByText(/題目生成後/)).toBeTruthy();
  });
});
