## 問題與行為

描述具體觸發、修改後行為與範圍。

## Issue

Refs #N（部分）或 Closes #N（全部驗收完成）。列出尚未完成與依賴。

## SPEC 對應

R / T（含實際子情境）/ A IDs。不要把對應編號寫成已通過。

## 驗證

| 指令/CI連結 | commit SHA | 實際結果 | mock/真服務/人工/效能 |
| --- | --- | --- | --- |
| 填寫 | 填寫 | passed/failed/blocked/skipped | 填寫 |

列出相關回歸、負向/邊界測試與未驗項；新增/變更的測試不能只重複implementation。缺憑證請明列blocked。

## 資料與版本影響

migration、相容性、fixture/profile/provider版本、成本/權限/來源影響；無影響請寫明依據。

## Review修正（更新既有PR）

逐thread連結 → 修正commit → 驗證，不用「已resolved」代替證據。

## 合併與清理

- [ ] 可建置、相關必要檢查在最新head通過。
- [ ] 無未核對blocking review，交PM審查；作者不自行merge/main push。
- [ ] Closes只列完整完成Issue。
- [ ] 確認來源branch只服務本PR，合併後由PM/agent核對自動刪除；未刪記cleanup pending。
