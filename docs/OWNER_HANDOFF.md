# 給 owner 的交接頁：現在有什麼、怎麼跑、哪些還不能相信

這一頁只做整理，不新增任何宣稱；細節在各自的文件（`QUICKSTART_DEMO.md`、`LAB_GEN.md`、`PARSE_TO_BENCHMARK.md`）。狀態以 Issue #1 的 PM 結論為準：**SPEC 發布判定仍是 RELEASE_BLOCKED**，
`docs/traceability.yaml` 仍是 0 項 passed。以下是已經能跑的「範圍內後端流程」，不是完成的產品。

## 流程（全在 main 上，CI 用假模型測過）

`上傳 PDF` → `解析（Unstructured fast）` → `建立 benchmark、釘住解析、素材計畫` → `生成題目（模型）` → `自動驗題（模型＋程式檢查）` → `owner 凍結（partial）` → `在凍結的 benchmark 上跑一個 baseline 並評分`

| 階段 | 做了什麼 | 看哪裡 |
|---|---|---|
| 解析與證據 | 只有乾淨頁面（沒有隱藏文字、有幾何）會成為證據；其他頁回報原因 | `PARSE_TO_BENCHMARK.md`、`POST .../plan` |
| 生成 | 一般題、跨頁題、干擾項題；每次呼叫最多 500 輸出 token；預算先預留；key 只進子程序環境 | `LAB_GEN.md`、`POST .../generate`、`GET .../generate` |
| 驗題 | 程式接地檢查＋第二次模型呼叫；兩者都過才 pass；不刪不改題 | `POST .../validate` |
| 凍結 | 只有 owner；每題都要 pass；compliance 一律 `partial`；資料庫強制 | `POST .../freeze` |
| 評分 | 本機詞彙檢索 baseline；recall／complete／localization 有分數；其餘 pending | `POST .../baseline-run`、`GET /api/v1/runs/{id}/results` |

## 第一次真實執行（只有你能做）

```bash
LAB_GEN_API_KEY=你的key scripts/demo.sh generate     # 或讓腳本讀 ./.env 的 ANTHROPIC_API_KEY（只印「using the API key from ./.env (not shown)」）
scripts/demo.sh down                                  # 看完清掉
```

- 會花錢：生成與驗證各約幾美分（預設每次上限 0.20 美元；價格是未經查證的牌價，可用 `LAB_GEN_PRICE_*` 覆寫）。
- **至今沒有任何人用真實模型跑過這整套**；CI 從不呼叫真實模型。第一次出問題很可能是我看不到的真實輸出造成的，把輸出貼回來就能修。
- 沒有 key：第 6–9 步印 `SKIPPED`，整個執行以 `RESULT: SKIPPED (no API key)` 和結束碼 3 結束，不會顯示 PASS。
- 如果真實模型的題目不是每題都通過自動驗證，第 8 步會印出「NOT FROZEN」和各 verdict 題數並跳過第 9 步：這是誠實的結果，不是 demo 壞掉。

## 這些結果「不能」被當成什麼

- **不是已驗證的 benchmark**：人工審核被你的範圍設為 `skipped`；驗證者與生成者是同一個提供者與模型（不是強意義的獨立）；「不看頁面也答得出來」的題目沒有被偵測；
  「這題真的需要兩頁」「這個干擾項真的會誤導」只有結構與接地檢查，沒有實測。
- **不是完整的 quota**：chart_table 沒有生成（快速解析沒有表格結構，不猜）；其他 bucket 不足 N、Q 時缺額會如實列出，不會湊數。
- **評分只是一部分**：correctness、citation_precision、citation_completeness、faithfulness 需要語意 judge，尚未執行，記為 pending（不是 0）；評估是 provisional。
- **hi_res／MinerU** 沒有環境（沒有 GPU 與模型），沒有跑。

## 仍然卡在你或外部的事

- 你手動 dispatch 並核准 `llm_smoke`（從 main）；我不會代你執行。
- G8 的殘留限制：藏在其他可見墨跡之下或之間的隱藏文字、Type3 字型、annotation 外觀串流沒有被偵測。
- 沒有獨立的 GitHub 核准者（#83、#13）；#15 需要外部輸入。
- 不穩定的 CI：Issue #145（瀏覽器啟動逾時，已有第一筆記錄）、Issue #162（暫存目錄清理，修正未證明有效）。
