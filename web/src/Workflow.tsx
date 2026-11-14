import React, { useEffect, useRef, useState } from "react";
import { QuestionsPanel } from "./QuestionsPanel";
import { QuestionReview } from "./QuestionReview";
import { BenchmarkDownload } from "./BenchmarkDownload";
import {
  cancelJob,
  createBenchmark,
  freezeBenchmark,
  getGenerationProgress,
  getJobStatus,
  getRunEstimate,
  getRunResults,
  parseDocument,
  planBenchmark,
  runBaseline,
  startJudgeRun,
  startMethodRun,
  startQuestionGeneration,
  startQuestionValidation,
  uploadPdf,
  validateUuid,
  type BenchmarkPlanResponse,
  type GenerationProgressResponse,
  type JobStatusResponse,
  type RunEstimate,
  type RunResultsResponse,
  type UploadDocumentResponse,
} from "./workflowApi";

export type StepState = "idle" | "running" | "success" | "error" | "skipped" | "conflict_409";

export function getStatusLabel(state: StepState): string {
  switch (state) {
    case "idle":
      return "未開始";
    case "running":
      return "進行中";
    case "success":
      return "成功";
    case "error":
      return "失敗";
    case "skipped":
      return "略過";
    case "conflict_409":
      return "409 誠實結果";
  }
}

export function getStatusStyle(state: StepState): React.CSSProperties {
  switch (state) {
    case "idle":
      return { backgroundColor: "#f1f3f4", color: "#5f6368", border: "1px solid #dadce0" };
    case "running":
      return { backgroundColor: "#e8f0fe", color: "#1a73e8", border: "1px solid #aecbfa" };
    case "success":
      return { backgroundColor: "#e6f4ea", color: "#137333", border: "1px solid #ceead6" };
    case "error":
      return { backgroundColor: "#fce8e6", color: "#c5221f", border: "1px solid #fad2cf" };
    case "skipped":
    case "conflict_409":
      return { backgroundColor: "#fef7e0", color: "#b06000", border: "1px solid #fdd663" };
  }
}

export type WorkflowProps = {
  fetchFn?: typeof fetch;
  /** Local development session written by `scripts/demo.sh ui` (web/.env.local, git-ignored). */
  devToken?: string;
  devProjectId?: string;
};

const ENV_TOKEN = (import.meta.env.VITE_LAB_DEV_TOKEN as string | undefined) ?? "";
const ENV_PROJECT = (import.meta.env.VITE_LAB_DEV_PROJECT_ID as string | undefined) ?? "";

export function Workflow({ fetchFn = fetch, devToken = ENV_TOKEN, devProjectId = ENV_PROJECT }: WorkflowProps) {
  // Security invariant: Token held strictly in memory
  const autoLogin = Boolean(devToken.trim() && validateUuid(devProjectId.trim()));
  const [token, setToken] = useState(autoLogin ? devToken.trim() : "");
  const [projectId, setProjectId] = useState(autoLogin ? devProjectId.trim() : "");

  const isAuthValid = Boolean(token.trim() && validateUuid(projectId.trim()));

  // Step 2: Upload
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadLogicalId, setUploadLogicalId] = useState("");
  const [uploadStatus, setUploadStatus] = useState<StepState>("idle");
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadedDoc, setUploadedDoc] = useState<UploadDocumentResponse | null>(null);

  // Step 3: Parse
  const [parseStatus, setParseStatus] = useState<StepState>("idle");
  const [parseError, setParseError] = useState<string | null>(null);
  const [parseJobId, setParseJobId] = useState<string | null>(null);
  const [parseJobDetail, setParseJobDetail] = useState<JobStatusResponse | null>(null);

  // Step 4: Benchmark + Plan
  const [benchLogicalIdInput, setBenchLogicalIdInput] = useState("");
  const [benchLogicalId, setBenchLogicalId] = useState<string | null>(null);
  const [planStatus, setPlanStatus] = useState<StepState>("idle");
  const [planError, setPlanError] = useState<string | null>(null);
  const [planResult, setPlanResult] = useState<BenchmarkPlanResponse | null>(null);

  // Step 5: Generate
  const [genModel, setGenModel] = useState("claude-haiku-4-5-20251001");
  const [genCount, setGenCount] = useState("");          // how many questions (filled with the plan's total once it is known)
  const [genStatus, setGenStatus] = useState<StepState>("idle");
  const [genError, setGenError] = useState<string | null>(null);
  const [genProgress, setGenProgress] = useState<GenerationProgressResponse | null>(null);

  // Step 6: Validate
  const [valModel, setValModel] = useState("claude-haiku-4-5-20251001");
  const [valStatus, setValStatus] = useState<StepState>("idle");
  const [valError, setValError] = useState<string | null>(null);
  const [valProgress, setValProgress] = useState<GenerationProgressResponse | null>(null);

  // Step 7: Freeze
  const [freezeStatus, setFreezeStatus] = useState<StepState>("idle");
  const [freezeError, setFreezeError] = useState<string | null>(null);
  const [freezeMessage409, setFreezeMessage409] = useState<string | null>(null);
  const [freezeLimits, setFreezeLimits] = useState<string[] | null>(null);

  // Step 8: Run Methods
  const [methodStatus, setMethodStatus] = useState<StepState>("idle");
  const [methodError, setMethodError] = useState<string | null>(null);
  const [methodJobDetail, setMethodJobDetail] = useState<JobStatusResponse | null>(null);
  const [methodModel, setMethodModel] = useState("claude-haiku-4-5-20251001");
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [activeEvalId, setActiveEvalId] = useState<string | null>(null);
  const [activeRunKind, setActiveRunKind] = useState<"baseline" | "model" | null>(null);

  // Step 9: Results & Judge
  const [resultsStatus, setResultsStatus] = useState<StepState>("idle");
  const [resultsError, setResultsError] = useState<string | null>(null);
  const [resultsData, setResultsData] = useState<RunResultsResponse | null>(null);
  const [judgeStatus, setJudgeStatus] = useState<StepState>("idle");
  const [judgeError, setJudgeError] = useState<string | null>(null);

  // Active polling controller ref for clean cancellation & unmount cleanup
  const activeAbortControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => {
      activeAbortControllerRef.current?.abort();
    };
  }, []);

  // The worst-case cost of the NEXT model step, in USD, from the API (Issue #186): nobody types a price.
  type Est = { value: RunEstimate | null; error: string | null };
  const noEst: Est = { value: null, error: null };
  const [genEst, setGenEst] = useState<Est>(noEst);
  const [valEst, setValEst] = useState<Est>(noEst);
  const [methodEst, setMethodEst] = useState<Est>(noEst);
  const [judgeEst, setJudgeEst] = useState<Est>(noEst);
  const countNumber = Number(genCount);
  const countValid = Number.isInteger(countNumber) && countNumber >= 1;

  const useEstimateEffect = (
    enabled: boolean,
    phase: "generate" | "validate" | "answer" | "judge",
    set: (e: Est) => void,
    extra: { questionCount?: number; runId?: string | null; onFirst?: (e: RunEstimate) => void } = {},
    deps: unknown[] = [],
  ) => {
    useEffect(() => {
      if (!enabled || !benchLogicalId) {
        set(noEst);
        return;
      }
      const controller = new AbortController();
      void getRunEstimate({
        projectId: projectId.trim(), logicalId: benchLogicalId, phase, questionCount: extra.questionCount,
        runId: extra.runId ?? undefined, token: token.trim(), fetchFn, signal: controller.signal,
      }).then((res) => {
        if (controller.signal.aborted) return;
        if (res.ok) {
          set({ value: res.data, error: null });
          extra.onFirst?.(res.data);
        } else {
          set({ value: null, error: res.error.message });
        }
      });
      return () => controller.abort();
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [enabled, benchLogicalId, projectId, token, ...deps]);
  };
  useEstimateEffect(planStatus === "success" && genStatus !== "running", "generate", setGenEst,
    { questionCount: genCount === "" ? undefined : countValid ? countNumber : undefined,
      onFirst: (e) => { if (genCount === "" && e.plan_total) setGenCount(String(e.plan_total)); } }, [genCount, genStatus]);
  useEstimateEffect(genStatus === "success" && valStatus !== "running" && freezeStatus !== "success", "validate", setValEst, {}, [genStatus, valStatus]);
  useEstimateEffect(freezeStatus === "success" && methodStatus !== "running", "answer", setMethodEst, {}, [freezeStatus, methodStatus]);
  useEstimateEffect(activeRunKind === "model" && Boolean(activeRunId) && judgeStatus !== "running", "judge", setJudgeEst,
    { runId: activeRunId }, [activeRunKind, activeRunId, judgeStatus]);

  // ---------------------------------------------------------------------------
  // Handlers
  // ---------------------------------------------------------------------------

  // Step 2: Upload PDF
  const handleUpload = async () => {
    if (!isAuthValid || !uploadFile || uploadStatus === "running") return;
    setUploadStatus("running");
    setUploadError(null);
    setUploadedDoc(null);

    const controller = new AbortController();
    activeAbortControllerRef.current = controller;

    try {
      const res = await uploadPdf({
        projectId: projectId.trim(),
        token: token.trim(),
        file: uploadFile,
        logicalId: uploadLogicalId.trim() || undefined,
        fetchFn,
        signal: controller.signal,
      });

      if (res.ok) {
        setUploadStatus("success");
        setUploadedDoc(res.data);
      } else {
        setUploadStatus("error");
        setUploadError(res.error.message);
      }
    } finally {
      if (activeAbortControllerRef.current === controller) {
        activeAbortControllerRef.current = null;
      }
    }
  };

  // Step 3: Parse PDF & Poll
  const handleParse = async () => {
    if (!uploadedDoc || parseStatus === "running") return;
    setParseStatus("running");
    setParseError(null);
    setParseJobDetail(null);

    const controller = new AbortController();
    activeAbortControllerRef.current = controller;

    try {
      const parseRes = await parseDocument({
        documentId: uploadedDoc.id,
        token: token.trim(),
        fetchFn,
        signal: controller.signal,
      });

      if (!parseRes.ok) {
        setParseStatus("error");
        setParseError(parseRes.error.message);
        return;
      }

      const jobId = parseRes.data.job_id;
      setParseJobId(jobId);

      // Poll GET /api/v1/jobs/{job_id} every 3s up to 10 minutes
      const deadline = Date.now() + 600_000;
      while (Date.now() < deadline) {
        if (controller.signal.aborted) break;

        const jobRes = await getJobStatus({
          jobId,
          token: token.trim(),
          fetchFn,
          signal: controller.signal,
        });

        if (!jobRes.ok) {
          setParseStatus("error");
          setParseError(jobRes.error.message);
          return;
        }

        setParseJobDetail(jobRes.data);
        const s = jobRes.data.status;
        if (s === "succeeded") {
          setParseStatus("success");
          return;
        }
        if (s === "failed") {
          setParseStatus("error");
          setParseError("解析任務失敗");
          return;
        }
        if (s === "cancelled") {
          setParseStatus("error");
          setParseError("解析任務已取消");
          return;
        }

        await new Promise((r) => setTimeout(r, 3000));
      }

      if (Date.now() >= deadline) {
        setParseStatus("error");
        setParseError("解析任務逾時（已超過 10 分鐘）");
      }
    } finally {
      if (activeAbortControllerRef.current === controller) {
        activeAbortControllerRef.current = null;
      }
    }
  };

  const handleCancelParse = async () => {
    if (parseJobId) {
      void cancelJob({ jobId: parseJobId, token: token.trim(), fetchFn });
    }
    activeAbortControllerRef.current?.abort();
    setParseStatus("error");
    setParseError("使用者已手動取消解析");
  };

  // Step 4: Create Benchmark + Plan
  const handleCreateAndPlan = async () => {
    if (parseStatus !== "success" || !uploadedDoc || planStatus === "running") return;
    setPlanStatus("running");
    setPlanError(null);
    setPlanResult(null);

    const controller = new AbortController();
    activeAbortControllerRef.current = controller;

    try {
      const generatedBenchLogicalId =
        benchLogicalIdInput.trim() || `bench-${Date.now().toString(36)}`;
      setBenchLogicalId(generatedBenchLogicalId);

      // 4a: Create benchmark
      const createRes = await createBenchmark({
        projectId: projectId.trim(),
        documentId: uploadedDoc.id,
        logicalId: generatedBenchLogicalId,
        token: token.trim(),
        fetchFn,
        signal: controller.signal,
      });

      if (!createRes.ok) {
        setPlanStatus("error");
        setPlanError(createRes.error.message);
        return;
      }

      // 4b: Plan benchmark
      const planRes = await planBenchmark({
        projectId: projectId.trim(),
        logicalId: generatedBenchLogicalId,
        revision: 1,
        parseJobId: parseJobId ?? undefined,
        token: token.trim(),
        fetchFn,
        signal: controller.signal,
      });

      if (planRes.ok) {
        setPlanStatus("success");
        setPlanResult(planRes.data);
      } else {
        setPlanStatus("error");
        setPlanError(planRes.error.message);
      }
    } finally {
      if (activeAbortControllerRef.current === controller) {
        activeAbortControllerRef.current = null;
      }
    }
  };

  // Step 5: Generate Questions
  const handleGenerate = async () => {
    if (planStatus !== "success" || !benchLogicalId || genStatus === "running") return;
    setGenStatus("running");
    setGenError(null);
    setGenProgress(null);

    const controller = new AbortController();
    activeAbortControllerRef.current = controller;

    try {
      const startRes = await startQuestionGeneration({
        projectId: projectId.trim(),
        logicalId: benchLogicalId,
        revision: 1,
        model: genModel.trim(),
        questionCount: countValid ? countNumber : undefined,
        token: token.trim(),
        fetchFn,
        signal: controller.signal,
      });

      if (!startRes.ok) {
        setGenStatus("error");
        setGenError(startRes.error.message);
        return;
      }

      // Poll GET generate
      const deadline = Date.now() + 900_000;
      while (Date.now() < deadline) {
        if (controller.signal.aborted) break;

        const progRes = await getGenerationProgress({
          projectId: projectId.trim(),
          logicalId: benchLogicalId,
          revision: 1,
          token: token.trim(),
          fetchFn,
          signal: controller.signal,
        });

        if (!progRes.ok) {
          setGenStatus("error");
          setGenError(progRes.error.message);
          return;
        }

        setGenProgress(progRes.data);
        const jobStatus = progRes.data.job?.status;
        if (jobStatus === "succeeded") {
          setGenStatus("success");
          return;
        }
        if (jobStatus === "failed" || jobStatus === "cancelled") {
          setGenStatus("error");
          setGenError(explainFailure("generate", jobStatus, progRes.data.job?.failure_reason));
          return;
        }

        await new Promise((r) => setTimeout(r, 3000));
      }

      if (Date.now() >= deadline) {
        setGenStatus("error");
        setGenError("題目生成任務逾時");
      }
    } finally {
      if (activeAbortControllerRef.current === controller) {
        activeAbortControllerRef.current = null;
      }
    }
  };

  // Step 6: Validate Questions
  const handleValidate = async () => {
    if (genStatus !== "success" || !benchLogicalId || valStatus === "running") return;
    setValStatus("running");
    setValError(null);
    setValProgress(null);

    const controller = new AbortController();
    activeAbortControllerRef.current = controller;

    try {
      const startRes = await startQuestionValidation({
        projectId: projectId.trim(),
        logicalId: benchLogicalId,
        revision: 1,
        model: valModel.trim(),
        token: token.trim(),
        fetchFn,
        signal: controller.signal,
      });

      if (!startRes.ok) {
        setValStatus("error");
        setValError(startRes.error.message);
        return;
      }

      // Poll GET generate until job.phase == "validate" and terminal
      const deadline = Date.now() + 900_000;
      while (Date.now() < deadline) {
        if (controller.signal.aborted) break;

        const progRes = await getGenerationProgress({
          projectId: projectId.trim(),
          logicalId: benchLogicalId,
          revision: 1,
          token: token.trim(),
          fetchFn,
          signal: controller.signal,
        });

        if (!progRes.ok) {
          setValStatus("error");
          setValError(progRes.error.message);
          return;
        }

        setValProgress(progRes.data);
        const job = progRes.data.job;
        if (job && job.phase === "validate") {
          if (job.status === "succeeded") {
            setValStatus("success");
            return;
          }
          if (job.status === "failed" || job.status === "cancelled") {
            setValStatus("error");
            setValError(explainFailure("validate", job.status, job.failure_reason));
            return;
          }
        }

        await new Promise((r) => setTimeout(r, 3000));
      }

      if (Date.now() >= deadline) {
        setValStatus("error");
        setValError("自動驗題任務逾時");
      }
    } finally {
      if (activeAbortControllerRef.current === controller) {
        activeAbortControllerRef.current = null;
      }
    }
  };

  // Step 7: Freeze
  const handleFreeze = async () => {
    if (valStatus !== "success" || !benchLogicalId || freezeStatus === "running") return;
    setFreezeStatus("running");
    setFreezeError(null);
    setFreezeMessage409(null);
    setFreezeLimits(null);

    const controller = new AbortController();
    activeAbortControllerRef.current = controller;

    try {
      const res = await freezeBenchmark({
        projectId: projectId.trim(),
        logicalId: benchLogicalId,
        revision: 1,
        token: token.trim(),
        fetchFn,
        signal: controller.signal,
      });

      if (res.ok) {
        setFreezeStatus("success");
        setFreezeLimits(res.data.limits || []);
      } else {
        if (res.status === 409) {
          // 409 is an honest outcome, not an error!
          setFreezeStatus("conflict_409");
          setFreezeMessage409(res.error.message);
        } else {
          setFreezeStatus("error");
          setFreezeError(res.error.message);
        }
      }
    } finally {
      if (activeAbortControllerRef.current === controller) {
        activeAbortControllerRef.current = null;
      }
    }
  };

  // Step 8a: Baseline Run (Lexical)
  const handleBaselineRun = async () => {
    if (freezeStatus !== "success" || !benchLogicalId || methodStatus === "running") return;
    setMethodStatus("running");
    setMethodError(null);
    setActiveRunId(null);
    setActiveEvalId(null);
    setActiveRunKind(null);
    setMethodJobDetail(null);
    setJudgeStatus("idle");
    setJudgeError(null);

    const controller = new AbortController();
    activeAbortControllerRef.current = controller;

    try {
      const res = await runBaseline({
        projectId: projectId.trim(),
        logicalId: benchLogicalId,
        revision: 1,
        token: token.trim(),
        fetchFn,
        signal: controller.signal,
      });

      if (res.ok) {
        setMethodStatus("success");
        setActiveRunKind("baseline");
        setActiveRunId(res.data.run_id);
        setActiveEvalId(res.data.evaluation_id);
        // Automatically fetch results
        void fetchResults(res.data.run_id, res.data.evaluation_id);
      } else {
        setMethodStatus("error");
        setMethodError(res.error.message);
      }
    } finally {
      if (activeAbortControllerRef.current === controller) {
        activeAbortControllerRef.current = null;
      }
    }
  };

  // Step 8b: Method Run (Model Answer)
  const handleMethodRun = async () => {
    if (freezeStatus !== "success" || !benchLogicalId || methodStatus === "running") return;
    setMethodStatus("running");
    setMethodError(null);
    setActiveRunId(null);
    setActiveEvalId(null);
    setActiveRunKind(null);
    setMethodJobDetail(null);
    setJudgeStatus("idle");
    setJudgeError(null);

    const controller = new AbortController();
    activeAbortControllerRef.current = controller;

    try {
      const res = await startMethodRun({
        projectId: projectId.trim(),
        logicalId: benchLogicalId,
        revision: 1,
        model: methodModel.trim(),
        token: token.trim(),
        fetchFn,
        signal: controller.signal,
      });

      if (!res.ok) {
        setMethodStatus("error");
        setMethodError(res.error.message);
        return;
      }

      setActiveRunId(res.data.run_id);
      setActiveRunKind("model");
      const jobId = res.data.job_id;

      // Poll answer job
      const deadline = Date.now() + 600_000;
      while (Date.now() < deadline) {
        if (controller.signal.aborted) break;

        const jobRes = await getJobStatus({
          jobId,
          token: token.trim(),
          fetchFn,
          signal: controller.signal,
        });

        if (!jobRes.ok) {
          setMethodStatus("error");
          setMethodError(jobRes.error.message);
          return;
        }

        setMethodJobDetail(jobRes.data);
        const s = jobRes.data.status;
        if (s === "succeeded") {
          setMethodStatus("success");
          void fetchResults(res.data.run_id);
          return;
        }
        if (s === "failed") {
          setMethodStatus("error");
          setMethodError("模型回答任務失敗");
          return;
        }
        if (s === "cancelled") {
          setMethodStatus("error");
          setMethodError("模型回答任務已取消");
          return;
        }

        await new Promise((r) => setTimeout(r, 3000));
      }

      if (Date.now() >= deadline) {
        setMethodStatus("error");
        setMethodError("模型回答任務逾時");
      }
    } finally {
      if (activeAbortControllerRef.current === controller) {
        activeAbortControllerRef.current = null;
      }
    }
  };

  // Step 9: Judge Run
  const handleJudge = async () => {
    if (
      activeRunKind !== "model" ||
      (methodStatus !== "success" && methodStatus !== "error") ||
      judgeStatus === "running" ||
      !activeRunId
    ) {
      return;
    }
    const runId = activeRunId;
    setJudgeStatus("running");
    setJudgeError(null);

    const controller = new AbortController();
    activeAbortControllerRef.current = controller;

    try {
      const res = await startJudgeRun({
        projectId: projectId.trim(),
        runId,
        model: methodModel.trim(),
        token: token.trim(),
        fetchFn,
        signal: controller.signal,
      });

      if (!res.ok) {
        setJudgeStatus(res.status === 409 ? "conflict_409" : "error");
        setJudgeError(res.error.message);
        return;
      }

      const jobId = res.data.job_id;

      // Poll judge job every 3s up to 600s
      const deadline = Date.now() + 600_000;
      while (Date.now() < deadline) {
        if (controller.signal.aborted) break;

        const jobRes = await getJobStatus({
          jobId,
          token: token.trim(),
          fetchFn,
          signal: controller.signal,
        });

        if (!jobRes.ok) {
          setJudgeStatus("error");
          setJudgeError(jobRes.error.message);
          return;
        }

        const s = jobRes.data.status;
        if (s === "succeeded") {
          setJudgeStatus("success");
          setActiveEvalId(null);
          void fetchResults(runId);
          return;
        }
        if (s === "failed") {
          // e.g. the budget ran out for some questions: the job still wrote an evaluation (those questions are errors with a reason)
          setJudgeStatus("error");
          setJudgeError("評分任務沒有全部成功（例如預算用完）：沒評到的題目在結果裡是「錯誤」並附原因，不是 0 分。");
          setActiveEvalId(null);
          void fetchResults(runId);
          return;
        }
        if (s === "cancelled") {
          setJudgeStatus("error");
          setJudgeError("評分任務已取消");
          return;
        }

        await new Promise((r) => setTimeout(r, 3000));
      }

      if (Date.now() >= deadline) {
        setJudgeStatus("error");
        setJudgeError("評分任務逾時");
      }
    } finally {
      if (activeAbortControllerRef.current === controller) {
        activeAbortControllerRef.current = null;
      }
    }
  };

  // Step 9: Results Fetch
  const fetchResults = async (runIdToFetch: string, evalIdToFetch?: string) => {
    setResultsStatus("running");
    setResultsError(null);

    const controller = new AbortController();
    activeAbortControllerRef.current = controller;

    try {
      const res = await getRunResults({
        runId: runIdToFetch,
        evaluationId: evalIdToFetch || activeEvalId || undefined,
        token: token.trim(),
        fetchFn,
        signal: controller.signal,
      });

      if (res.ok) {
        setResultsStatus("success");
        setResultsData(res.data);
      } else {
        setResultsStatus("error");
        setResultsError(res.error.message);
      }
    } finally {
      if (activeAbortControllerRef.current === controller) {
        activeAbortControllerRef.current = null;
      }
    }
  };

  const done = (st: StepState) => st === "success";
  const steps: { key: string; label: string; state: StepState }[] = [
    { key: "auth", label: "登入", state: isAuthValid ? "success" : "idle" },
    { key: "upload", label: "上傳", state: uploadStatus },
    { key: "parse", label: "解析", state: parseStatus },
    { key: "plan", label: "建題庫", state: planStatus },
    { key: "generate", label: "出題", state: genStatus },
    { key: "validate", label: "驗題", state: valStatus },
    { key: "freeze", label: "凍結", state: freezeStatus },
    { key: "method", label: "作答", state: methodStatus },
    { key: "results", label: "結果", state: resultsStatus },
  ];
  const firstOpen = steps.find((st) => !(done(st.state) || (st.key === "freeze" && st.state === "conflict_409")));
  const failed = steps.find((st) => st.state === "error");
  const nextStepKey = failed?.key ?? firstOpen?.key ?? "done";
  const NEXT_HINTS: Record<string, string> = {
    auth: "在步驟 1 貼上 Token 與 Project ID。",
    upload: "在步驟 2 選一份 PDF 並上傳。",
    parse: "在步驟 3 按「開始解析」（通常幾十秒）。",
    plan: "在步驟 4 輸入一個 Benchmark 名稱，建立題庫與素材計畫。",
    generate: "在步驟 5 按「開始生成題目」。這一步會呼叫真實模型、會花一點錢（幾分錢美金）。",
    validate: "在步驟 6 按「開始自動驗題」，讓模型檢查每一題是否有根據。",
    freeze: "在步驟 7 凍結題庫。只有每一題都通過驗題才會成功；沒過的不會被刪掉。",
    method: "在步驟 8 讓一個方法回答所有題目（詞彙 baseline 不花錢；模型回答方法會花錢）。",
    results: "在步驟 9 看評分結果。",
    done: "下面的結果與逐題得分就是這次評測。分數都是暫定的（provisional），人工審查尚未進行。",
  };
  const nextHint = failed ? `「${failed.label}」這一步失敗了，請看該步驟的錯誤訊息後重試。` : NEXT_HINTS[nextStepKey];

  return (
    <section aria-label="工作流程頁面" style={{ marginTop: "1rem" }}>
      <h2 className="page-title">評測工作流程</h2>
      <p className="muted">
        依序完成：上傳 PDF → 解析 → 出題 → 驗題 → 凍結 → 讓方法作答 → 看分數。每一步完成後，下一步才會開放。
      </p>

      <ol className="stepper" aria-label="進度">
        {steps.map((st, i) => (
          <li key={st.label} className={`stepper-item stepper-${st.state}`} aria-current={st.key === nextStepKey ? "step" : undefined}>
            <span className="stepper-dot">{st.state === "success" ? "✓" : i + 1}</span>
            <span className="stepper-label">{st.label}</span>
          </li>
        ))}
      </ol>
      <div className="next-hint" role="note" data-testid="next-hint">
        <strong>{nextStepKey === "done" ? "全部完成" : "現在該做："}</strong> {nextHint}
      </div>

      {/* Step 1 Card: Credentials and Project */}
      <div style={cardStyle}>
        <div style={headerStyle}>
          <h3 style={{ margin: 0 }}>步驟 1: 設定認證與專案</h3>
          <span style={{ ...badgeStyle, ...getStatusStyle(isAuthValid ? "success" : "idle") }}>
            {getStatusLabel(isAuthValid ? "success" : "idle")}
          </span>
        </div>
        {autoLogin ? (
          <p data-testid="workflow-autologin" style={{ marginTop: "0.75rem" }}>
            本機開發模式：已自動登入（專案 {projectId}）。不需要輸入任何 Token。
          </p>
        ) : (
        <div style={{ marginTop: "0.75rem" }}>
            <div style={{ marginBottom: "0.5rem" }}>
              <label htmlFor="workflow-token" style={{ display: "block", fontWeight: "bold" }}>
                認證 Token (Bearer Token) *
              </label>
              <input
                id="workflow-token"
                type="password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder="lab_act_<actor_uuid>_<hmac_sha256>"
                style={{ width: "100%", maxWidth: "500px", padding: "0.25rem" }}
                autoComplete="off"
              />
            </div>
            <div style={{ marginBottom: "0.5rem" }}>
              <label htmlFor="workflow-project-id" style={{ display: "block", fontWeight: "bold" }}>
                Project ID (UUID) *
              </label>
              <input
                id="workflow-project-id"
                type="text"
                value={projectId}
                onChange={(e) => setProjectId(e.target.value)}
                placeholder="00000000-0000-0000-0000-000000000000"
                style={{ width: "100%", maxWidth: "500px", padding: "0.25rem" }}
              />
              {projectId.trim() && !validateUuid(projectId.trim()) && (
                <span style={{ color: "#c00", fontSize: "0.85rem" }}>Project ID 格式必須為有效 UUID</span>
              )}
            </div>
            <p className="help">
              不知道填什麼？在專案資料夾執行 <code>scripts/demo.sh ui</code>，會自動登入；或把終端機印出的 <code>PROJECT_ID</code> 與 <code>TOKEN</code> 貼到這裡。
              Token 只放在這個分頁的記憶體裡，不會存到瀏覽器、也不會寫進紀錄。
            </p>
        </div>
        )}
      </div>

      {/* Step 2 Card: Upload PDF */}
      <div style={cardStyle}>
        <div style={headerStyle}>
          <h3 style={{ margin: 0 }}>步驟 2: 上傳 PDF 文件</h3>
          <span style={{ ...badgeStyle, ...getStatusStyle(uploadStatus) }}>
            {getStatusLabel(uploadStatus)}
          </span>
        </div>
        <div style={{ marginTop: "0.75rem" }}>
          <div style={{ marginBottom: "0.5rem" }}>
            <label htmlFor="workflow-pdf-file" style={{ display: "block" }}>
              選擇 PDF 檔案 *
            </label>
            <input
              id="workflow-pdf-file"
              type="file"
              accept="application/pdf,.pdf"
              onChange={(e) => setUploadFile(e.target.files?.[0] ?? null)}
              disabled={!isAuthValid || uploadStatus === "running"}
            />
          </div>
          <div style={{ marginBottom: "0.5rem" }}>
            <label htmlFor="workflow-upload-logical-id" style={{ display: "block" }}>
              自訂 Logical ID（選填）
            </label>
            <input
              id="workflow-upload-logical-id"
              type="text"
              value={uploadLogicalId}
              onChange={(e) => setUploadLogicalId(e.target.value)}
              placeholder="例如 demo-doc-1"
              style={{ width: "100%", maxWidth: "300px" }}
              disabled={!isAuthValid || uploadStatus === "running"}
            />
          </div>
          <button
            type="button"
            onClick={handleUpload}
            disabled={!isAuthValid || !uploadFile || uploadStatus === "running"}
            style={buttonStyle}
          >
            {uploadStatus === "running" ? "上傳中…" : "上傳 PDF"}
          </button>

          {uploadStatus === "running" && <p role="status" style={{ color: "#0066cc" }}>文件上傳中，請稍候…</p>}
          {uploadError && (
            <div role="alert" style={errorBoxStyle}>
              <strong>上傳失敗：</strong> {uploadError}
            </div>
          )}

          {uploadedDoc && (
            <div data-testid="upload-result" style={successBoxStyle}>
              <p style={{ margin: "0 0 0.5rem 0", fontWeight: "bold", color: "#137333" }}>文件上傳成功！</p>
              <ul style={{ margin: 0, paddingLeft: "1.2rem", fontSize: "0.9rem" }}>
                <li>Document ID: <strong data-testid="step2-doc-id">{uploadedDoc.id}</strong></li>
                <li>頁數: <strong data-testid="step2-page-count">{uploadedDoc.page_count}</strong> 頁</li>
                <li>SHA-256: {uploadedDoc.sha256 || "—"}</li>
                <li>檔案大小: {uploadedDoc.size_bytes !== undefined ? `${uploadedDoc.size_bytes} 位元組` : "—"}</li>
              </ul>
            </div>
          )}
        </div>
      </div>

      {/* Step 3 Card: Parse */}
      <div style={cardStyle}>
        <div style={headerStyle}>
          <h3 style={{ margin: 0 }}>步驟 3: 文件解析 (Parse)</h3>
          <span style={{ ...badgeStyle, ...getStatusStyle(parseStatus) }}>
            {getStatusLabel(parseStatus)}
          </span>
        </div>
        <div style={{ marginTop: "0.75rem" }}>
          <p style={{ margin: "0 0 0.5rem 0", fontSize: "0.9rem", color: "#555" }}>
            啟動後端 Unstructured 進行結構解析，並輪詢任務進度直到完成。
          </p>
          <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
            <button
              type="button"
              onClick={handleParse}
              disabled={!uploadedDoc || parseStatus === "running"}
              style={buttonStyle}
            >
              {parseStatus === "running" ? "解析中…" : "開始解析"}
            </button>
            {parseStatus === "running" && (
              <button
                type="button"
                onClick={handleCancelParse}
                style={{ ...buttonStyle, backgroundColor: "#fee", borderColor: "#f99" }}
              >
                取消解析
              </button>
            )}
          </div>

          {parseStatus === "running" && (
            <p role="status" style={{ color: "#0066cc", marginTop: "0.5rem" }}>
              解析任務執行中（每 3 秒輪詢）… {parseJobDetail ? `當前狀態：${parseJobDetail.status}` : ""}
            </p>
          )}
          {parseError && (
            <div role="alert" style={errorBoxStyle}>
              <strong>解析失敗：</strong> {parseError}
            </div>
          )}

          {parseJobDetail && (
            <div style={parseStatus === "success" ? successBoxStyle : infoBoxStyle}>
              <p style={{ margin: "0 0 0.25rem 0", fontWeight: "bold" }}>
                {parseStatus === "success" ? "解析任務完成！" : "解析任務進度："}
              </p>
              <ul style={{ margin: 0, paddingLeft: "1.2rem", fontSize: "0.9rem" }}>
                <li>Job ID: <strong data-testid="step3-job-id">{parseJobDetail.id || parseJobId || "—"}</strong></li>
                <li>狀態: <strong data-testid="step3-status">{parseJobDetail.status || "—"}</strong></li>
                <li>階段 (Stage): {parseJobDetail.stage || "—"}</li>
                <li>重試次數: {parseJobDetail.attempt ?? "—"}</li>
              </ul>
            </div>
          )}
        </div>
      </div>

      {/* Step 4 Card: Benchmark + Plan */}
      <div style={cardStyle}>
        <div style={headerStyle}>
          <h3 style={{ margin: 0 }}>步驟 4: 建立 Benchmark 與素材計畫</h3>
          <span style={{ ...badgeStyle, ...getStatusStyle(planStatus) }}>
            {getStatusLabel(planStatus)}
          </span>
        </div>
        <div style={{ marginTop: "0.75rem" }}>
          <div style={{ marginBottom: "0.5rem" }}>
            <label htmlFor="workflow-bench-id" style={{ display: "block" }}>
              Benchmark Logical ID（選填，留空自動產生）
            </label>
            <input
              id="workflow-bench-id"
              type="text"
              value={benchLogicalIdInput}
              onChange={(e) => setBenchLogicalIdInput(e.target.value)}
              placeholder="例如 demo-bench-1"
              style={{ width: "100%", maxWidth: "300px" }}
              disabled={parseStatus !== "success" || planStatus === "running"}
            />
          </div>
          <button
            type="button"
            onClick={handleCreateAndPlan}
            disabled={parseStatus !== "success" || planStatus === "running"}
            style={buttonStyle}
          >
            {planStatus === "running" ? "建立中…" : "建立 Benchmark 與素材計畫"}
          </button>

          {planStatus === "running" && <p role="status" style={{ color: "#0066cc" }}>正在建立 Benchmark 並釘選解析結果…</p>}
          {planError && (
            <div role="alert" style={errorBoxStyle}>
              <strong>建立計畫失敗：</strong> {planError}
            </div>
          )}

          {planResult && (
            <div style={successBoxStyle}>
              <p style={{ margin: "0 0 0.5rem 0", fontWeight: "bold", color: "#137333" }}>素材計畫建立成功！</p>
              <ul style={{ margin: 0, paddingLeft: "1.2rem", fontSize: "0.9rem" }}>
                <li>Benchmark ID: <strong>{planResult.logical_id || benchLogicalId || "—"}</strong></li>
                <li>狀態: {planResult.status || "—"}</li>
                <li>
                  可用頁數 (Usable Pages):{" "}
                  {planResult.pages?.usable && planResult.pages.usable.length > 0
                    ? planResult.pages.usable.map((p) => p + 1).join(", ")
                    : "—"}
                </li>
                <li>合規性預測: {planResult.compliance_projection || "—"}</li>
              </ul>
            </div>
          )}
        </div>
      </div>

      {/* Step 5 Card: Generate Questions */}
      <div style={cardStyle}>
        <div style={headerStyle}>
          <h3 style={{ margin: 0 }}>步驟 5: 生成題目 (Generate)</h3>
          <span style={{ ...badgeStyle, ...getStatusStyle(genStatus) }}>
            {getStatusLabel(genStatus)}
          </span>
        </div>
        <div style={{ marginTop: "0.75rem" }}>
          <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", marginBottom: "0.5rem" }}>
            <div>
              <label htmlFor="workflow-gen-model" style={{ display: "block" }}>模型 (Model)</label>
              <input
                id="workflow-gen-model"
                type="text"
                value={genModel}
                onChange={(e) => setGenModel(e.target.value)}
                disabled={planStatus !== "success" || genStatus === "running"}
                style={{ width: "250px" }}
              />
            </div>
            <div>
              <label htmlFor="workflow-gen-count" style={{ display: "block" }}>要出幾題</label>
              <input
                id="workflow-gen-count"
                type="number"
                min={1}
                max={genEst.value?.plan_total ?? 1000}
                value={genCount}
                onChange={(e) => setGenCount(e.target.value)}
                disabled={planStatus !== "success" || genStatus === "running"}
                style={{ width: "110px" }}
              />
              {genEst.value?.plan_total ? <span className="muted small"> （這份文件建議 {genEst.value.plan_total} 題）</span> : null}
            </div>
          </div>

          {genEst.value?.split && (
            <p data-testid="gen-split" className="small" style={{ margin: "0 0 0.25rem 0" }}>
              {genEst.value.question_count} 題 = 一般 {genEst.value.split.general} ＋ 跨頁 {genEst.value.split.cross_page} ＋ 易混淆 {genEst.value.split.confusable}
            </p>
          )}
          {genCount !== "" && !countValid && <p role="note" className="small" style={{ color: "#c5221f" }}>題數要是 1 以上的整數</p>}
          <EstimateLine est={genEst} label="開始前先告訴你：這一步" testId="gen-estimate" />
          <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
            <button
              type="button"
              onClick={handleGenerate}
              disabled={planStatus !== "success" || genStatus === "running" || (genCount !== "" && !countValid)}
              style={buttonStyle}
            >
              {genStatus === "running" ? "生成中…" : "開始生成題目"}
            </button>
            <span style={moneyWarningStyle}>會呼叫真實模型、會花錢</span>
          </div>

          {genStatus === "running" && (
            <p role="status" style={{ color: "#0066cc", marginTop: "0.5rem" }}>
              題目生成中（輪詢進度）… {genProgress?.job ? `任務狀態：${genProgress.job.status}` : ""}
            </p>
          )}
          {genError && (
            <div role="alert" style={errorBoxStyle}>
              <strong>生成失敗：</strong> {genError}
            </div>
          )}

          {genProgress && (
            <div style={genStatus === "success" ? successBoxStyle : infoBoxStyle}>
              <p style={{ margin: "0 0 0.5rem 0", fontWeight: "bold" }}>
                {genStatus === "success" ? "題目生成完成！" : "生成進度："}
              </p>
              <ul style={{ margin: 0, paddingLeft: "1.2rem", fontSize: "0.9rem" }}>
                <li>任務狀態: {genProgress.job?.status || "—"}</li>
                <li>總題數: {genProgress.questions ?? "—"} 題</li>
                <li>
                  各 Bucket 題數: 一般 {genProgress.by_bucket?.general ?? "—"} (目標 {genProgress.general_target ?? "—"}) /
                  跨頁 {genProgress.by_bucket?.cross_page ?? "—"} (目標 {genProgress.cross_page_target ?? "—"}) /
                  易混淆 {genProgress.by_bucket?.confusable ?? "—"} (目標 {genProgress.confusable_target ?? "—"})
                </li>
                <li>題目缺額 (Shortfall): {genProgress.shortfall ?? "—"}</li>
                <li>
                  已支出: {genProgress.ledger?.spent_micros !== undefined ? `約 ${(genProgress.ledger.spent_micros / 1_000_000).toFixed(4)} 美元` : "—"}
                </li>
              </ul>
            </div>
          )}
        </div>
      </div>

      {/* Step 6 Card: Automated Question Validation */}
      <div style={cardStyle}>
        <div style={headerStyle}>
          <h3 style={{ margin: 0 }}>步驟 6: 自動驗題 (Validate)</h3>
          <span style={{ ...badgeStyle, ...getStatusStyle(valStatus) }}>
            {getStatusLabel(valStatus)}
          </span>
        </div>
        <div style={{ marginTop: "0.75rem" }}>
          <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", marginBottom: "0.5rem" }}>
            <div>
              <label htmlFor="workflow-val-model" style={{ display: "block" }}>模型 (Model)</label>
              <input
                id="workflow-val-model"
                type="text"
                value={valModel}
                onChange={(e) => setValModel(e.target.value)}
                disabled={genStatus !== "success" || valStatus === "running"}
                style={{ width: "250px" }}
              />
            </div>
          </div>

          <p data-testid="validate-explain" className="muted small" style={{ margin: "0 0 0.25rem 0" }}>
            自動驗題檢查兩件事：(1) 程式比對每一題的答案、要點和數字是不是真的出現在模型看到的原文裡；(2) 再用模型判斷「光看那段原文，答不答得出來」。
            兩項都過才算通過；沒通過的題目不能凍結，除非你把它排除（會記錄理由、不會刪除）。這是同一個模型做的自動檢查，不是人工審查。
          </p>
          <EstimateLine est={valEst} label="開始前先告訴你：這一步" testId="val-estimate" />
          <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
            <button
              type="button"
              onClick={handleValidate}
              disabled={genStatus !== "success" || valStatus === "running"}
              style={buttonStyle}
            >
              {valStatus === "running" ? "驗證中…" : "開始自動驗題"}
            </button>
            <span style={moneyWarningStyle}>會呼叫真實模型、會花錢</span>
          </div>

          {valStatus === "running" && (
            <p role="status" style={{ color: "#0066cc", marginTop: "0.5rem" }}>
              自動驗題中（輪詢進度）… {valProgress?.job ? `任務狀態：${valProgress.job.status}` : ""}
            </p>
          )}
          {valError && (
            <div role="alert" style={errorBoxStyle}>
              <strong>驗題失敗：</strong> {valError}
            </div>
          )}

          {valProgress?.validation && (
            <div style={valStatus === "success" ? successBoxStyle : infoBoxStyle}>
              <p style={{ margin: "0 0 0.5rem 0", fontWeight: "bold" }}>驗證結果題數統計：</p>
              <ul style={{ margin: 0, paddingLeft: "1.2rem", fontSize: "0.9rem" }}>
                <li>通過 (Pass): <strong>{valProgress.validation.pass ?? "—"}</strong></li>
                <li>失敗 (Fail): {valProgress.validation.fail ?? "—"}</li>
                <li>需人工審查 (Needs Review): {valProgress.validation.needs_review ?? "—"}</li>
                <li>未驗證 (Unvalidated): {valProgress.validation.unvalidated ?? "—"}</li>
                {valProgress.validation.note && <li style={{ color: "#666" }}>說明: {valProgress.validation.note}</li>}
              </ul>
            </div>
          )}
        </div>
      </div>

      {/* Questions list: appears as soon as questions exist */}
      {(genProgress?.questions ?? 0) > 0 && benchLogicalId && (
        <div style={cardStyle} data-testid="questions-card">
          <div style={headerStyle}>
            <h3 style={{ margin: 0 }}>題目一覽</h3>
            <span style={{ ...badgeStyle, ...getStatusStyle("success") }}>{genProgress?.questions} 題</span>
          </div>
          <div style={{ marginTop: "0.75rem" }}>
            {genProgress?.benchmark_status === "validating" && freezeStatus !== "success" ? (
              <QuestionReview
                fetchFn={fetchFn}
                token={token}
                projectId={projectId}
                logicalId={benchLogicalId}
                reloadKey={`${genStatus}-${valStatus}-${freezeStatus}-${genProgress?.questions ?? 0}`}
              />
            ) : (
              <>
                <BenchmarkDownload fetchFn={fetchFn} token={token} projectId={projectId} logicalId={benchLogicalId} />
                <QuestionsPanel
                fetchFn={fetchFn}
                token={token}
                projectId={projectId}
                logicalId={benchLogicalId}
                reloadKey={`${genStatus}-${valStatus}-${freezeStatus}-${genProgress?.questions ?? 0}-${activeRunId ?? ""}-${resultsStatus}`}
                runId={activeRunId}
                evaluationId={activeEvalId}
              />
              </>
            )}
          </div>
        </div>
      )}

      {/* Step 7 Card: Freeze */}
      <div style={cardStyle}>
        <div style={headerStyle}>
          <h3 style={{ margin: 0 }}>步驟 7: 凍結 Benchmark (Freeze)</h3>
          <span style={{ ...badgeStyle, ...getStatusStyle(freezeStatus) }}>
            {getStatusLabel(freezeStatus)}
          </span>
        </div>
        <div style={{ marginTop: "0.75rem" }}>
          <p style={{ margin: "0 0 0.5rem 0", fontSize: "0.9rem", color: "#555" }}>
            合規性固定為 partial。每一題「未被排除」的題目都必須通過驗證才能凍結（至少要剩一題）；沒過的題目可以在上方「題目一覽」排除（會記錄理由、不會刪除）。若仍有未通過的題目，後端回傳 409 為誠實結果，非系統錯誤。
          </p>
          <button
            type="button"
            onClick={handleFreeze}
            disabled={valStatus !== "success" || freezeStatus === "running"}
            style={buttonStyle}
          >
            {freezeStatus === "running" ? "凍結中…" : "凍結 Benchmark"}
          </button>

          {freezeStatus === "running" && <p role="status" style={{ color: "#0066cc" }}>正在請求凍結 Benchmark…</p>}
          {freezeError && (
            <div role="alert" style={errorBoxStyle}>
              <strong>凍結請求發生錯誤：</strong> {freezeError}
            </div>
          )}

          {/* 409 Honest message */}
          {freezeStatus === "conflict_409" && freezeMessage409 && (
            <div role="alert" data-testid="freeze-409-message" style={warningBoxStyle}>
              <p style={{ margin: "0 0 0.25rem 0", fontWeight: "bold", color: "#b06000" }}>
                凍結未通過（409 誠實結果，非系統錯誤）：
              </p>
              <p style={{ margin: 0, fontSize: "0.9rem" }}>{freezeMessage409}</p>
              <p style={{ margin: "0.25rem 0 0 0", fontSize: "0.85rem", color: "#777" }}>
                基準測試需所有題目皆通過自動驗證方可凍結。此為模型輸出之真實評測現象，非系統異常。
              </p>
            </div>
          )}

          {/* 200 Success */}
          {freezeStatus === "success" && freezeLimits && (
            <div style={successBoxStyle}>
              <p style={{ margin: "0 0 0.5rem 0", fontWeight: "bold", color: "#137333" }}>
                Benchmark 成功凍結為 PARTIAL！
              </p>
              <p style={{ margin: "0 0 0.25rem 0", fontSize: "0.9rem", fontWeight: "bold" }}>系統限制與聲明：</p>
              <ul style={{ margin: 0, paddingLeft: "1.2rem", fontSize: "0.85rem" }}>
                {freezeLimits.map((limit, idx) => (
                  <li key={idx}>{limit}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>

      {/* Step 8 Card: Run Methods */}
      <div style={cardStyle}>
        <div style={headerStyle}>
          <h3 style={{ margin: 0 }}>步驟 8: 執行評測方法 (Run Method)</h3>
          <span style={{ ...badgeStyle, ...getStatusStyle(methodStatus) }}>
            {getStatusLabel(methodStatus)}
          </span>
        </div>
        <div style={{ marginTop: "0.75rem" }}>
          <p style={{ margin: "0 0 0.5rem 0", fontSize: "0.9rem", color: "#555" }}>
            在已凍結的 Benchmark 上執行方法評測。可選擇本機詞彙檢索 Baseline 或呼叫真實模型進行回答。
          </p>

          <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", alignItems: "center", marginBottom: "0.75rem" }}>
            <div>
              <button
                type="button"
                onClick={handleBaselineRun}
                disabled={freezeStatus !== "success" || methodStatus === "running"}
                style={buttonStyle}
              >
                {methodStatus === "running" ? "執行中…" : "詞彙 baseline"}
              </button>
              <span style={{ fontSize: "0.85rem", color: "#666", marginLeft: "0.5rem" }}>（本機檢索，無額外花費）</span>
            </div>

            <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
              <button
                type="button"
                onClick={handleMethodRun}
                disabled={freezeStatus !== "success" || methodStatus === "running"}
                style={buttonStyle}
              >
                {methodStatus === "running" ? "執行中…" : "模型回答方法"}
              </button>
              <span style={moneyWarningStyle}>會呼叫真實模型、會花錢</span>
            </div>
          </div>

          <EstimateLine est={methodEst} label="「模型回答方法」這一步" testId="method-estimate" />
          <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", marginBottom: "0.5rem" }}>
            <div>
              <label htmlFor="workflow-method-model" style={{ display: "block", fontSize: "0.85rem" }}>模型 (模型回答用)</label>
              <input
                id="workflow-method-model"
                type="text"
                value={methodModel}
                onChange={(e) => setMethodModel(e.target.value)}
                disabled={freezeStatus !== "success" || methodStatus === "running"}
                style={{ width: "220px", fontSize: "0.85rem" }}
              />
            </div>
          </div>

          {methodStatus === "running" && (
            <p role="status" style={{ color: "#0066cc", marginTop: "0.5rem" }}>
              方法評測執行中… {methodJobDetail ? `當前狀態：${methodJobDetail.status}` : ""}
            </p>
          )}
          {methodError && (
            <div role="alert" style={errorBoxStyle}>
              <strong>評測執行失敗：</strong> {methodError}
            </div>
          )}

          {activeRunId && (
            <p style={{ fontSize: "0.85rem", color: "#555", margin: "0.5rem 0 0 0" }}>
              Run ID: <strong>{activeRunId}</strong>
            </p>
          )}
        </div>
      </div>

      {/* Step 9 Card: Results */}
      <div style={cardStyle}>
        <div style={headerStyle}>
          <h3 style={{ margin: 0 }}>步驟 9: 評分結果 (Results)</h3>
          <span style={{ ...badgeStyle, ...getStatusStyle(resultsStatus) }}>
            {getStatusLabel(resultsStatus)}
          </span>
        </div>
        <div style={{ marginTop: "0.75rem" }}>
          <div data-testid="judge-block" style={{ marginBottom: "0.75rem" }}>
            <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", flexWrap: "wrap" }}>
              <button
                type="button"
                onClick={handleJudge}
                disabled={activeRunKind !== "model" || (methodStatus !== "success" && methodStatus !== "error") || judgeStatus === "running"}
                style={buttonStyle}
              >
                用評分模型評分
              </button>
              <span style={moneyWarningStyle}>會呼叫真實模型、會花錢</span>
            </div>
            <EstimateLine est={judgeEst} label="評分這一步" testId="judge-estimate" />
            <p className="help" data-testid="judge-help">
              {activeRunKind === "baseline"
                ? "目前這個 run 是詞彙 baseline，沒有模型答案，不能評分。要評分請在步驟 8 執行「模型回答方法」。"
                : activeRunKind !== "model" || (methodStatus !== "success" && methodStatus !== "error")
                  ? "先在步驟 8 執行「模型回答方法」，答完才能評分。"
                  : "評分模型和答題模型是同一家（同模型家族），沒有用人工標註校準，所以分數是暫定的。沒有作答的題目（棄答、失敗）不會呼叫評分模型。"}
            </p>
            {judgeStatus === "running" && <p role="status" style={{ color: "#0066cc" }}>評分中…（每個作答的題目一次呼叫）</p>}
            {judgeError && (
              <div role="alert" style={judgeStatus === "conflict_409" ? warningBoxStyle : errorBoxStyle}>
                <strong>{judgeStatus === "conflict_409" ? "目前不能評分：" : "評分失敗："}</strong> {judgeError}
              </div>
            )}
          </div>
          <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", marginBottom: "0.75rem" }}>
            <button
              type="button"
              onClick={() => activeRunId && fetchResults(activeRunId, activeEvalId || undefined)}
              disabled={!activeRunId || resultsStatus === "running"}
              style={buttonStyle}
            >
              {resultsStatus === "running" ? "讀取結果中…" : "重新整理評分結果"}
            </button>
          </div>

          {resultsStatus === "running" && <p role="status" style={{ color: "#0066cc" }}>讀取評分結果中…</p>}
          {resultsError && (
            <div role="alert" style={errorBoxStyle}>
              <strong>讀取結果失敗：</strong> {resultsError}
            </div>
          )}

          {resultsData && (
            <div style={{ marginTop: "0.5rem" }}>
              <p style={{ margin: "0 0 0.5rem 0", fontSize: "0.9rem" }}>
                Evaluation 狀態：<strong>{resultsData.evaluation?.status ?? resultsData.evaluation_status ?? "—"}</strong>
                {resultsData.provisional !== false ? "（暫定，不是正式成績）" : ""} ｜ Grader: {resultsData.evaluation?.grader_revision ?? resultsData.grader_revision ?? "—"}
              </p>

              {resultsData.judge && (
                <div data-testid="judge-summary" style={infoBoxStyle}>
                  <p style={{ margin: 0 }}>
                    評分模型 {resultsData.judge.model}（prompt {resultsData.judge.prompt_revision}）：評了 {resultsData.judge.judged_questions} 題，
                    其中 {resultsData.judge.questions_with_a_contradicted_fact} 題有被判定為與標準答案相反的事實。
                  </p>
                  <p style={{ margin: "0.25rem 0 0 0" }} className="muted small">限制：{resultsData.judge.limitation}</p>
                </div>
              )}
              {resultsData.excluded_questions && resultsData.excluded_questions.length > 0 && (
                <p data-testid="excluded-note" className="muted small" style={{ margin: "0.25rem 0" }}>
                  已排除 {resultsData.excluded_questions.length} 題（凍結前由 owner 排除，不評分）
                </p>
              )}
              {resultsData.submissions && (
                <p style={{ margin: "0 0 0.5rem 0", fontSize: "0.85rem", color: "#555" }}>
                  提交統計：回答 {resultsData.submissions.answered ?? "—"} 筆 ｜ 模型棄權 {resultsData.submissions.abstained_by_model ?? "—"} 筆 ｜ 方法失敗{" "}
                  {Object.values(resultsData.submissions.method_failures || {}).reduce((a, b) => a + b, 0)} 筆
                  {(resultsData.submissions.no_answer_retrieval_only ?? 0) > 0 && (
                    <span data-testid="no-answer-note"> ｜ 無答案（這個方法只做檢索、不產生答案）{resultsData.submissions.no_answer_retrieval_only} 筆</span>
                  )}
                </p>
              )}

              <table
                data-testid="results-table"
                style={{ width: "100%", borderCollapse: "collapse", marginTop: "0.5rem", fontSize: "0.9rem" }}
              >
                <thead>
                  <tr style={{ backgroundColor: "#f1f3f4", borderBottom: "2px solid #ccc", textAlign: "left" }}>
                    <th style={{ padding: "0.5rem" }}>指標名稱 (Metric)</th>
                    <th style={{ padding: "0.5rem" }}>已評分題數 (Scored)</th>
                    <th style={{ padding: "0.5rem" }}>錯誤題數 (Error)</th>
                    <th style={{ padding: "0.5rem" }}>待評分題數 (Pending)</th>
                    <th style={{ padding: "0.5rem" }}>平均得分 (Mean，0–100)</th>
                  </tr>
                </thead>
                <tbody>
                  {resultsData.metrics && resultsData.metrics.length > 0 ? (
                    resultsData.metrics.map((m) => {
                      const isPending = m.pending > 0;
                      return (
                        <tr key={m.metric_name} style={{ borderBottom: "1px solid #eee" }}>
                          <td style={{ padding: "0.5rem", fontWeight: "bold" }}>
                            {m.metric_name || "—"}
                            {METRIC_HELP[m.metric_name] && (
                              <div style={{ fontWeight: "normal", fontSize: "0.8rem", color: "#5f6368" }}>{METRIC_HELP[m.metric_name]}</div>
                            )}
                          </td>
                          <td style={{ padding: "0.5rem" }}>{m.scored !== undefined ? m.scored : "—"}</td>
                          <td style={{ padding: "0.5rem" }}>{m.error !== undefined ? m.error : "—"}</td>
                          <td style={{ padding: "0.5rem", color: isPending ? "#b06000" : "inherit" }}>
                            {isPending
                              ? `${m.pending}（尚未評分（judge 未執行））`
                              : m.pending !== undefined
                                ? m.pending
                                : "—"}
                          </td>
                          <td style={{ padding: "0.5rem" }}>
                            {m.mean !== null && m.mean !== undefined ? (
                              m.mean.toFixed(2)
                            ) : isPending ? (
                              <span style={{ color: "#b06000" }}>尚未評分（judge 未執行）</span>
                            ) : (
                              "—"
                            )}
                          </td>
                        </tr>
                      );
                    })
                  ) : (
                    <tr>
                      <td colSpan={5} style={{ padding: "1rem", textAlign: "center", color: "#666" }}>
                        無指標資料
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}

          {!resultsData && resultsStatus !== "running" && !resultsError && (
            <p style={{ color: "#666", fontSize: "0.9rem" }}>尚未取得評分結果。請先於步驟 8 執行方法。</p>
          )}
        </div>
      </div>
    </section>
  );
}

// -----------------------------------------------------------------------------
// Styles
// -----------------------------------------------------------------------------
const FAILURE_TEXT: Record<string, string> = {
  budget_exhausted: "預算不夠，一次呼叫都送不出去。預算是依題數自動算的，通常是這個題庫之前已經花掉一部分額度：請減少題數，或重新建立一個題庫再試。",
  partial_calls: "有些呼叫失敗了（例如模型服務暫時出錯），成功的部分已經寫入；可以再按一次補足剩下的題目。",
  lab_gen_failed: "和模型服務的連線中斷，這一批的結果遺失；已送出的呼叫會保留預留的額度，不會當成沒花錢。請稍後再試。",
  lab_gen_refused: "出題程式拒絕送出（設定或價格有問題），沒有任何呼叫被送出，也沒有花錢。",
  no_calls: "沒有需要送出的呼叫（題目數量已經達到你要的題數）。",
  benchmark_not_generating: "題庫目前不在「出題中」的狀態，無法執行。",
  benchmark_not_validating: "題庫目前不在「驗題中」的狀態，無法執行。",
  internal_error: "系統內部錯誤，沒有花錢；請再試一次，若重複發生請回報。",
};

function explainFailure(what: "generate" | "validate", status: string, code?: string | null): string {
  const name = what === "generate" ? "題目生成" : "自動驗題";
  if (status === "cancelled") return `${name}已取消。`;
  const text = code ? FAILURE_TEXT[code] : undefined;
  return text ? `${name}失敗：${text}` : `${name}失敗${code ? `（${code}）` : ""}。`;
}

function EstimateLine({ est, label, testId }: { est: { value: RunEstimate | null; error: string | null }; label: string; testId: string }) {
  if (est.error) return <p data-testid={testId} className="muted small">無法估算費用：{est.error}</p>;
  if (!est.value) return null;
  const v = est.value;
  return (
    <p data-testid={testId} className="small" style={{ margin: "0.25rem 0" }}>
      {label}最多約 <strong>{v.estimated_max_usd} 美元</strong>（{v.calls} 次呼叫，依最壞情況估算，實際通常低很多）
      {v.bound ? "；這一步的提示長度用上限估算" : ""}
    </p>
  );
}

const METRIC_HELP: Record<string, string> = {
  recall_at_k: "檢索命中：標準答案需要的證據，有沒有被找回來（0–100）",
  complete_at_k: "完整命中：需要的證據是否全部找回來",
  localization_precision: "定位精準度（0–100，和其他分數同一個尺度）：回傳的區域有多少落在正確位置。整頁檢索天生偏低：0.8 代表 0.8%，不是 80%",
  textual_consistency: "文字一致性（這批題目不適用）",
  correctness: "答案正確性：答案是否包含標準答案的事實（需要評分模型）",
  citation_precision: "引用精準度：引用的段落是否真的支持該論述（需要評分模型）",
  citation_completeness: "引用完整度：每個論述是否都有被支持的引用（需要評分模型）",
  faithfulness: "忠實度：答案有多少論述被引用的文字支持（需要評分模型）",
};

const cardStyle: React.CSSProperties = {
  marginBottom: "1rem",
  padding: "1.1rem 1.25rem",
  border: "1px solid var(--border)",
  borderRadius: "10px",
  backgroundColor: "var(--surface)",
  boxShadow: "0 1px 2px rgba(60, 64, 67, 0.08)",
};

const headerStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  borderBottom: "1px solid var(--border)",
  paddingBottom: "0.6rem",
  gap: "0.75rem",
};

const badgeStyle: React.CSSProperties = {
  fontSize: "0.78rem",
  padding: "0.2rem 0.65rem",
  borderRadius: "999px",
  fontWeight: 600,
  whiteSpace: "nowrap",
};

const buttonStyle: React.CSSProperties = {
  padding: "0.5rem 1.1rem",
  cursor: "pointer",
  fontSize: "0.92rem",
  borderRadius: "8px",
  border: "1px solid var(--primary)",
  backgroundColor: "var(--primary)",
  color: "#fff",
  fontWeight: 600,
};

const moneyWarningStyle: React.CSSProperties = {
  color: "#c5221f",
  fontSize: "0.85rem",
  fontWeight: "bold",
};

const errorBoxStyle: React.CSSProperties = {
  marginTop: "0.5rem",
  padding: "0.5rem",
  backgroundColor: "#fce8e6",
  border: "1px solid #fad2cf",
  color: "#c5221f",
  fontSize: "0.9rem",
  borderRadius: "3px",
};

const successBoxStyle: React.CSSProperties = {
  marginTop: "0.5rem",
  padding: "0.6rem",
  backgroundColor: "#e6f4ea",
  border: "1px solid #ceead6",
  fontSize: "0.9rem",
  borderRadius: "3px",
};

const warningBoxStyle: React.CSSProperties = {
  marginTop: "0.5rem",
  padding: "0.6rem",
  backgroundColor: "#fef7e0",
  border: "1px solid #fdd663",
  fontSize: "0.9rem",
  borderRadius: "3px",
};

const infoBoxStyle: React.CSSProperties = {
  marginTop: "0.5rem",
  padding: "0.6rem",
  backgroundColor: "#f8f9fa",
  border: "1px solid #dadce0",
  fontSize: "0.9rem",
  borderRadius: "3px",
};
