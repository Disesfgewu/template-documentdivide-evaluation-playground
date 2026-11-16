import { describe, expect, it } from "vitest";
import { failureReason, progressView } from "./parseProgress";

describe("progressView", () => {
  it("shows pages and percent, and no estimate before 5 % or before 5 seconds", () => {
    expect(progressView({ pages_done: 0, pages_total: 834 }, 60_000)).toEqual({ percent: 0, text: "已處理 0 / 834 頁（0%）" });
    expect(progressView({ pages_done: 30, pages_total: 834 }, 60_000)?.eta).toBeUndefined();      // 3.6 %: too early to extrapolate
    expect(progressView({ pages_done: 100, pages_total: 834 }, 2_000)?.eta).toBeUndefined();      // only 2 s of data
  });

  it("estimates the rest from the pace so far", () => {
    const v = progressView({ pages_done: 417, pages_total: 834 }, 4 * 60_000)!;                   // half in 4 min -> about 4 more
    expect(v.percent).toBe(50);
    expect(v.eta).toBe("預估還要約 4 分鐘");
    expect(progressView({ pages_done: 800, pages_total: 834 }, 400_000)?.eta).toBe("預估還要不到 2 分鐘");
  });

  it("at the total it says it is saving, never 'about 0 minutes'", () => {
    const v = progressView({ pages_done: 834, pages_total: 834 }, 400_000)!;
    expect(v.percent).toBe(100);
    expect(v.eta).toContain("存進資料庫");
  });

  it("clamps nonsense and gives nothing for a missing or invalid progress", () => {
    expect(progressView({ pages_done: 900, pages_total: 834 }, 1000)?.percent).toBe(100);
    expect(progressView({ pages_done: -5, pages_total: 10 }, 1000)?.percent).toBe(0);
    for (const bad of [null, undefined, { pages_done: 1, pages_total: 0 }, { pages_done: NaN, pages_total: 5 }]) {
      expect(progressView(bad as never, 1000)).toBeNull();
    }
  });
});

describe("failureReason", () => {
  it("is the reason of the transition into failed", () => {
    expect(failureReason([{ to: "failed", reason: "persist_failed" }, { to: "running", reason: "lease acquired" }])).toBe("persist_failed");
    expect(failureReason([{ to: "running", reason: "x" }])).toBeNull();
    expect(failureReason(undefined)).toBeNull();
  });
});
