# desktop — pdf2epub 桌面端

Tauri 2 + React 19 + Tailwind v4。界面主壳在 `src/App.tsx`,三屏拆在 `src/screens/`
(Convert / Library / Settings),共用组件在 `src/primitives.tsx`,纯函数在 `src/format.ts`,
领域类型在 `src/types.ts`,事件流解析在 `src/events.ts`。
主题与材质在 `src/index.css`(纸 / 墨 / 朱砂三色,变量名与 CSS 变量逐字对应)。
Rust 侧 `src-tauri/src/lib.rs` 提供 IPC:
`convert_file` / `preflight` / `cancel_convert` / `library_sync` / `library_set_meta` /
`check_env` / `save_apikey` / `set_cli_path` / `open_epub`,并通过 `conv://progress` 推送日志。

## 开发

```bash
npm install
npm run tauri dev                    # 开发模式
npm run build                        # 仅前端(tsc + vite)
npm run tauri build -- --no-bundle   # release exe → src-tauri/target/release/desktop.exe
```

产品名是 `pdf2epub`,但 Cargo crate 名是 `desktop`,所以产物文件名是 `desktop.exe`——
打包脚本 `../scripts/build_release.py` 负责把它重命名成 `pdf2epub.exe`。

## 检查与测试

```bash
npm run lint        # eslint(flat config,含 react-hooks 规则)
npm test            # vitest 单跑一遍
npm run test:watch  # 边改边跑
npm run check       # tsc + eslint + vitest,提交前跑这个
```

eslint 配置在 `eslint.config.js`,vitest 在 `vitest.config.ts`。测试文件与被测模块同目录
(`src/**/*.test.ts(x)`),环境是 jsdom,`src/test/setup.ts` 里挂了 jest-dom 断言并在每个用例后清理 DOM。

`vitest.config.ts` 里刻意设成 `pool: "forks" + singleFork`:jsdom 默认起独立 worker 会往系统
temp 写模块运行时,在受限环境(沙盒、CI 容器)下直接 EPERM。单 fork 顺序跑全套约 3 秒,不值得为并行踩这个坑。

## 引擎发现顺序

`cli.exe`(exe 同级) → 环境变量 `PDF2EPUB_CLI` / `PDF2EPUB_HOME` → 向上最多 5 级找 `.venv/Scripts/ebook-converter.exe`。
开发形态直接跑 `.venv`(改 Python 代码即时生效);发布形态是 exe 同级放 `cli.exe` + `config/`。

前置:Node.js ≥ 20 · Rust stable-msvc · Visual Studio Build Tools(C++ workload)。
