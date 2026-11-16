import { describe, expect, it } from "vitest";
import { resumeSteps, STATUS_LABEL_ZH } from "./resume";

describe("resumeSteps: the benchmark status and its job decide where the stepper opens", () => {
  it("planning: the plan exists, generation has not started", () => {
    expect(resumeSteps("planning", null, 0)).toMatchObject({ resumable: true, generate: "idle", validate: "idle", freeze: "idle", inFlight: false });
  });

  it("generating: a live job is running (refresh), a succeeded one is done, a failed one is an error", () => {
    expect(resumeSteps("generating", { phase: "generate", status: "running" }, 0)).toMatchObject({ generate: "running", inFlight: true });
    expect(resumeSteps("generating", { phase: "generate", status: "queued" }, 0)).toMatchObject({ generate: "running", inFlight: true });
    expect(resumeSteps("generating", { phase: "generate", status: "succeeded" }, 12)).toMatchObject({ generate: "success", inFlight: false });
    expect(resumeSteps("generating", { phase: "generate", status: "failed" }, 0)).toMatchObject({ generate: "error" });
    expect(resumeSteps("generating", { phase: "generate", status: "cancelled" }, 0)).toMatchObject({ generate: "error" });
    expect(resumeSteps("generating", null, 0)).toMatchObject({ generate: "idle" });
    expect(resumeSteps("generating", null, 9).generate).toBe("success");      // questions exist without a job row
  });

  it("validating: generation is done; the validate job decides", () => {
    expect(resumeSteps("validating", null, 12)).toMatchObject({ generate: "success", validate: "idle" });
    expect(resumeSteps("validating", { phase: "validate", status: "running" }, 12)).toMatchObject({ validate: "running", inFlight: true });
    expect(resumeSteps("validating", { phase: "validate", status: "succeeded" }, 12)).toMatchObject({ validate: "success", freeze: "idle" });
    expect(resumeSteps("validating", { phase: "validate", status: "failed" }, 12)).toMatchObject({ validate: "error" });
    // the last job is the generation one: validation has not started
    expect(resumeSteps("validating", { phase: "generate", status: "succeeded" }, 12)).toMatchObject({ validate: "idle" });
  });

  it("ready_to_freeze and frozen", () => {
    expect(resumeSteps("ready_to_freeze", null, 12)).toMatchObject({ generate: "success", validate: "success", freeze: "idle", resumable: true });
    expect(resumeSteps("frozen", null, 12)).toMatchObject({ generate: "success", validate: "success", freeze: "success", resumable: true });
  });

  it("every other status is honestly not resumable here and says the data is not deleted", () => {
    for (const status of ["draft", "parsing", "needs_review", "paused_budget", "failed", "cancelled", "something_new"]) {
      const r = resumeSteps(status, null, 0);
      expect(r.resumable, status).toBe(false);
      expect(r.note, status).toContain("沒有被刪除");
    }
    expect(resumeSteps("draft", null, 0).note).toContain(STATUS_LABEL_ZH.draft);
  });
});
