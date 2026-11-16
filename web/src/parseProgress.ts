// Parse progress shown on the page (Issue #205): pure helpers, no I/O.
export type ParseProgress = { pages_done: number; pages_total: number; updated_at?: string };

export type ProgressView = { percent: number; text: string; eta?: string };

/** "已處理 120 / 834 頁（14%）" and, once at least 5% is done and some time has passed, "預估還要約 N 分鐘". `elapsedMs` = time since the job began. */
export function progressView(p: ParseProgress | null | undefined, elapsedMs: number): ProgressView | null {
  if (!p || !Number.isFinite(p.pages_done) || !Number.isFinite(p.pages_total) || p.pages_total < 1) return null;
  const done = Math.max(0, Math.min(p.pages_done, p.pages_total));
  const ratio = done / p.pages_total;
  const percent = Math.floor(ratio * 100);
  const text = `已處理 ${done} / ${p.pages_total} 頁（${percent}%）`;
  if (done >= p.pages_total) return { percent: 100, text, eta: "正在整理並存進資料庫…" };
  if (ratio < 0.05 || elapsedMs < 5000) return { percent, text };
  const remainingSec = (elapsedMs / ratio) * (1 - ratio) / 1000;
  const eta = remainingSec < 90 ? "預估還要不到 2 分鐘" : `預估還要約 ${Math.round(remainingSec / 60)} 分鐘`;
  return { percent, text, eta };
}

/** The reason of the latest transition into "failed" (a short fixed code such as persist_failed), or null. */
export function failureReason(transitions: { to: string; reason: string }[] | undefined): string | null {
  const t = (transitions ?? []).find((x) => x.to === "failed");
  return t && t.reason ? t.reason : null;
}
