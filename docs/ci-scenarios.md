# pdf2epub CI / workflow 整理与「按 PR 场景分层」改造方案

> 分析日期：2026-11-25 · 仓库：`WanderSu/pdf2epub`（public，默认分支 `main`）
> 数据来源：本地 `.github/`、git 历史、`gh api` 实测（分支保护 / workflow runs / job 步骤耗时）、PR 列表。
>
> **这是一份历史分析（方案 A 落地前的现状盘点 + 决策依据），不是当前规范**：现行行为以
> `.github/workflows/ci.yml` 与 `scripts/ci_scope.py` 为准。2026-11-28 从 `work/` 移入
> `docs/`，好让代码注释里的引用不会随 `work/` 清理而断掉。

---

## 一、现状盘点

### 1.1 文件清单

| 文件 | 作用 | 备注 |
|---|---|---|
| `.github/workflows/ci.yml` | 唯一 workflow | 165 行，2 个 job |
| `.github/PULL_REQUEST_TEMPLATE.md` | PR 模板 | 只有「做了什么 / 风险 / 测试 / 回滚 / 自查」，**没有改动类型选择** |
| `.github/ISSUE_TEMPLATE/bug_report.yml` | Bug 模板 | 与 CI 无关 |
| `pyproject.toml` | pytest / ruff ratchet 配置 | `[tool.ruff] include` 钉住新增+核心路径 |
| `scripts/bump_version.py --check` | 7 处版本号一致性 | 纯标准库，**不需要装依赖** |
| `scripts/verify_epub.py` / `build_release.py` / `check_release.py` | 发布形态校验 | 手工发布流程，CI 里只用到 verify |
| `CONTRIBUTING*.md` | 说明合并前必须 CI 绿、发布不自动化 | 与现状一致 |

缺失：`.github/dependabot.yml`、`CODEOWNERS`、labeler、`permissions:` 声明、任何缓存配置、merge queue。

### 1.2 workflow 现状

```yaml
on: push(main) / pull_request(main) / workflow_dispatch
concurrency: ci-${{ github.ref }}，cancel-in-progress: true
env: PYTHONUTF8=1 / PYTHONIOENCODING=utf-8

jobs:
  引擎与前端(Ubuntu)   # ubuntu-latest
    checkout → setup-python 3.12 → pip install uv → uv sync --locked
    → apt 字体 + 下载 pandoc 3.14 → uvx ruff check → uv run pytest -q
    → setup-node 20 (npm cache) → npm ci → npm run check
    → apt Tauri 系统依赖 → rust-toolchain stable → cargo test --locked

  桌面端与发布形态(Windows)  # windows-latest
    checkout → setup-python → pip install uv → uv sync --locked
    → 下载 pandoc 3.14 → bump_version.py --check
    → setup-node → npm ci → npm run check
    → rust-toolchain → cargo test --locked
    → npx tauri build --no-bundle → pyinstaller cli.spec → smoke test
    → build_release --skip-build --cli-dir dist_ci/cli → build_installer(NSIS)
    → upload-artifact(pdf2epub-installer: zip + setup.exe)
```

### 1.3 分支保护（`gh api .../branches/main/protection` 实测）

```json
"required_status_checks": {
  "strict": false,
  "contexts": ["引擎与前端(Ubuntu)", "桌面端与发布形态(Windows)"],
  "checks":   [{"context":"引擎与前端(Ubuntu)"}, {"context":"桌面端与发布形态(Windows)"}]
},
"enforce_admins": true,
"required_conversation_resolution": true,
"allow_force_pushes": false, "allow_deletions": false,
"required_linear_history": false
```

- 没有 `required_pull_request_reviews`（不强制 review），没有 ruleset（`rulesets: []`）。
- **`enforce_admins: true`**：管理员也被 required checks 约束，配置改错会把自己也锁死。
- 仓库允许 squash / merge / rebase 三种合并方式；`delete_branch_on_merge: false`。

### 1.4 耗时实测（`gh run list` + jobs API，单位秒）

| run | 场景 | 总时长 | Ubuntu job | Windows job |
|---|---|---|---|---|
| #8 `docs/security-policy-en` | **纯文档 PR** | 6m48s | 358s | 405s |
| `docs/refresh-screenshots` | **纯文档 PR** | 7m53s | — | — |
| `release/0.5.7` | 版本+代码 PR | 16m16s | — | — |
| `0.6.0` 大版本 PR | 引擎+前端+Rust | 12m9s | — | — |
| push main（文档合并后） | push | 11m39s | 235s | **695s** |

纯文档 PR #8 的步骤耗时明细（两个 job 并行，取慢者）：

| 步骤 | Ubuntu | Windows |
|---|---|---|
| Rust 测试 `cargo test --locked` | **170s** | 118s |
| 桌面壳构建 `tauri build --no-bundle` | — | **193s** |
| 安装 pandoc 3.14 + 中文字体 | 46s | 2s |
| `npm ci` | 40s | 28s |
| `pytest` | 41s | — |
| apt Tauri 系统依赖 | 22s | — |
| PyInstaller | — | 19s |
| `uv sync --locked` | 1s | 6s |
| `npm run check` | 7s | 6s |
| bump_version / ruff / smoke | 1s | 1s / 4s |

结论：**Rust 冷编译（311s）+ Tauri 构建（193s）占了大头，且完全没有缓存**；纯文档改动也要为它们排 7–11 分钟队。

### 1.5 PR 场景分布（全部 8 个 PR）

| # | 标题 | 类型 | 实际需要验证什么 |
|---|---|---|---|
| 1 | 安全与工程加固 / CI / 拆分 | 全量 | 全部 + CI 自身 |
| 2 | MinerU HTTP / batch 重试 / 凭证复核 | 引擎+安全 | python + 安全审计 |
| 3 | 重生成桌面端截图 + 文档 | **纯文档** | 几乎无 |
| 4 | 英文主文档 + 0.5.7 | 文档+版本 | 版本一致性 + 发布形态 |
| 5 | 0.6.0 页单元与标题裁决 + AI 校对 | 引擎+前端 | python + frontend |
| 6 | 删除 OCR-footnote-preservation.md | **纯文档**（关闭未合并） | 几乎无 |
| 7 | 中文安全策略 / 删 docs/stages | **纯文档** | 几乎无 |
| 8 | 英文安全策略对齐 | **纯文档** | 几乎无 |

**8 个 PR 里 4 个是纯文档**，全部跑了 7–11 分钟全量。另外每次 PR 合并还会再触发一次 `push main` 的 run（同样的 7–11 分钟），即**一次合并付两次全量成本**。外部 fork PR 目前 0 个（仓库 forks=0），但仓库是 public，迟早要面对。

### 1.6 一段关键历史：`paths-ignore` 尝试后立即 revert

```
271ee2f  CI:纯文档改动不再触发流水线   (+10 行，push/PR 都加 paths-ignore)
70670d5  Revert "CI:纯文档改动不再触发流水线"   (-10 行，4 分钟后)
```

271ee2f 的提交说明里已经写了配套动作：「合并前不再强制"必须有过检查"，直推 main 也不需要先跑流水线」。但当前分支保护**仍然**是原来的两个 required contexts + `enforce_admins: true`。

**这就是死锁根源**：GitHub 的 required status checks 按 context 名匹配。workflow 因 `paths-ignore` 完全不触发 → 这两个 context 永远不上报 → 状态恒为 pending → PR 永远无法合并（管理员也一样）。想靠「不触发」省时间，就必须同步改分支保护；而改保护又存在过渡期 PR 卡住的风险，所以当时的做法四分钟就被 revert 了。

> 这一条决定了后面所有方案的设计前提：**required context 必须每次都上报 success**，不能靠「workflow 不运行」或「job 被 skip」来省时间（skipped job 是否满足 required check 在不同配置下行为不一致，不能赌）。

---

## 二、问题清单（按优先级）

### P0 — 直接阻塞「按场景分层」

1. **required checks 与「跳过触发」死锁**：见 1.6。任何 `paths-ignore` / `paths` 方案都必须先解决 context 上报。
2. **所有 PR 一视同仁**：docs / 版本号 / 前端 / 引擎 / Rust / 依赖升级 / CI 自身 / 安全敏感 / fork PR 全部走同一条 11 分钟双平台流水线，成本与风险完全不匹配。
3. **分支保护 context 名是中文 job 名**：一旦拆分或重命名 job，忘记同步改保护就会全仓库 PR 卡死。

### P1 — 明显浪费与假绿风险

4. **零 Rust 缓存**：`Cargo.lock` 未变也每次冷编译，`cargo test` 170s + `tauri build` 193s 是大头；`Swatinem/rust-cache@v2` 可以直接砍掉大半。
5. **跨平台重复劳动**：`npm ci` + `npm run check` + `cargo test --locked` 在 Ubuntu 和 Windows 各跑一遍；前端检查是平台无关的，Rust 单测也不需要两个平台各跑一次。
6. **秒级检查绑在最慢的 job 上**：`bump_version.py --check`（纯标准库）和 `ruff` 现在要等 Windows 7 分钟才出结果；纯文档 PR 改了 README 下载链接的版本号，也要等 11 分钟才知道挂了。
7. **pandoc 无缓存、无完整性校验**：每次 apt 装字体 + 从 GitHub release 下载 deb 46s，且没有 SHA256 校验，网络抖动/上游篡改会造成假红或供应链风险。
8. **`uvx ruff` / `pip install uv` / `rust-toolchain@stable` 全部浮动最新版**：ratchet 策略只钉了 include 路径，没钉工具版本；某天上游发新版规则或 toolchain 行为变化，会让与改动无关的 PR 突然变红。
9. **pytest 的 skip 会掩盖问题**：`tests/conftest.py` 明确「缺 pandoc 则 skip，不 fail」。CI 里 pandoc 安装一旦静默失效，大量 EPUB 用例会被跳过但仍显示绿。
10. **无 `permissions:` 声明**：默认 `GITHUB_TOKEN` 权限取决于仓库设置（可能是 read/write）；`pull_request` 下公开仓库应显式收窄到 `contents: read` + `pull-requests: read`。

### P2 — 治理与可运维性

11. **`workflow_dispatch` 无输入**：无法手动指定「只跑发布形态」或「强制全量复核」。
12. **PR 模板没有改动类型**：自动分类只能靠路径，遇到「改了 .github 模板」「标题写了 release 但只动 README」这类场景容易误判，也没有人工覆盖通道（`ci:full` 标签）。
13. **`pull_request` 未声明 types**：默认不含 `labeled` / `ready_for_review`，打标签不会重跑、draft 转正式不重跑。
14. **无 `merge_group` 触发**：将来启用 merge queue 会缺检查。
15. **无依赖自动更新配置**：三套锁文件（`uv.lock` / `desktop/package-lock.json` / `desktop/src-tauri/Cargo.lock`）全靠手工，且锁文件变更应当强制走全量。
16. **`concurrency.cancel-in-progress: true` 对 `push main` 也生效**：连续合并会取消上一个 main run，导致某些 main 提交没有完整 CI 记录，与「合并前 CI 绿」的约定有缝隙。
17. **PR 模板里的自查项没有自动化**：「没提交真实 Token / 样本 / work/ / output/ / logs/」「不降低 EPUB 校验严格度」目前纯靠人眼，CI 可以秒级兜底。

---

## 三、改造目标与设计前提

1. **required context 恒定上报**：两个受保护 job 永远运行并 success，重步骤按范围跳过（而不是跳过 job 或跳过整个 workflow）。
2. **零改分支保护即可上线**（方案 A）：沿用现有两个中文 job 名，不动 GitHub 设置，规避过渡期死锁。
3. **分类在服务端的只读信息上做**：`git diff --name-only` + PR 标题 + 标签 + 是否 fork，**不执行 PR 里的代码**；不使用 `pull_request_target`（它会带着 secrets 跑 fork 代码）。
4. **fail-safe**：分类脚本本身出错或判定不明 → 默认 `full`，绝不因优化而漏检。
5. **安全敏感路径只加不减**：`net.py` / `safe_zip.py` / `mineru_http.py` / `epub/verify.py` / `verify_epub.py` / 凭证相关 / 锁文件 / workflow 自身 → 一律强制全量。
6. **fork PR 不降检查**：外部贡献者一律 `full`（且已有 `permissions` 收窄，天然不需要 secrets）。

---

## 四、场景 → 检查矩阵（目标态）

| # | PR 场景 | 判定依据 | 跑什么 | 目标耗时 |
|---|---|---|---|---|
| S1 | 纯文档 / 截图 / LICENSE / 模板 | 改动全部落在 `*.md`、`docs/**`、`LICENSE*`、`SECURITY*`、`CONTRIBUTING*`、`.github/ISSUE_TEMPLATE/**`、PR 模板 | 常驻秒级：版本一致性 + 产物/凭证门禁 + 文档链接校验 | **< 1 min** |
| S2 | Python 引擎 / 测试 / config / scripts | `src/pdf2epub/**`、`tests/**`、`config/**`、`scripts/**`、`pyproject.toml` | + ruff / pytest / pandoc 3.14 | 1.5–2 min |
| S3 | 桌面前端 | `desktop/src/**`、`package*.json`、`vite/vitest/tsconfig/eslint` | + tsc / eslint / vitest（只在 Ubuntu） | ~1 min |
| S4 | Rust / Tauri 壳 | `desktop/src-tauri/**` | + cargo test + Tauri 系统依赖 + `tauri build --no-bundle` | 3–4 min |
| S5 | 版本号 / 发布形态 | `cli.spec`、`tauri.conf.json`、`Cargo.toml`、`build_release.py`、`types.ts`、README 下载链接、版本号类提交 | + Windows 全量：Tauri build / PyInstaller / smoke test | 3–5 min |
| S6 | 依赖升级 | `uv.lock` / `package-lock.json` / `Cargo.lock` | 强制 full | 4–6 min |
| S7 | CI 自身 / 分类脚本 | `.github/workflows/**`、`scripts/ci_scope.py` | 强制 full（配置改坏不能靠它自己跳过） | 4–6 min |
| S8 | 安全敏感 | `net.py`、`safe_zip.py`、`mineru_http.py`、`epub/verify.py`、`verify_epub.py`、`*credential*` | 强制 full + PR 模板打勾提醒人工复核 | 4–6 min |
| S9 | 外部 fork PR | `head.repo.fork == true` | 强制 full，无 secrets | 4–6 min |
| S10 | 人工覆盖 | 标签 `ci:full`（强制全量）/ `ci:docs`（强制轻量）/ `workflow_dispatch.scope` | 覆盖自动分类 | 按选择 |
| S11 | push 到 main（含合并后那次） | 事件类型 | 至少基础集；建议 full（直推 main 的唯一防线），靠缓存压缩到 2–3 min | 2–3 min |
| — | draft / ready_for_review | `types` 补齐 | 重新分类，不漏检 | — |

> 覆盖优先级：`workflow_dispatch.scope` > 标签 > fork/锁文件/安全路径/workflow 自身 > 路径分类 > 默认 full。
> 任何一处判定为 full，`python/frontend/rust/release` 全置真。

---

## 五、方案 A（推荐，零改分支保护，可直接落地）

### 5.1 思路

- **两个受保护 job 保留原名、永远运行**（checkout + 分类 + 常驻秒级检查），因此两个 required context 每次都上报 success。
- 重步骤用 `if: steps.scope.outputs.X == 'true'` 逐个跳过；分类逻辑放在 `scripts/ci_scope.py`，两个 job 各跑一次（3 秒、无 needs 依赖、无死锁）。
- 顺手做去重、缓存、版本固定、权限收窄、dispatch 输入、types 补齐。
- **分支保护一个字都不用改**，上线风险最低，随时 revert 单个提交即可回滚。

### 5.2 新增 `scripts/ci_scope.py`（分类 + 门禁，纯标准库，可本地单测）

要点（完整实现落地时生成）：

```python
# 输入(env): EVENT_NAME / BASE_SHA / BEFORE_SHA / HEAD_SHA / PR_TITLE /
#            PR_LABELS(json) / FORK / FORCE_SCOPE
# 输出(GITHUB_OUTPUT): python / frontend / rust / release / full / docs_only
# 退出码非 0: 门禁失败(禁止文件 / 疑似凭证 / 样本文件被新增)

DOC      = ["*.md", "docs/**", "LICENSE*", "SECURITY*", "CONTRIBUTING*",
            ".github/ISSUE_TEMPLATE/**", ".github/PULL_REQUEST_TEMPLATE.md"]
PYTHON   = ["src/pdf2epub/**", "tests/**", "config/**", "scripts/**",
            "pyproject.toml", "cli.spec", ".python-version"]
FRONTEND = ["desktop/src/**", "desktop/package*.json", "desktop/vite.config.ts",
            "desktop/vitest.config.ts", "desktop/tsconfig*.json",
            "desktop/eslint.config.js", "desktop/index.html"]
RUST     = ["desktop/src-tauri/**"]
RELEASE  = ["cli.spec", "scripts/build_release.py", "scripts/check_release.py",
            "scripts/verify_epub.py", "desktop/src-tauri/tauri.conf.json",
            "desktop/src-tauri/Cargo.toml", "desktop/src/types.ts",
            "README.md", "README.zh-CN.md", "pyproject.toml"]
FULL     = ["uv.lock", "desktop/package-lock.json", "desktop/src-tauri/Cargo.lock",
            ".github/workflows/**", "scripts/ci_scope.py",
            "src/pdf2epub/net.py", "src/pdf2epub/backends/safe_zip.py",
            "src/pdf2epub/backends/mineru_http.py", "src/pdf2epub/epub/verify.py",
            "scripts/verify_epub.py", "*credential*", "apikey*"]

GUARD_BAN = ["apikey.json", "*.pdf", "*.epub", "work/**", "output/**", "logs/**",
             "dist_cli/**", "dist_release/**", ".venv/**"]  # 新增即失败
GUARD_TOKEN_RE = r"(sk-[A-Za-z0-9]{20,}|MINERU_API_TOKEN\s*[:=]\s*\S+)"

# 事件差异：
#   pull_request  → git diff --name-only <base_sha>...<head_sha>   (三点)
#   push          → git diff --name-only <before>..<head>            (两点；before 全 0 时视为全量)
#   workflow_dispatch → 无 diff，完全由 FORCE_SCOPE 决定
#   fork / 锁文件 / 安全路径 / workflow 自身 / push main / 分类异常 → full=true
#   ci:full 标签 → full；ci:docs 标签 → docs_only
#   docs_only = 所有文件都命中 DOC 且 full=false
```

门禁用 `git diff --diff-filter=A --name-only` 只看新增文件，避免历史文件误伤；命中即 `sys.exit(1)`，两个 job 同时变红。

### 5.3 新 `ci.yml` 草案

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
    types: [opened, synchronize, reopened, ready_for_review, labeled, unlabeled]
  workflow_dispatch:
    inputs:
      scope:
        description: 强制指定检查范围(auto=按改动自动判定)
        type: choice
        default: auto
        options: [auto, docs, python, frontend, rust, release, full]

# 最小权限:fork PR 下 GITHUB_TOKEN 天然只读,这里显式钉死
permissions:
  contents: read
  pull-requests: read

concurrency:
  group: ci-${{ github.event.pull_request.number || github.ref }}
  # PR 的连续 push 取消旧 run;push main 不取消,保证每个 main 提交都有完整记录
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

env:
  PYTHONUTF8: "1"
  PYTHONIOENCODING: utf-8
  PANDOC_VERSION: "3.14"

jobs:
  engine:
    name: 引擎与前端(Ubuntu)          # ← 分支保护的 context 名,禁止改名
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0              # 分类需要 base/head 的完整历史

      - name: 变更范围判定 + PR 门禁
        id: scope
        shell: bash
        env:
          EVENT_NAME: ${{ github.event_name }}
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
          BEFORE_SHA: ${{ github.event.before }}
          HEAD_SHA: ${{ github.event.pull_request.head.sha || github.sha }}
          PR_TITLE: ${{ github.event.pull_request.title || '' }}
          PR_LABELS: ${{ toJSON(github.event.pull_request.labels.*.name) }}
          FORK: ${{ github.event.pull_request.head.repo.fork }}
          FORCE_SCOPE: ${{ inputs.scope || 'auto' }}
        run: python scripts/ci_scope.py

      # ---- 常驻秒级检查:纯文档 PR 也跑,且几秒出结果 ----
      - name: 版本号一致性
        run: python scripts/bump_version.py --check   # 纯标准库,无需 uv sync
      - name: 文档/链接轻量校验(可选)
        if: steps.scope.outputs.docs_only == 'true'
        run: python scripts/ci_scope.py --check-docs

      # ---- Python 引擎层 ----
      - uses: astral-sh/setup-uv@v5
        if: steps.scope.outputs.python == 'true' || steps.scope.outputs.full == 'true'
        with:
          enable-cache: true
          cache-dependency-glob: uv.lock
      - name: 安装 Python 依赖(锁文件)
        if: steps.scope.outputs.python == 'true' || steps.scope.outputs.full == 'true'
        run: uv sync --locked
      - name: 安装 pandoc ${{ env.PANDOC_VERSION }} 与中文字体
        if: steps.scope.outputs.python == 'true' || steps.scope.outputs.full == 'true'
        run: |
          # deb 按版本+架构缓存;下载后校验 SHA256 再 dpkg -i
          bash scripts/ci_install_pandoc.sh
      - name: Ruff(版本由 uv.lock 固定,不再用浮动的 uvx)
        if: steps.scope.outputs.python == 'true' || steps.scope.outputs.full == 'true'
        run: uv run ruff check
      - name: pytest(禁止大规模 skip 造成的假绿)
        if: steps.scope.outputs.python == 'true' || steps.scope.outputs.full == 'true'
        run: uv run pytest -q -ra --strict-markers

      # ---- 前端(平台无关,只在这里跑一次) ----
      - uses: actions/setup-node@v4
        if: steps.scope.outputs.frontend == 'true' || steps.scope.outputs.full == 'true'
        with:
          node-version: "20"
          cache: npm
          cache-dependency-path: desktop/package-lock.json
      - name: 前端依赖 + 检查
        if: steps.scope.outputs.frontend == 'true' || steps.scope.outputs.full == 'true'
        working-directory: desktop
        run: npm ci && npm run check

      # ---- Rust ----
      - name: 安装 Tauri 构建依赖
        if: steps.scope.outputs.rust == 'true' || steps.scope.outputs.full == 'true'
        run: |
          sudo apt-get update
          sudo apt-get install -y --no-install-recommends \
            libwebkit2gtk-4.1-dev libgtk-3-dev libayatana-appindicator3-dev \
            librsvg2-dev patchelf libssl-dev
      - uses: dtolnay/rust-toolchain@stable
        if: steps.scope.outputs.rust == 'true' || steps.scope.outputs.full == 'true'
      - uses: Swatinem/rust-cache@v2
        if: steps.scope.outputs.rust == 'true' || steps.scope.outputs.full == 'true'
        with:
          workspaces: desktop/src-tauri
      - name: Rust 测试
        if: steps.scope.outputs.rust == 'true' || steps.scope.outputs.full == 'true'
        working-directory: desktop/src-tauri
        run: cargo test --locked

  desktop:
    name: 桌面端与发布形态(Windows)   # ← 分支保护的 context 名,禁止改名
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: 变更范围判定 + PR 门禁
        id: scope
        shell: bash                 # Windows runner 上 bash 可用
        env:
          EVENT_NAME: ${{ github.event_name }}
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
          BEFORE_SHA: ${{ github.event.before }}
          HEAD_SHA: ${{ github.event.pull_request.head.sha || github.sha }}
          PR_TITLE: ${{ github.event.pull_request.title || '' }}
          PR_LABELS: ${{ toJSON(github.event.pull_request.labels.*.name) }}
          FORK: ${{ github.event.pull_request.head.repo.fork }}
          FORCE_SCOPE: ${{ inputs.scope || 'auto' }}
        run: python scripts/ci_scope.py

      - name: 跳过说明(轻量场景仍上报 success)
        if: steps.scope.outputs.release != 'true' && steps.scope.outputs.full != 'true'
        run: echo "本场景不涉及发布形态,跳过 Windows 打包链路"

      # ---- 发布形态:仅 S5/S6/S7/S8/S9/full ----
      - uses: actions/setup-python@v5
        if: steps.scope.outputs.release == 'true' || steps.scope.outputs.full == 'true'
        with:
          python-version: "3.12"
      - uses: astral-sh/setup-uv@v5
        if: steps.scope.outputs.release == 'true' || steps.scope.outputs.full == 'true'
        with:
          enable-cache: true
          cache-dependency-glob: uv.lock
      - name: 安装 Python 依赖(锁文件)
        if: steps.scope.outputs.release == 'true' || steps.scope.outputs.full == 'true'
        run: uv sync --locked
      - name: 安装 pandoc ${{ env.PANDOC_VERSION }}
        if: steps.scope.outputs.release == 'true' || steps.scope.outputs.full == 'true'
        shell: pwsh
        run: ./scripts/ci_install_pandoc.ps1
      - uses: actions/setup-node@v4
        if: steps.scope.outputs.release == 'true' || steps.scope.outputs.full == 'true'
        with:
          node-version: "20"
          cache: npm
          cache-dependency-path: desktop/package-lock.json
      - name: 前端依赖(仅 Tauri CLI 需要,不重复跑 npm run check)
        if: steps.scope.outputs.release == 'true' || steps.scope.outputs.full == 'true'
        working-directory: desktop
        run: npm ci
      - uses: dtolnay/rust-toolchain@stable
        if: steps.scope.outputs.release == 'true' || steps.scope.outputs.full == 'true'
      - uses: Swatinem/rust-cache@v2
        if: steps.scope.outputs.release == 'true' || steps.scope.outputs.full == 'true'
        with:
          workspaces: desktop/src-tauri
      - name: 桌面壳构建
        if: steps.scope.outputs.release == 'true' || steps.scope.outputs.full == 'true'
        working-directory: desktop
        run: npx tauri build --no-bundle
      - name: 打包引擎(PyInstaller onedir)
        if: steps.scope.outputs.release == 'true' || steps.scope.outputs.full == 'true'
        run: uv run --with pyinstaller pyinstaller cli.spec --noconfirm --distpath dist_ci --workpath build_ci
      - name: 最小发布形态 smoke test
        if: steps.scope.outputs.release == 'true' || steps.scope.outputs.full == 'true'
        shell: pwsh
        run: ./scripts/ci_smoke.ps1        # 原内联脚本抽出,便于本地复现
```

配套的小改动：

- `pyproject.toml` 的 `[dependency-groups].dev` 增加 `ruff==<锁定版本>`，`uv.lock` 跟着更新；CI 用 `uv run ruff check` 取代浮动的 `uvx ruff check`。
- `scripts/ci_install_pandoc.sh` / `.ps1`：deb/zip 按 `PANDOC_VERSION` 缓存到 `~/.cache`，下载后校验官方 SHA256，再加字体；Linux 侧字体安装也只在缓存未命中时执行。
- `scripts/ci_smoke.ps1`：把 Windows job 里那段内联的造 PDF → 复制 → CLI → verify 抽出来。
- `ci_scope.py --check-docs`：docs-only 时做相对链接 / 图片存在性检查（README 里有本地 `docs/*.png` 引用），不联网、不跑 pandoc。
- `pytest -q -ra --strict-markers` + 在 job 末尾加一句「若 skipped 数 > 阈值则失败」的守卫，堵住 conftest 的 skip 假绿。

### 5.4 预期收益（基于 1.4 的实测）

| 场景 | 现在 | 方案 A（含缓存） |
|---|---|---|
| 纯文档 PR（占历史 PR 的 50%） | 6m48s–11m39s | **~1 min（两个 runner 启动 + 秒级检查）** |
| 引擎改动 PR | ~11 min | ~1.5–2 min |
| 前端改动 PR | ~11 min | ~1 min |
| 全量 / 发布形态 / fork PR | ~11–16 min | ~3–5 min（rust-cache 命中后） |
| 版本号不一致的反馈延迟 | ~7–11 min | **~10 s** |

---

## 六、方案 B（进阶：单一 gate，彻底取消空跑 runner）

方案 A 里纯文档 PR 仍要启动两个 runner（各 20–40s）。若要连这部分也省掉，需要把分支保护改成只要求一个汇聚 job：

```yaml
jobs:
  scope:            # 秒级分类
  python:          # needs: scope, if: ...
  frontend:        # needs: scope, if: ...
  rust:            # needs: scope, if: ...
  release:         # needs: scope, if: ...
  ci-gate:         # 唯一 required check
    name: ci-gate
    needs: [scope, python, frontend, rust, release]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - name: 汇总判定
        env:
          NEEDS: ${{ toJSON(needs) }}
          EXPECT: ${{ needs.scope.outputs.required }}   # 期望必须 success 的 job 集合
        run: python scripts/ci_gate.py
```

`ci_gate.py` 校验：所有 `needs.*.result` 只能是 `success` / `skipped`，且 `EXPECT` 里的 job 必须为 `success`（防止分类写错导致该跑的没跑），否则失败。

**迁移顺序（关键，别踩 271ee2f 的坑）**：

1. 先合并新 workflow，但**保留两个旧 job 名作为轻量别名**（`引擎与前端(Ubuntu)` / `桌面端与发布形态(Windows)` 各做一个只跑 `ci_gate` 判定的 no-op job，或让它们成为真实分层的入口）。此时分支保护仍指向旧名，一切正常。
2. 挑一个没有在飞 PR 的时间窗，把 required checks 改成 `ci-gate`：

   ```bash
   echo '{"strict":false,"contexts":["ci-gate"],"checks":[{"context":"ci-gate"}]}' \
     | gh api -X PATCH repos/WanderSu/pdf2epub/branches/main/protection/required_status_checks --input -
   ```

   （`gh api` 需要 admin 权限；改完在 仓库 Settings → Branches 里目视核对。`enforce_admins: true` 下配错会把自己也锁住，务必先确认 `ci-gate` 已经在最近一次 run 里上报过 success。）
3. 稳定跑过 2–3 个 PR 后，再删掉两个别名 job。
4. 回滚：把 contexts 改回原两个中文名 + revert workflow 提交。

**取舍**：方案 B 更省 runner、分层更清晰，但要动 GitHub 设置、有迁移窗口期、且 `ci_gate.py` 自身成为新的单点。建议先上方案 A（一个提交、零外部改动），观察 1–2 周，再决定是否进 B。

---

## 七、配套改动清单

| 项 | 内容 |
|---|---|
| `.github/PULL_REQUEST_TEMPLATE.md` | 顶部加「改动类型」多选框：纯文档 / 引擎 / 前端 / Rust / 发布与版本 / 依赖升级 / CI 配置 / 安全敏感；并写明「标注类型只影响检查范围，不影响是否需要 review」「打 `ci:full` 标签可强制全量」；自查项补一条「涉及 `net.py`/`safe_zip.py`/`epub/verify.py` 时已人工复核」 |
| `.github/labels` | 新建 `ci:full`、`ci:docs` 两个标签（可用 `gh label create`） |
| `.github/dependabot.yml` | 三个 ecosystem：`pip`(根 `/`，含 `uv.lock`)、`npm`(`/desktop`)、`cargo`(`/desktop/src-tauri`)，每周；依赖 PR 由 FULL 规则自动走全量 |
| `.github/workflows/ci.yml` | `permissions`、`types`、`workflow_dispatch.inputs`、`concurrency` 收敛，见 5.3 |
| `CONTRIBUTING.md` / `.zh-CN.md` | 补一节「CI 按改动范围分层」，说明标签覆盖、dispatch 用法、纯文档 PR 也会跑秒级检查、发布仍是手工 |
| `README*.md` | CI badge 不变（workflow 文件名不变） |
| 分支保护 | 方案 A 不动；方案 B 见第六节。**另建议** `required_linear_history: true` + `delete_branch_on_merge: true`（可选，与 CI 无关但能减少 1.5 里的重复 run 观感） |
| action 供应链 | 可选：把 `actions/checkout@v4` 等固定到 commit SHA + 启用 Dependabot 更新 action；项目 SECURITY.md 已把依赖漏洞列为范围内，这条与之呼应 |

---

## 八、落地顺序与验证清单

**顺序（每个都是可独立 revert 的提交）**

1. `scripts/ci_scope.py` + 本地单测（纯标准库，先在 Windows/Linux 各跑一次）。
2. `pyproject.toml` 锁 ruff 版本 → `uv lock` → 确认 `uv run ruff check` 与现在 `uvx ruff check` 结果一致。
3. pandoc 安装脚本 + SHA256 校验 + 缓存；抽出 `ci_smoke.ps1`。
4. 替换 `ci.yml`（方案 A），**不改分支保护**。
5. PR 模板 / labels / dependabot / CONTRIBUTING 文档。
6. 观察 1–2 周 → 再评估方案 B / merge queue。

**验证清单**

- [ ] 建一个只改 `README.md` 的 PR：两个 required check 都绿，总耗时 < 1.5 min，分支保护允许合并。
- [ ] 同一个 PR 打 `ci:full` 标签：workflow 重跑且走全量（验证 `labeled` types 生效）。
- [ ] 改 `src/pdf2epub/net.py`：自动 full（安全路径规则生效）。
- [ ] 只改 `desktop/src/App.tsx`：跑引擎基础检查 + 前端，不跑 Windows 打包。
- [ ] 只改 `cli.spec`：跑发布形态全链路。
- [ ] 故意把 `pyproject.toml` 版本改成不一致：约 10 秒内红（不再等 Windows 7 分钟）。
- [ ] `workflow_dispatch` 选 `release`：只跑发布形态。
- [ ] 新增一个 `apikey.json` 或 `foo.pdf` 的 PR：门禁秒级变红。
- [ ] 从一个 fork 提 PR：自动 full，且无任何 secrets 相关报错（验证 `permissions` 收窄）。
- [ ] 连续两次合并到 main：两个 `push main` run 都完整保留（验证 `cancel-in-progress` 调整）。
- [ ] 对比一次 full run 的耗时：rust-cache 命中后应明显低于 1.4 的 695s。

---

## 九、一句话结论

**不要再用 `paths-ignore` 让 workflow 不触发**（这正是 271ee2f 四分钟后被 revert 的原因：两个 required contexts 永不上报，PR 会被永久卡住）。改成「**两个受保护 job 常驻上报、重步骤按 `scripts/ci_scope.py` 的分类结果逐个跳过**」，再加 rust/uv 缓存、跨平台去重、秒级门禁前置、标签与 dispatch 覆盖通道 —— 纯文档 PR 从 7–11 分钟降到 1 分钟内，全量 PR 降到 3–5 分钟，且分支保护一个字都不用改。

---

## 十、落地状态（2026-11-25，方案 A 已实施）

**分支保护零改动**（复查 contexts 仍是那两个中文 job 名、`enforce_admins: true`）。

| 文件 | 状态 |
|---|---|
| `.github/workflows/ci.yml` | 重写：两个 job 名不变、常驻上报；重步骤按 scope outputs 跳过；加 `permissions` / `types` / dispatch `scope` / rust-cache / 版本断言 |
| `scripts/ci_scope.py` | 新建：路径+标题+标签+fork 分类、PR 门禁、docs 链接检查、12 条自测 |
| `scripts/ci_install_pandoc.py` | 新建：跨平台固定 pandoc 3.14；SHA256 取自 GitHub Release API 的 asset `digest`（非本地自证） |
| `.github/PULL_REQUEST_TEMPLATE.md` | 加改动类型勾选、标签/覆盖说明、安全路径自查项 |
| `.github/dependabot.yml` | 新建：`uv` / `npm` / `cargo` / `github-actions` 四个 ecosystem |
| `CONTRIBUTING.md` / `.zh-CN.md` | 补「CI scopes / CI 检查范围」一节；`uvx ruff` → `uv run ruff` |
| `pyproject.toml` / `uv.lock` | ruff 钉死 `0.17.6` 并纳入 `[tool.ruff] include`；`uv lock` 只新增 ruff |

与草案的偏差（有意为之）：

- **smoke test 保留内联在 ci.yml**，没有抽成 `.ps1`：避免 PowerShell 5.1 读无 BOM UTF-8 时中文乱码。
- **pandoc 用单个跨平台 Python 脚本**取代 sh + ps1 两份；已实跑，SHA256 校验通过并打印 `pandoc 3.14`。
- **未加 uv / pandoc 的跨 run 缓存**：实测 `uv sync` 1s、pandoc 下载 2s，收益为零而跨平台缓存路径易错；真正的大头（Rust 冷编译）用 `Swatinem/rust-cache@v2` 解决。
- `FORCE_SCOPE` 用 `github.event.inputs.scope` 而非 `inputs.scope`，避免非 dispatch 事件下引用可能不存在的上下文。
- `rust=true` 会连带 `release=true`：改 Tauri 壳仍验证 Windows 打包，不比原 CI 弱。

本地验证：分类自测 **12/12**；4 个历史提交分类符合预期（`SECURITY.md` → docs_only、引擎提交 → python、版本提交 → full）；标签/fork/push/dispatch 覆盖 5/5；门禁拦截 `apikey.json`；docs 链接检查通过；`uv run ruff check`（含新脚本）通过；`uv run pytest -q` **890 passed / 8 skipped**；`uv sync --locked`、`bump_version.py --check`、YAML 结构、`git diff --check` 全通过。**GitHub Actions 本身只能在远端跑，请用落地后的第一个 PR 实测。**

仍需人工在 GitHub 上做一次（可删可改、不影响合并）：

```bash
gh label create ci:full --description "强制走全量 CI" --color B60205
gh label create ci:docs --description "确认纯文档、走轻量 CI" --color 0E8A16
```
