// What the owner is told about a document (Issues #204 and #203): notes recorded at upload, and the reason of a rejection, in zh-TW.
export type Note = { text: string; tone: "info" | "warn" };

const FLAG_TEXT: Record<string, Note> = {
  xref_not_zero_indexed: { tone: "info", text: "PDF 的交叉參照表編號有個無害的小問題（物件從 1 開始編號），讀取器已在讀取時自動修正；檔案本身沒有被修改，存的就是你上傳的原檔。" },
  trailing_bytes_after_eof: { tone: "info", text: "PDF 結尾（%%EOF 之後）有多餘的位元組（有些掃描軟體會加），不影響讀取；檔案本身沒有被修改。" },
  no_text_layer: { tone: "warn", text: "這份 PDF 沒有文字層（抽樣的頁面都讀不到文字，通常是掃描圖片）。本系統只處理文字，不做 OCR，所以無法用它出題。請先用 OCR 軟體轉成可選取文字的 PDF，或改用文字 PDF。" },
};

export function flagNotes(flags: string[] | undefined): Note[] {
  return (flags ?? []).map((f) => FLAG_TEXT[f] ?? { tone: "info" as const, text: `文件備註：${f}` });
}

const REJECTION_TEXT: Record<string, string> = {
  corrupt_pdf: "這份 PDF 讀不了：檔案損壞或結構無效。請用 PDF 閱讀器開啟確認，或重新匯出一份。",
  pdf_parser_warning: "這份 PDF 讀得開，但讀取器回報了一個不在「無害清單」上的問題，所以沒有接受。可以試著用 PDF 工具另存成新檔再上傳。",
  invalid_pdf: "這個檔案不是有效的 PDF。",
  empty_file: "這個檔案是空的。",
  unsupported_encryption: "這份 PDF 有加密，請先移除密碼再上傳。",
  page_limit_exceeded: "這份 PDF 的頁數超過上限。",
  payload_too_large: "這個檔案超過大小上限。",
};

/** The reason of a rejected upload in words; the server's own message stays in parentheses so nothing is hidden. */
export function uploadErrorText(code: string | undefined, message: string): string {
  const zh = code ? REJECTION_TEXT[code] : undefined;
  return zh ? `${zh}（${message}）` : message;
}

/** The page numbers a plan found usable, bounded: the count and the first few, never an 834-number list. */
export function usablePagesSummary(usable: number[] | undefined, show = 12): string {
  if (!usable || usable.length === 0) return "—";
  const head = usable.slice(0, show).map((p) => p + 1).join(", ");
  return usable.length > show ? `共 ${usable.length} 頁（第 ${head} … 頁，其餘省略）` : `共 ${usable.length} 頁（第 ${head} 頁）`;
}
