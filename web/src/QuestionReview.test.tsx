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
      { id: Q1, ordinal: 0, quota_bucket: "general", question_text: "第一題？", verdict: "pass", verdict_reason: "ok", excluded: false, exclusion_reason: null },
      { id: Q2, ordinal: 1, quota_bucket: "general", question_text: "第二題？", verdict: "fail", verdict_reason: "validator: not supported", excluded: excluded2,
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

  it("offers no buttons once the benchmark is frozen", async () => {
    const f = vi.fn(async () => json(review(true, "frozen")));
    render(<QuestionReview fetchFn={f as unknown as typeof fetch} token="t" projectId={PROJECT} logicalId="bench" reloadKey="k" />);
    await waitFor(() => expect(screen.getByTestId("review-summary")).toBeTruthy());
    expect(screen.queryAllByRole("button").length).toBe(0);
  });

  it("a refused request (not the owner) is shown as an error, not as an empty list", async () => {
    const f = vi.fn(async () => json({ error: { message: "permission denied" } }, 403));
    render(<QuestionReview fetchFn={f as unknown as typeof fetch} token="t" projectId={PROJECT} logicalId="bench" reloadKey="k" />);
    expect((await screen.findByRole("alert")).textContent).toContain("讀取題目失敗");
  });
});
