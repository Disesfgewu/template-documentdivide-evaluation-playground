import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { App } from "./App";
import { fetchApiStatus, type ApiStatus } from "./status";

afterEach(cleanup);

const json = (status: number, body: unknown) =>
  (async () => new Response(JSON.stringify(body), { status })) as typeof fetch;

describe("fetchApiStatus", () => {
  it("maps 200 ready", async () => {
    const s = await fetchApiStatus(json(200, { status: "ready", provider_mode: "mock", checks: { database: { ok: true, detail: "x" } } }));
    expect(s.kind).toBe("ready");
  });
  it("maps 503 not_ready", async () => {
    const s = await fetchApiStatus(json(503, { status: "not_ready", provider_mode: "mock", checks: { database: { ok: false, detail: "down" } } }));
    expect(s.kind).toBe("not_ready");
  });
  it("never shows ready when a check failed or no checks exist", async () => {
    const failing = { status: "ready", provider_mode: "mock", checks: { database: { ok: false, detail: "down" } } };
    expect((await fetchApiStatus(json(200, failing))).kind).toBe("error");
    expect((await fetchApiStatus(json(200, { status: "ready", provider_mode: "mock", checks: {} }))).kind).toBe("error");
  });
  it("null body and malformed checks are errors, not exceptions", async () => {
    expect((await fetchApiStatus(json(200, null))).kind).toBe("error");
    expect((await fetchApiStatus(json(200, { status: "ready", provider_mode: "mock", checks: { db: "ok" } }))).kind).toBe("error");
    expect((await fetchApiStatus(json(200, { status: "ready", provider_mode: "mock", checks: [] }))).kind).toBe("error");
  });
  it("times out a hanging request", async () => {
    const hang = ((_u: unknown, init?: RequestInit) =>
      new Promise<Response>((_r, reject) => init?.signal?.addEventListener("abort", () => reject(new Error("aborted"))))) as typeof fetch;
    expect((await fetchApiStatus(hang, 20)).kind).toBe("error");
  });
  it("rejects a 200 that claims not_ready and a 503 that claims ready", async () => {
    expect((await fetchApiStatus(json(200, { status: "not_ready", provider_mode: "mock", checks: {} }))).kind).toBe("error");
    expect((await fetchApiStatus(json(503, { status: "ready", provider_mode: "mock", checks: { d: { ok: false, detail: "x" } } }))).kind).toBe("error");
  });
  it("network failure and non-JSON are errors, never ready", async () => {
    const down = (async () => { throw new TypeError("fail"); }) as typeof fetch;
    expect((await fetchApiStatus(down)).kind).toBe("error");
    const html = (async () => new Response("<html>", { status: 502 })) as typeof fetch;
    expect((await fetchApiStatus(html)).kind).toBe("error");
  });
  it("defaults generation to unknown when /readyz has no generation field", async () => {
    const s = await fetchApiStatus(
      json(200, {
        status: "ready",
        provider_mode: "mock",
        checks: { database: { ok: true, detail: "x" } },
      }),
    );
    expect(s.kind).toBe("ready");
    if (s.kind === "ready") {
      expect(s.generation).toEqual({
        state: "unknown",
        providerHost: null,
        model: null,
        lastCallAt: null,
        detail: null,
      });
    }
  });
});

describe("App", () => {
  it("states plainly which features are not connected yet", () => {
    render(<App load={() => new Promise<ApiStatus>(() => {})} />);
    const note = screen.getByRole("region", { name: "尚未接入的功能" }).textContent ?? "";
    expect(note).toContain("還沒有實作");
    expect(note).toContain("暫定");
    expect(note).toContain("人工審查尚未進行");
  });
  it("shows loading first", () => {
    render(<App load={() => new Promise<ApiStatus>(() => {})} />);
    expect(screen.getByRole("status").textContent).toContain("檢查中");
  });
  it("a rejected loader becomes an error, not an endless loading state", async () => {
    render(<App load={() => Promise.reject(new Error("boom"))} />);
    expect((await screen.findByRole("alert")).textContent).toContain("錯誤");
  });
  it("shows error state", async () => {
    render(<App load={async () => ({ kind: "error", message: "無法連線到 LAB API" })} />);
    expect((await screen.findByRole("alert")).textContent).toContain("無法連線");
  });
  it("shows failing checks and never labels the page as a fake model because of the legacy provider setting", async () => {
    render(
      <App
        load={async () => ({
          kind: "not_ready",
          providerMode: "mock",
          generation: {
            state: "unknown",
            providerHost: null,
            model: null,
            lastCallAt: null,
            detail: null,
          },
          checks: { database: { ok: false, detail: "down" } },
        })}
      />,
    );
    const s = await screen.findByText(/API 未就緒/);
    expect(s.textContent).not.toContain("非真模型");
    expect(document.body.textContent).not.toContain("非真模型");
    expect(screen.getByText(/database：失敗/)).toBeTruthy();
  });
  it("displays worker_running generation status with provider host and model", async () => {
    render(
      <App
        load={async () => ({
          kind: "ready",
          providerMode: "mock",
          generation: {
            state: "worker_running",
            providerHost: "api.anthropic.com",
            model: "fake-model",
            lastCallAt: "2026-11-13T12:00:00Z",
            detail: null,
          },
          checks: { database: { ok: true, detail: "ok" } },
        })}
      />,
    );
    const gen = await screen.findByTestId("generation-status");
    expect(gen.textContent).toContain("真實模型");
    expect(gen.textContent).toContain("api.anthropic.com");
    expect(gen.textContent).toContain("fake-model");
    expect(gen.textContent).toBe("題目生成：真實模型（api.anthropic.com），上次呼叫 fake-model");
  });
  it("displays no_worker generation status with explanation and detail title", async () => {
    render(
      <App
        load={async () => ({
          kind: "ready",
          providerMode: "mock",
          generation: {
            state: "no_worker",
            providerHost: null,
            model: null,
            lastCallAt: null,
            detail: "no question-generation worker is running; model steps will fail",
          },
          checks: { database: { ok: true, detail: "ok" } },
        })}
      />,
    );
    const gen = await screen.findByTestId("generation-status");
    expect(gen.textContent).toContain("沒有可用的出題 worker");
    expect(gen.textContent).toContain("沒有 API key");
    expect(gen.textContent).toContain("模型步驟會失敗");
    expect(gen.getAttribute("title")).toBe("no question-generation worker is running; model steps will fail");
  });
  it("displays unknown generation status", async () => {
    render(
      <App
        load={async () => ({
          kind: "ready",
          providerMode: "mock",
          generation: {
            state: "unknown",
            providerHost: null,
            model: null,
            lastCallAt: null,
            detail: null,
          },
          checks: { database: { ok: true, detail: "ok" } },
        })}
      />,
    );
    const gen = await screen.findByTestId("generation-status");
    expect(gen.textContent).toContain("狀態未知");
    expect(gen.textContent).toBe("題目生成：狀態未知");
  });
  it("opens on the workflow tab, the way a person actually uses it", () => {
    render(<App load={() => new Promise<ApiStatus>(() => {})} />);
    expect(screen.getByRole("region", { name: "工作流程頁面" })).toBeTruthy();
    expect(screen.queryByRole("region", { name: "文件操作員介面" })).toBeNull();
    expect(screen.getByTestId("next-hint").textContent).toContain("Token");
  });
  it("renders DocumentOperator section and system limits notice without PR or issue numbers in UI", () => {
    render(<App initialTab="operator" load={() => new Promise<ApiStatus>(() => {})} />);
    expect(screen.getByRole("region", { name: "文件操作員介面" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "系統限制與功能說明" }).textContent).toContain("後端服務可用性");
    expect(document.body.textContent).not.toMatch(/PR\s*#\d+/i);
    expect(document.body.textContent).not.toMatch(/#\d+/);
  });
});
