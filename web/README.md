# LAB web（React 骨架）

#3 的增量：Vite＋React＋TypeScript 骨架，只有一個「系統狀態」畫面，內容直接取自 LAB API 的 `/readyz`。

- 狀態：檢查中、錯誤（連不到、非 JSON、狀態碼與內容不一致）、就緒、未就緒。沒有任何假資料；HTTP 200 卻回 `not_ready`，或 503 卻回 `ready`，都視為錯誤。
- provider 為 `mock` 時，畫面會標示「本機假資料，非真模型」。

```bash
cd web
npm ci            # 版本由 package-lock.json 鎖定；.npmrc 停用 install scripts
npm run dev       # /api/* 代理到 LAB_API_URL（預設 http://127.0.0.1:8000）
npm test && npm run build
```

`.npmrc` 設定 `legacy-peer-deps`：npm 10.9 解析 `@vitejs/plugin-react` 的 optional peer 時會出現 `Cannot read properties of null (reading 'edgesOut')`。因此 peer 依賴（`@testing-library/dom`）改為明確列在 devDependencies。
