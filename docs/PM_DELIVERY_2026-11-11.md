# 11/11 前交付計畫（PM）

基準：2026-11-08（台灣時間），main `ce4047f4c5f6f545ab6629a1459098c2e6f48000`，[main CI 42443829066](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/actions/runs/42443829066)。目標是 **11/10 23:59 前完成正式驗收**，11/10 20:00 以候選版證據做發布判定。採用 [Epic #1](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/issues/1)、[發布 Issue #15](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/issues/15) 和 [SPEC 第 18、19 節](document-seperater-playground_SPEC_v0.1.md) 的完整定義。

基準版本已具備認證 PDF 上傳／下載（[#73](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/pull/73)）及確定性生成控制（[#75](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/pull/75)）。截至該 SHA，[發布追溯表](traceability.yaml) 登記 182 項，0 項完整通過、7 項部分完成、1 項阻擋、174 項待登記執行。這是發布證據的覆蓋狀態，已合併增量另計。真服務、人工、效能和還原仍缺證據，正式完工屬高風險目標，不能承諾如期達標。

## 審查與開發優先序

| 工作線 | 先交付 | 解鎖條件與下一步 |
| --- | --- | --- |
| 文件操作 | [#83](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/issues/83) 沿用 [#78](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/pull/78) 完成真瀏覽器 → FastAPI → PostgreSQL 的 F01 上傳、21 頁、下載雜湊、冪等重播及跨專案拒絕 | 可直接準備測試，因 #73 已在 main。另審 [#76](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/pull/76) 血緣 API 的查詢上限、權限及分頁，通過後逐項核對 [#67](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/issues/67)；完整 UI 仍由 #13 驗收。 |
| 遷移與持久化 | 先重審 [#77](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/pull/77) 的既有 DB 採納、checksums、鎖與重跑；[#74](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/pull/74) 在 #77 進 main 後補 Alembic 0010 及真 pgvector 驗證 | 所有 schema 變更依 main 串行分配 revision。[#79](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/pull/79) 與 #74 現同用 0010，沿用現有 PR 協調下一號。#79 需真 PostgreSQL 驗證凍結防繞過，另追完整四部分、獨立驗題與人工簽核資料。 |
| 私有 workflow | [#28](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/issues/28) 按 [#30](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/pull/30) → [#37](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/pull/37) → [#39](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/pull/39) 核對 pin、真 graph/client、持久化與恢復 | 授權 runner 提供兩個 SHA、實際 Go 測試與 smoke、獨立 reviewer；公開 CI 保持無私有憑證。與文件、遷移線並行。 |
| 評測整合 | 先接通解析 → 四部分題庫驗證與凍結 → 同一版本跑兩種獨立方法 → 分別呈現正確性、來源及 faithfulness | [#80](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/pull/80)/[#81](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/pull/81)/[#82](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/pull/82) 的讀取端點依呼叫端需求審查；用受限角色驗 gold 隔離。read API、fixture 或 mock 各自列驗收範圍。 |
| 發布證據 | [#15](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/issues/15) 同步收證，不等所有功能合併才準備 | #2 核對 main 保護與清理；#8/#9/#12 核對雙 parser、圖文必要性和人工校準；#6/#11 核對真模型；#14 核對負載、備份及隔離還原。 |

每位 coding agent 先修原 PR 的 review／CI，再認領單一可交付子任務；各工作線可由不同認領者並行。除直接解鎖上表或修復發布阻擋外，暫緩新增獨立元件、非必要重構與重複 PR。每輪先核對精確 head、適用 CI、實作及 review threads；小增量按適用測試驗收。每次合併後立即核對關閉條件與來源 branch，讓已驗收子案結案，父案以完整驗收為準。PM 在 Epic 記錄新證據、阻擋、下一步與責任角色，尊重現有認領。審查和合併依 [開發協議](DEVELOPMENT.md)，包括 PM 作者的獨立審查與 expected head squash merge。

## 時間點與交付判定

| 台灣時間 | 應有結果 |
| --- | --- |
| 11/08 21:00 | 核實哪個授權 runner 能用真 chat／embedding／vision、兩套 parser 模型／硬體及各至少 20 頁可授權 golden、人工覆核人員、私有 agentKit 存取、負載與還原環境。缺件在原 Issue 記錄取得方式、責任角色與預計可用時間。 |
| 11/09 20:00 | 目標完成 #83 的瀏覽器驗收，完成可合併 DB 增量的決策，讓 agentKit、解析、建題及評分工作線各有可重跑證據。無法跑完的流程列到實際停住的步驟。 |
| 11/10 12:00 | 固定候選 SHA；先行重跑真實全流程、人工覆核、500／600 頁與 log/vector 負載、隔離還原。每筆證據含 commit／schema／fixture／profile／actual／trace，記在 [追溯表](traceability.yaml) 與相應 Issue。候選 SHA 變更需補跑受影響證據。 |
| 11/10 20:00 | Go／no-go。只有 R01–R30、A01–A48、T01–T104 所有必要情境和 [六類真實門檻](RELEASE_EVIDENCE.md) 可獨立重現，另一位開發者能從乾淨環境重跑，且 #15 通過，才可正式發布並關閉 Epic。 |
| 11/10 23:59 | 若仍有缺口，保留 #15 和 #1，交付最後一個已驗證 SHA、可重現指令與阻擋清單。可展示實際通過的內部流程；若 #83 仍未通過，交付範圍以已驗收的 API 路徑為限。 |

憑證與私有資料只在授權環境使用。報告可列版本、去敏 trace 與證據位置；人工驗證、真解析、真模型、規模與還原各自留實際紀錄。發布檢查依 [查核指令](RELEASE_EVIDENCE.md) 執行，任何必要項被標為 blocked、skipped 或無法重現，結論為 `RELEASE_BLOCKED`。
