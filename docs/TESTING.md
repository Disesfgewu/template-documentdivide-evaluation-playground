# 測試入口

```bash
python3 -m pip install -r requirements-dev.txt
scripts/check.sh                 # 依變更檔案選擇 suite（相對 origin/main）
scripts/check.sh --scope all     # 全部 suite（main push / 發布 gate 使用）
```

- CI：`.github/workflows/ci.yml` 的 `checks` job。PR 走 `--scope auto`，main push/手動走 `--scope all`。
- 未對應任何 suite 的路徑會觸發全部 suite；任一 suite 0 個測試即失敗（防空綠）。
- 目前 suite：`governance`（必要檔、PR/Issue templates、workflow 無空 job/`continue-on-error`/`|| true`、CODEOWNERS 身份核實、相對連結、密鑰樣式掃描、branch 清理判定）。後續 Issue 新增應用程式 suite 時需在 `scripts/check.sh` 註冊並對應路徑。
- 本文件不代表 T01–T104 已執行；各 PR 驗證表須列實際指令、SHA 與結果。

## 合併後 branch 清理

```bash
python3 -m tools.governance.branch_cleanup --repo Disesfgewu/template-documentdivide-evaluation-playground --pr <N>            # dry run
python3 -m tools.governance.branch_cleanup --repo Disesfgewu/template-documentdivide-evaluation-playground --pr <N> --execute  # 刪除前重查
```

僅當 PR merged、同 repo、非 default/main/protected、目前 branch head 等於該 PR head、且無其他 open PR 以其為 head/base 時才刪除；否則輸出 `cleanup_pending` 與原因，exit 2。
