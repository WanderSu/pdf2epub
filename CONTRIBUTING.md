# 参与开发

感谢你愿意改进 pdf2epub。这份文档只讲「怎么搭环境、怎么改、怎么提交、怎么发布」,
设计与取舍见 `docs/design.md`。

## 环境

| 组件 | 版本 | 用途 |
|---|---|---|
| Python | 3.12+ | 引擎 |
| [uv](https://docs.astral.sh/uv/) | 最新 | 依赖与虚拟环境 |
| Node.js | 20+ | 桌面端前端 |
| Rust | stable | 桌面壳(Tauri 2) |
| pandoc | 3.x | Markdown → EPUB;缺失时相关用例会被跳过 |

云端 OCR(MinerU / PaddleOCR-VL)只在显式开启时才跑;离线开发不需要凭证。

## 安装依赖

依赖一律按锁文件安装,不要用无锁更新:

```bash
uv sync --locked
cd desktop && npm ci
```

凭证只放在项目根的 `apikey.json`(已 gitignore)或环境变量
`MINERU_API_TOKEN` / `PADDLEOCR_TOKEN`,**任何情况下都不要提交、粘贴或写进日志**。

## 测试

```bash
uv run pytest -q                    # 全量离线测试
uvx ruff check                      # 静态检查(pyproject 的 include 名单)
cd desktop && npm run check         # tsc + eslint + vitest
cd desktop/src-tauri && cargo test --locked
```

真实云端 OCR 消耗额度,默认不跑;需要时显式开启:

```bash
PDF2EPUB_LIVE_OCR=1 uv run pytest -q tests/test_live_ocr_footnotes.py
```

新增网络、缓存、解压相关逻辑时,**必须补异常、超时、重试与体积上限的用例** ——
这些路径出了问题不会报错,只会静默产出坏书或重复扣费。

## 提交约定

- **一个提交只做一件事**:能独立描述、能单独跑过相关用例。多件事混在一个提交里
  会被要求重拆。
- 改动前先跑相关用例,改动后先跑最小集、再跑全量。
- 提交说明写清 **风险 / 测试 / 回滚方式**;测试数字要来自真实运行。
- 不删除已有的缓存兼容逻辑,不降低 EPUB 校验严格度,不改变文件名、工作目录与
  产物恢复语义。
- 不要提交:真实 Token、PDF/EPUB 样本、`work/`、`output/`、`logs/`、用户数据。

## 分支与发布

`main` 受保护:禁止 force-push 与删除,合并前必须 CI 通过(引擎/前端/Rust 与
Windows 发布形态两条流水线)。

发布是维护者的手工步骤,**不放在 CI 里自动执行**:

```bash
uv run python scripts/bump_version.py <版本>
uv run python scripts/build_release.py <版本>
uv run python scripts/check_release.py dist_release/<版本>
gh release create v<版本> <zip> --notes-file <notes>
```

发布前确认 zip 里没有 `apikey.json`、`work/`、`output/`、`logs/` 与本机路径。
