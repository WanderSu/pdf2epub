# Windows 安装流程改造方案（评估稿）

> 目标（来自待办清单）：发行版 release 应更适用于**常规软件安装流程** —— 下载安装包 → 双击 →
> 下一步/完成 → 开始菜单与卸载项 → 升级保留数据。
> 本文件是**评估稿**：现状证据、可选方案、推荐路线、文件级改动清单、迁移规则与验收。
> 实施拆成两阶段，见 §7。

## 1. 现状（绿色版）

| 路径 | 谁写 | 内容 | `Program Files` 下可写？ |
|---|---|---|---|
| `<安装根>\pdf2epub.exe` | 发布包 | Tauri 壳 | 否（安装时写一次） |
| `<安装根>\cli\`（190 MB） | 发布包 | PyInstaller 引擎 onedir | 否 |
| `<安装根>\config\` | 发布包 | `config.yaml` / `book.css`（只读） | 否，但引擎只需要读 |
| `<安装根>\apikey.json` | **设置页会写** | 云端/ AI 凭证 | **否** → 保存凭证失败 |
| `<安装根>\work\` | **引擎会写** | 中间产物、解析缓存（`_parts/` 是钱） | **否** → 转换失败 |
| `<安装根>\output\` | **引擎会写** | 交付的 EPUB | **否** → 转换失败 |
| `<安装根>\pandoc\`（可选） | **首启下载会写** | 内置 pandoc | **否** → 下载失败 |
| `%LOCALAPPDATA%\pdf2epub\settings.json` | 壳写 | 设置 | 是 ✔ |
| `%LOCALAPPDATA%\pdf2epub\library.json` | 壳写 | 书库 | 是 ✔ |

证据（代码位置）：

- 壳把 CLI 子进程的 `cwd` 固定为安装根（`desktop/src-tauri/src/lib.rs` `convert_file` /
  `preflight` 里的 `install_root` 分支），而 CLI 的 `--work` 默认是相对 `work/`、
  `-o` 由前端默认值 `"output"` 传进来（`desktop/src/App.tsx`），两者都相对 cwd 解析。
- 凭证写入目标是 `apikey_target()`（安装根）；引擎侧 `paths._roots()` 只查
  cwd / exe 同级 / exe 上级（`src/pdf2epub/paths.py`）。
- 首启 pandoc 下载写安装根 `pandoc/`（`lib.rs` `install_pandoc`）；引擎侧
  `epub/pandoc.py::_pandoc_candidates()` 查 exe 同级/上级与 `PDF2EPUB_PANDOC`。
- `settings.json` / `library.json` 已经在 `%LOCALAPPDATA%\pdf2epub\`（v0.6 就搬过去了），
  所以**设置和书库不会因为装到 Program Files 而丢**；丢的是 work/output/凭证/pandoc。

结论：**装到 `Program Files` 之前，必须先把「会写的东西」搬出安装目录**；
否则只能做 per-user 安装（装到 `%LOCALAPPDATA%`，目录可写）。

## 2. 目标流程

1. 下载 `pdf2epub-v<版本>-win-x64-setup.exe`（NSIS），双击。
2. 选择「仅为我安装（默认，不弹 UAC）」或「为所有用户安装（需要管理员）」；
   安装目录可改。
3. 完成页可勾选「立即运行」；开始菜单 +（可选）桌面快捷方式；控制面板有卸载项。
4. 升级：安装新版本即可，`%LOCALAPPDATA%\pdf2epub\` 里的设置/书库/work/output/凭证
   **全部保留**；卸载默认保留用户数据（安装器里明确写「删除用户数据」是另一个勾选项）。
5. 绿色版 zip 继续发布（便携/受限环境），两者数据互通。

## 3. 方案对比

| 方案 | 改动量 | 安装到 Program Files | 升级保留数据 | 风险 |
|---|---|---|---|---|
| A. 只加 NSIS，**per-user**（装到 `%LOCALAPPDATA%\pdf2epub-app`） | 小：只写安装器 | 否（per-user 目录可写） | work/output 在安装目录里，**卸载/覆盖升级有丢的风险** | 低 |
| B. **数据目录重构** + NSIS（per-user 默认，可选 per-machine） | 中：Rust/Python/前端 + 安装器 + 迁移 | 是 | 是（数据在 `%LOCALAPPDATA%`） | 中（迁移路径必须测试） |
| C. MSI（WiX） | 大：签名、ICE 校验、组件 GUID | 是 | 是 | 高（WiX 维护成本） |

推荐 **B**，理由：

- 用户待办里的「常规软件安装流程」隐含「能装到 Program Files、能升级、能卸载」，
  A 满足不了 Program Files（除非装到用户目录，那和绿色版差别不大）；
- 数据搬出安装目录**对绿色版也是收益**：现在覆盖解压升级会丢 `work/`（含已计费的
  `_parts/` 云端结果）与 `output/`；
- MSI 的复杂度（组件 GUID、ICE 校验、签名）对单人项目不划算；NSIS 能覆盖
  per-user/per-machine、静默安装、卸载、开始菜单，脚本可读可审。

## 4. 推荐路线的具体改动

### 4.1 数据根统一为 `%LOCALAPPDATA%\pdf2epub\`

```
%LOCALAPPDATA%\pdf2epub\
  settings.json      （已有）
  library.json       （已有）
  apikey.json        （新；从安装根迁移）
  work\              （新；中间产物 + _parts 云端缓存）
  output\            （新；交付 EPUB）
  logs\              （新；CLI 批处理日志，桌面端目前 --no-log）
  pandoc\pandoc.exe  （新；首启下载的内置 pandoc）
```

### 4.2 Rust 壳（`desktop/src-tauri/src/lib.rs`）

1. `app_data_dir()` 已有，新增 `data_subdir("work" | "output" | "logs" | "pandoc")`
   并 `create_dir_all`（失败要报可读错误，不再静默）。
2. `convert_file` / `preflight`：
   - 子进程 `cwd` 从安装根改为 **`app_data_dir()`** —— 这样 CLI 的 `--work`/`-o`
     相对默认值、以及 `api_key_file()` 的「cwd 优先」都自然落在用户目录
     （`config/` 仍由引擎按 exe 上级找到，已验证 `paths._roots()` 的 exe 分支）。
   - 显式传 `--work <data>\work`，`-o` 由前端默认值改为 `<data>\output`（见 4.3）。
   - 子进程环境加 `PDF2EPUB_PANDOC=<data>\pandoc\pandoc.exe`（引擎侧已支持该变量）。
3. `save_apikey` / `ai_key` / `check_env`：`apikey_target()` 改为数据目录；
   读取时**兼容旧位置**（安装根 `apikey.json` 存在且新位置没有 → 首次运行迁移过去）。
4. `install_pandoc`：下载解压到 `<data>\pandoc`；`bundled_pandoc()` 也查这里。
5. `open_output_dir` / `library_sync` / `delete_book_output`：仍然按传入的
   `outputDir` 解析，不写死安装根（现状已经是），只需默认值来自数据目录。
6. 迁移（首次运行，只做一次，写 `settings.json` 的 `layout_migrated: true`）：
   - 安装根有 `apikey.json` 且数据目录没有 → **移动**（不复制，避免两处凭证）。
   - 安装根有 `pandoc/` 且数据目录没有 → 移动整个目录。
   - 安装根有 `work/`、`output/`：见 §5 的「三档规则」。

### 4.3 前端（`desktop/src/App.tsx` / `SettingsScreen`）

- 初始 `outputDir`：优先 `settings.output_dir`；否则向后端要默认值
  （新增命令 `default_dirs()` 返回 `{output, work, data}`），不要把 `"output"` 写死。
- 设置页显示的数据目录/输出目录改成数据目录下的绝对路径；「打开输出目录」照旧。
- 文案提示：输出目录可改，改到别处时书库仍按绝对路径工作（现有逻辑已支持）。

### 4.4 Python 引擎（`src/pdf2epub/paths.py`、`epub/pandoc.py`）

- 大概率**不需要改**：`cwd` 变成数据目录后，`api_key_file()` 的 cwd 分支即命中；
  `config_dir()` 由 exe 上级命中；pandoc 走 `PDF2EPUB_PANDOC`。
- 仍需补一条：`_roots()` 增加环境变量 `PDF2EPUB_DATA_DIR`（可选），用于「壳没传 cwd 的
  特殊调用」（例如用户从命令行调 `cli.exe`），让凭证/默认目录仍然可定位。
  带单测：`PDF2EPUB_DATA_DIR` 优先于 cwd。
- `PANDOC_MISSING_HINT` 文案改为「放进数据目录的 pandoc/ 或安装目录的 pandoc/」。

### 4.5 安装器（新增 `installer/` 与 `scripts/build_installer.py`）

**不建议**让 Tauri 的 bundler 去管那 190 MB 的 `cli/` 树（`bundle.resources` 在 Windows
的落点需要试验确认，而且会改写现有「Green zip = 安装根布局」的约定）。改为：

- `scripts/build_installer.py`：复用 `build_release.py` 的 staging（`dist_release/v<版本>/`），
  再调 `makensis` 产出 `dist_release/pdf2epub-v<版本>-win-x64-setup.exe`；
- `installer/installer.nsi`：
  - 安装根默认 `$PROGRAMFILES64\pdf2epub`（per-machine）或 `$LOCALAPPDATA\Programs\pdf2epub`
    （per-user）；两种模式由 `--mode user|machine` 映射到 `/DPER_USER=1` 编译宏，
    或直接出两个安装包（推荐后者：不用运行时选，安装包体积翻倍但逻辑简单；
    也可以出「一个包、首屏选模式」——NSIS `MultiUser.nsh`）。
  - 文件：`pdf2epub.exe`、`cli\**`、`config\**`、`README.md`、可选 `pandoc\**`；
  - 注册卸载项（`DisplayName`/`DisplayVersion`/`Publisher`/`DisplayIcon`、
    `EstimatedSize`、`QuietUninstallString`）；
  - 开始菜单快捷方式、可选桌面快捷方式、`Uninstall` 保留用户数据；
  - 可选：`WinVer` 检查、WebView2 检测（Tauri 依赖）。
- `scripts/build_release.py` 增加 `--with-installer`（默认关，CI/本地按需）；
  `scripts/check_release.py` 增加「安装器存在且体积/版本号自洽」的检查（不实际安装）。
- CI：Windows job 在 release scope 时构建安装器并作为 artifact 上传（不发布）。

### 4.6 签名与体积（评估结论）

- 签名：无证书 → SmartScreen 会拦「未知发布者」。可在文档里给出「代码签名证书 + 
  `signtool`/`osslsigncode`」的接入点（`build_installer.py` 预留 `--sign` 钩子），
  不做默认实现。
- 体积：安装包 ≈ 190 MB（cli/_internal 占绝大部分），NSIS 用 LZMA 可压到 ~90 MB
  左右（与现有 zip 98 MB 同量级）。安装器不再重复带 zip。

## 5. 数据迁移三档规则（关键）

首次运行（`layout_migrated` 未置位）时，按旧数据的位置决定：

1. **全新安装**（安装根没有 `work/`、`output/`、`apikey.json`）：直接把默认值设为数据目录。
2. **旧目录可写**（绿色版升级，安装根在用户目录/非受保护位置）：
   - `output` / `work` 若存在，**沿用**（把绝对路径写进 `settings.output_dir`；
     work 由壳传 `--work <旧安装根>/work`），并在设置页提示「检测到旧数据目录，
     可在设置里迁移到 `%LOCALAPPDATA%`」；提供一键迁移（移动目录 + 重写
     `library.json` 里的 EPUB 绝对路径前缀）。
   - `apikey.json`、`pandoc/` 直接移到数据目录（它们没有路径引用）。
3. **旧目录不可写**（装到 Program Files 且旧数据在里面 —— 只可能来自手工拷贝）：
   - 启动时检测写权限（试写 `.<name>.writetest`），失败则强制迁移到数据目录，
     并重写 `library.json` 路径；迁移失败给出明确错误（列出源/目标/失败原因），
     绝不静默改用空目录（否则用户会以为书丢了）。

## 6. 验收清单（实施后逐条打勾）

- [ ] 全新 per-user 安装：不弹 UAC；转换一本本地 PDF 成功；`work/output` 落在
      `%LOCALAPPDATA%\pdf2epub\`。
- [ ] per-machine 安装到 `Program Files`：转换成功（证明不再写安装目录）；
      设置页能保存凭证；首启能下载 pandoc。
- [ ] 升级安装（旧版本 → 新版本）：`settings.json`/`library.json`/`work/_parts` 全在；
      已计费的 OCR 结果仍能复用（`reuse` 事件）。
- [ ] 卸载：程序文件清干净，用户数据保留；勾「删除用户数据」时才删。
- [ ] 绿色版 zip 仍可用，且与安装版共用同一份用户数据。
- [ ] 静默安装 `/S` 可用（企业/脚本场景），卸载静默可用。
- [ ] 失败路径：数据目录不可写、输出目录被占用、安装目录只读 —— 都给出可读错误。

## 7. 分阶段实施

1. **阶段 1（代码，无安装器）**：4.1–4.4 的数据目录重构 + 迁移 + 单测；
   绿色版 zip 先享受到「升级不丢 work/output」。
2. **阶段 2（打包）**：4.5 的 NSIS 安装器 + `build_installer.py` + CI artifact；
   先出 per-user 包，再出 per-machine 包。
3. **阶段 3（可选）**：签名钩子、WebView2 检测、自动更新（评估后再做）。

阶段 1 完成前不动 `tauri.conf.json` 的 bundle 配置，避免「安装器能装、装上就跑不起来」。

## 8. 实施进度（2026-11-28）

**阶段 1a 已落地（「不进安装目录写」的路由）**：

- 壳新增 `default_dirs` 命令（output / work / data），前端启动时取一次；`App` 不再把
  `"output"` 写死，相对输出目录也改为按**数据根**解析（不再是进程 cwd）。
- `convert_file` 显式传 `--work <writable_dir("work")>`；子进程设 `PDF2EPUB_PANDOC`
  （数据根或旧安装根的 pandoc）。
- `apikey.json` 新写入落数据根；旧文件存在则继续用旧文件（不制造第二份）；开发检出
  （有 `pyproject.toml`/`.venv` + `config/`）仍写项目根。
- 首启 pandoc 下载目标改为 `%LOCALAPPDATA%\pdf2epub\pandoc`；引擎侧候选链加上该位置
  （`PDF2EPUB_PANDOC` 之外的第二条保险）。
- 兼容策略：安装根下**已存在**的 `work/`、`output/`、`apikey.json`、`pandoc/` 一律沿用
  —— 现有绿色版用户行为完全不变，新装用户不再往安装目录写。
- 验证：Rust 单测 53 条（新增 `writable_dirs_prefer_legacy_install_layout`、
  `relative_output_uses_data_root_not_cwd`、重写 `apikey_prefers_data_dir_then_legacy_then_dev_checkout`）、
  Python 单测（pandoc 数据目录候选）、前端 tsc/eslint/vitest 全绿。

**阶段 2（per-user 安装器）已落地（2026-11-28，v0.6.2）**：

- 新增 `installer/installer.nsi`（NSIS 3.11）：默认 per-user，装到
  `%LOCALAPPDATA%\Programs\pdf2epub`，不弹 UAC；开始菜单快捷方式 +
  「设置 → 应用」卸载项；`/S` 静默安装、`/D=` 指定目录；卸载**只删程序，不删用户数据**。
- 新增 `scripts/build_installer.py`：复用 `build_release.py` 的 staging，自动找/下载 NSIS
  （GitHub 镜像 + 钉死的 sha256，落到 `.tools/`），编译、校验体积与 FileVersion，
  `--verify-install` 会「静默装到临时目录 → 跑 `cli.exe --help` → 静默卸载」。
- 版本守卫与 `build_release.py` 同源（版本号不一致直接拒绝构建）。
- 实测（v0.6.2）：安装包 76.9 MB、FileVersion 0.6.2；静默安装后
  `pdf2epub.exe` / `cli\cli.exe` / `config\` / `Uninstall.exe` 齐全；HKCU 卸载项含
  DisplayName/DisplayVersion/Publisher/URLInfoAbout；静默卸载后目录与注册表项都清掉；
  用户数据目录 `%LOCALAPPDATA%\pdf2epub\` 未被触碰。
- per-machine 版（`--mode machine`）安装器已经能编出来，但**先不要发布**：Program Files
  下需要阶段 1b 的自动迁移（把旧 work/output 搬到数据根），否则从绿色版升级上来的用户
  会写失败。per-user 版没有这个问题（安装目录就在 `%LOCALAPPDATA%` 下，可写）。
- 打包命令（写进 README 的贡献/发布说明前先记在这里）：

  ```powershell
  uv run python scripts/build_release.py 0.6.2 --skip-build
  uv run python scripts/build_installer.py 0.6.2 --verify-install
  ```

- CI:Windows 发布范围(`scripts/ci_scope.py` 判定为 release)被触发时,会真的编一遍
  安装包并作为 `pdf2epub-installer` artifact 上传(zip + setup.exe);staging 用
  `build_release.py --cli-dir dist_ci/cli` 指向 CI 的 PyInstaller 产物。发版时把
  setup.exe 与 zip 一起挂到 GitHub Release(见 CONTRIBUTING 的发布步骤)。

**阶段 1b（数据迁移）已落地（2026-11-28，v0.6.2）**：

- 新增 Tauri 命令 `migrate_data(source?)` + 设置页「数据位置」区块：把安装根下的
  `work/`、`output/`、`pandoc/`、`apikey.json` 搬到 `%LOCALAPPDATA%\pdf2epub\`，
  并把 `library.json` 里指向旧位置的**产物路径**、`settings.json` 里的绝对输出目录
  一起改写（`source` 是用户的源 PDF，不搬也不改）。
- 目标位置已存在的项**不覆盖**：保留旧数据并在结果里列出（`kept`）；写
  `layout.json` 标记保证幂等。
- 启动时自动迁移：仅当安装根**不可写**（per-machine 装到 `Program Files`）且还有旧数据
  时执行；可写的绿色版一律不动（`should_auto_migrate` 的三条件）。
- 验证：Rust 单测 4 条（路径改写只认旧根 / 搬 work+output+apikey 并改写库与设置 /
  目标已存在则保留 / 自动迁移只在只读安装触发），`cargo test` 58 条全绿；前端
  tsc/eslint/vitest 全绿；壳内嵌资源核对通过。
- 因此 per-machine 安装包（`build_installer.py --mode machine`）现在具备发布条件，
  但仍建议先在小范围机器上验证一次 UAC 安装/升级路径。
