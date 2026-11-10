# agentKit checkout（`Disesfgewu/agentkit` 已於 2026-11-10 公開）

> 更新：agentKit 已公開，下面「需要唯讀憑證」的說法已過時：任何人都能直接 `git submodule update --init`，CI 的 `llm-smoke` job 也不再使用 token。標準 clone 與公開 CI 仍預設不初始化 submodule（為了速度與一致性），不是因為權限。


The playground's standard clone and public CI use the worker scaffold without
initializing submodules. Keep the checkout non-recursive for contributors and
CI runners without agentKit access. The pinned revision is the gitlink at
`third_party/agentkit`.

## Authorized verification

From a clean playground checkout, use a Git credential helper already
authorized for read-only access to agentKit. Run these commands with Go 1.27.1
available, as declared by both modules.

```sh
set -eu
export GIT_TERMINAL_PROMPT=0
export GOTOOLCHAIN=local
git submodule update --init --checkout -- third_party/agentkit
expected=$(git rev-parse "HEAD:third_party/agentkit")
actual=$(git -C third_party/agentkit rev-parse HEAD)
test "$expected" = "$actual"
go version
(cd third_party/agentkit && go test ./...)
(cd services/worker && go test ./... && go build ./...)
```

Record the playground commit, gitlink, toolchain, and command results. The worker
commands verify the existing scaffold. A worker test that imports and executes
the real graph and client remains required for issue #28; the checkpoint,
provider and job workflow integration is tracked in issue #6.

Public CI keeps its regular checkout. Never pass private checkout credentials
to code from untrusted pull requests. Keep credentials out of URLs and keep
private checkout contents out of public build artifacts and container contexts.

To update the pin, select a reviewed commit reachable from agentKit's default
branch, check it out in the submodule, run the authorized checks, and commit
the changed gitlink on a playground PR branch.
