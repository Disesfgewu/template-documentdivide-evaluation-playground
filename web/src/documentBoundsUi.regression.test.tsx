import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { DocumentOperator } from "./DocumentOperator";
const id = "11111111-1111-1111-1111-111111111111";
const token = "ui-secret-canary";
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
function uploadInputs() {
  fireEvent.change(screen.getByLabelText(/認證 Token/i), {target:{value:token}});
  fireEvent.change(screen.getByLabelText(/Project ID/i), {target:{value:id}});
  fireEvent.change(document.getElementById("upload-file")!, {target:{files:[new File(["%PDF-1.4"],"test.pdf",{type:"application/pdf"})]}});
}
it("HTTP error message and code canaries are absent from upload and download DOM", async () => {
  const fetchFn = vi.fn(async () => new Response(JSON.stringify({error:{message:`proxy Bearer ${token}`, code:token}}),{status:502}));
  render(<DocumentOperator fetchFn={fetchFn as typeof fetch} />);
  uploadInputs();
  fireEvent.click(screen.getByRole("button",{name:"上傳 PDF"}));
  await waitFor(() => expect(screen.getByTestId("upload-error")).toBeTruthy());
  expect(screen.getByTestId("upload-error").textContent).not.toContain(token);
  fireEvent.change(screen.getByLabelText(/Document ID/i), {target:{value:id}});
  fireEvent.click(screen.getByRole("button",{name:"下載 PDF"}));
  await waitFor(() => expect(screen.getByTestId("download-error")).toBeTruthy());
  expect(document.body.textContent).not.toContain(token);
});
it("timeout after upload headers releases loading and retries with the same key", async () => {
  const originalTimeout = globalThis.setTimeout;
  vi.spyOn(globalThis,"setTimeout").mockImplementation(((callback: TimerHandler,delay?:number,...args:unknown[]) =>
    originalTimeout(callback,delay===120_000?10:delay,...args)) as typeof setTimeout);
  const keys:string[]=[];
  const fetchFn = vi.fn(async (_url:RequestInfo|URL, init?:RequestInit) => {
    keys.push((init?.headers as Record<string,string>)["Idempotency-Key"]);
    if(keys.length===1) return new Response(new ReadableStream({pull(){return new Promise(()=>{});}}),{status:201});
    return new Response(JSON.stringify({id,document_version_id:id,project_id:id,resource_id:id,artifact_id:id,logical_id:"default",revision:1,sha256:"a".repeat(64),size_bytes:8,page_count:1}),{status:201});
  });
  render(<DocumentOperator fetchFn={fetchFn as typeof fetch} generateIdempotencyKey={()=>"stable-timeout-key"} />);
  uploadInputs();
  fireEvent.click(screen.getByRole("button",{name:"上傳 PDF"}));
  await waitFor(()=>expect(screen.getByTestId("upload-error").textContent).toContain("逾時"));
  expect(screen.queryByRole("status")).toBeNull();
  expect(screen.queryByRole("button",{name:"取消上傳"})).toBeNull();
  fireEvent.click(screen.getByRole("button",{name:"上傳 PDF"}));
  await waitFor(()=>expect(screen.getByTestId("upload-success")).toBeTruthy());
  expect(keys).toEqual(["stable-timeout-key","stable-timeout-key"]);
});
