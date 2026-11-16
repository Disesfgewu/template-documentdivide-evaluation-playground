// Map what the server says about a benchmark onto the workflow steps, so the page can reopen where the owner stopped (Issue #194).
// Pure: no fetch, no React. The page applies the result; the tests pin the mapping for every benchmark status.

export type ResumeState = "idle" | "running" | "success" | "error";

export type ResumeJob = { phase: string; status: string; failure_reason?: string | null } | null | undefined;

export type ResumeSteps = {
  resumable: boolean;
  generate: ResumeState;
  validate: ResumeState;
  freeze: ResumeState;
  /** a queued/running job: the page cannot follow it live after a reload, the owner presses refresh */
  inFlight: boolean;
  /** what the owner should read: where the benchmark is and what to do */
  note: string;
};

export const STATUS_LABEL_ZH: Record<string, string> = {
  draft: "草稿", parsing: "解析中", planning: "已做素材計畫", generating: "出題中", validating: "驗題階段",
  ready_to_freeze: "可凍結", frozen: "已凍結", needs_review: "待人工審核", paused_budget: "預算暫停", failed: "失敗", cancelled: "已取消",
};

const isLive = (s: string) => s === "queued" || s === "running";

function jobState(job: ResumeJob, phase: string, questions: number): ResumeState {
  if (!job || job.phase !== phase) return phase === "generate" && questions > 0 ? "success" : "idle";
  if (isLive(job.status)) return "running";
  if (job.status === "succeeded") return "success";
  return "error";                                   // failed or cancelled
}

export function resumeSteps(status: string, job: ResumeJob, questions: number): ResumeSteps {
  const none = { generate: "idle", validate: "idle", freeze: "idle", inFlight: false } as const;
  switch (status) {
    case "planning":
      return { ...none, resumable: true, note: "題庫與素材計畫已建好，接下來按「開始生成題目」。" };
    case "generating": {
      const generate = jobState(job, "generate", questions);
      return { ...none, resumable: true, generate, inFlight: generate === "running",
        note: generate === "running" ? "出題任務還在跑；按「重新整理狀態」看進度。"
          : generate === "error" ? "上次出題失敗，可以重新開始生成。" : generate === "success" ? "題目已生成，接下來驗題。" : "還沒開始出題。" };
    }
    case "validating": {
      const validate = jobState(job, "validate", questions);
      return { ...none, resumable: true, generate: "success", validate, inFlight: validate === "running",
        note: validate === "running" ? "驗題任務還在跑；按「重新整理狀態」看進度。"
          : validate === "error" ? "上次驗題失敗，可以重新驗題。" : validate === "success" ? "驗題完成，可以檢視、排除題目後凍結。" : "題目已生成，接下來驗題。" };
    }
    case "ready_to_freeze":
      return { ...none, resumable: true, generate: "success", validate: "success", note: "每一題都通過驗題，可以凍結了。" };
    case "frozen":
      return { ...none, resumable: true, generate: "success", validate: "success", freeze: "success",
        note: "題庫已凍結。可以下載題目檔給開發人員、上傳他們的答案、看分數。" };
    default:
      return { ...none, resumable: false,
        note: `這個題庫的狀態是「${STATUS_LABEL_ZH[status] ?? status}」，目前無法在這個頁面接續；資料沒有被刪除。` };
  }
}
