# PM 與開發協議

## 工作真相與任務

SPEC: [完整規格](document-seperater-playground_SPEC_v0.1.md)。路線: [Epic #1](https://github.com/Disesfgewu/template-documentdivide-evaluation-playground/issues/1)。第一件任務為 #2。

| 順位 | Issue | 依賴 |
| --- | --- | --- |
| G0 治理、CI與main保護 | #2 | 無 |
| F1 骨架與環境 | #3 | #2 |
| C1 契約與fixtures | #4 | #3 |
| D1 資料庫/權限/稽核 | #5 | #4 |
| W1 workflow/成本/trace | #6 | #5 |
| P1 PDF資產與座標 | #7 | #5,#6 |
| P2 兩parser adapters | #8 | #7 |
| V1 圖文與跨頁驗證 | #9 | #8 |
| B1 題庫與凍結 | #10 | #9 |
| M1 方法/pgvector | #11 | #4,#6,#7 |
| S1 評分 | #12 | #10,#11 |
| U1 操作UI | #13 | #10,#11,#12 |
| O1 維運/安全/效能 | #14 | #13 |
| REL 完整驗收 | #15 | #14 |

Issue 中的最新依賴與 PM scope 決定執行順序。過大任務可提出小型子任務，但不能因一個部分 PR 合併就關閉整件。第一輪 #2 的治理文件/CI 可先分可驗收PR；例如必要檢查尚未存在時，先合入經審查的有效CI，再將實際check名設定為required，避免初始化死鎖。#2在所有治理驗收完成前仍保持開啟。

## main 與審查門檻

main只收可建置、相關必要測試通過、已審查的增量。最終產品完成取決於 #15及SPEC DoD，不能將main目前存在程式當成成品。Coding agent不得push main、merge、強推或改設定繞過PM。

PM每次審查記錄：

- PM-REVIEW: PASS / REQUEST_CHANGES / BLOCKED，精確head SHA。
- 適用Issue、R/T/A、已讀範圍與實際測試/CI證據。
- 缺陷含優先序、檔案/行、重現、影響、修正與回歸驗收。
- 未執行、mock、真服務、人工及效能測試的差別。

有新head就重審受影響內容，舊PASS不自動有效。Merge前重新核對head與checks，使用expected SHA；優先squash，無衝突、非draft、依賴已滿足、blocking findings已核對、repository規則也通過才merge。所有新必要程式碼要有實際對應測試，不能以空job綠燈代替。每PR驗相關增量；release跑全套T01–T104必要情境、真provider/人工/效能，blocked/skipped不能當passed。

同一GitHub帳號無法對自己的PR提交有效的獨立APPROVE。PM可用COMMENT留下審查判定，但不能偽造第二帳號或繞過required approvals；若設定需要不同身份，缺少時BLOCKED並請maintainer提供真正reviewer。CODEOWNERS也只能加入已核實可用的身份。

## 合併、關Issue與branch清理

PR完整完成Issue用Closes #N；部分PR用Refs #N。PM合併後查merged=true與merged head，再核對驗收，GitHub未自動close時由有權限者補關。不要close仍有外部依賴或未驗收部分的Issue。

優先啟用repo delete_branch_on_merge（#2驗證），merge後再核對遠端branch是否不存在。若仍存在，PM或coding agent需先確認：同repo、非main/default/protected、目前head等於被合併PR head、無其他open PR使用或依賴，然後才可用授權CLI/API刪除。檢查與刪除有競態時停止並重查；不刪fork。新提交另開Issue/PR處理，不猜測已合併。

沒有刪branch工具/權限時記cleanup pending，交有權限者清理，下輪追蹤；絕不能用把ref指到main的方式假刪除。Coding agent本地branch清理也須保留未提交工作，不能自動reset --hard。

## 排程與狀態

PM已建立每小時30分的定期檢查（Asia/Taipei），在Issue/PR/review中溝通，只有實質更新才通知。此為定期檢查，不是即時事件觸發，外部服務故障可能延後。

Coding agent需在自己的執行環境建立獨立的持久排程，建議每小時45分、Asia/Taipei，並立即跑一次。不能只在聊天承諾會繼續，也不能假定PM排程會執行coding agent。使用runner本身支持的schedule；沒有時使用可核對的系統scheduler，避免常駐sleep/busy polling。任務要有single-run lock、最大執行時間、日誌輪替與退出碼。排程不代表每輪都開新PR，優先修review/CI；只有狀態變動才留言。

狀態：ready → in_progress → in_review → changes_requested／blocked → in_review → merged → cleanup_pending／done。Issue狀態以GitHub為準，本地cache需可重建。工具或憑證不可用就記阻擋並結束本輪，不無限重試。

main保護、自動刪branch、CI與coding agent排程目前是 #2/agent首輪待驗證工作；本文件不是它們已在GitHub或外部主機啟用的證據。
