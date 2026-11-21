import { describe, expect, it } from "vitest";
import { flagNotes, uploadErrorText, usablePagesSummary } from "./documentNotes";

describe("flagNotes", () => {
  it("says in words what was recorded, and that no scan can be used", () => {
    const n = flagNotes(["no_text_layer", "xref_not_zero_indexed", "trailing_bytes_after_eof"]);
    expect(n[0].tone).toBe("warn");
    expect(n[0].text).toContain("沒有文字層");
    expect(n[0].text).toContain("OCR");
    expect(n[1].text).toContain("沒有被修改");
    expect(n[2].text).toContain("%%EOF");
    expect(flagNotes(undefined)).toEqual([]);
    expect(flagNotes(["something_new"])[0].text).toContain("something_new");     // an unknown note is shown, not hidden
  });
});

describe("uploadErrorText", () => {
  it("separates 'cannot be read' from 'readable but with a reader problem not on the list' and keeps the server's message", () => {
    const a = uploadErrorText("corrupt_pdf", "document rejected: corrupt_pdf (the PDF cannot be read)");
    const b = uploadErrorText("pdf_parser_warning", "document rejected: pdf_parser_warning (...)");
    expect(a).toContain("讀不了");
    expect(b).toContain("讀得開");
    expect(b).toContain("無害清單");
    expect(a).not.toBe(b);
    expect(a).toContain("document rejected: corrupt_pdf");
    expect(uploadErrorText("something_else", "boom")).toBe("boom");
    expect(uploadErrorText(undefined, "boom")).toBe("boom");
  });
});

describe("usablePagesSummary", () => {
  it("is bounded however many pages are usable", () => {
    expect(usablePagesSummary(Array.from({ length: 834 }, (_, i) => i))).toBe("共 834 頁（第 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12 … 頁，其餘省略）");
    expect(usablePagesSummary([0, 2])).toBe("共 2 頁（第 1, 3 頁）");
    expect(usablePagesSummary([])).toBe("—");
    expect(usablePagesSummary(undefined)).toBe("—");
  });
});
