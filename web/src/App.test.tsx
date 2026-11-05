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
    const s = await fetchApiStatus(json(503, { status: "not_ready", provider_mode: "mock", checks: {} }));
    expect(s.kind).toBe("not_ready");
  });
  it("rejects a 200 that claims not_ready and a 503 that claims ready", async () => {
    expect((await fetchApiStatus(json(200, { status: "not_ready", provider_mode: "mock", checks: {} }))).kind).toBe("error");
    expect((await fetchApiStatus(json(503, { status: "ready", provider_mode: "mock", checks: {} }))).kind).toBe("error");
  });
  it("network failure and non-JSON are errors, never ready", async () => {
    const down = (async () => { throw new TypeError("fail"); }) as typeof fetch;
    expect((await fetchApiStatus(down)).kind).toBe("error");
    const html = (async () => new Response("<html>", { status: 502 })) as typeof fetch;
    expect((await fetchApiStatus(html)).kind).toBe("error");
  });
});

describe("App", () => {
  it("shows loading first", () => {
    render(<App load={() => new Promise<ApiStatus>(() => {})} />);
    expect(screen.getByRole("status").textContent).toContain("檢查中");
  });
  it("shows error state", async () => {
    render(<App load={async () => ({ kind: "error", message: "無法連線到 LAB API" })} />);
    expect((await screen.findByRole("alert")).textContent).toContain("無法連線");
  });
  it("labels mock provider and failing checks", async () => {
    render(<App load={async () => ({ kind: "not_ready", providerMode: "mock", checks: { database: { ok: false, detail: "down" } } })} />);
    const s = await screen.findByText(/API 未就緒/);
    expect(s.textContent).toContain("非真模型");
    expect(screen.getByText(/database：失敗/)).toBeTruthy();
  });
});
