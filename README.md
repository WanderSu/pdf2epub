# pdf2epub

> **PDF → EPUB 电子书转换工具** · 电子版 / 扫描版 / Markdown 一键转换,自动清理,排版精致

[![Release](https://img.shields.io/github/v/release/WanderSu/pdf2epub?color=FF4D00&label=release)](https://github.com/WanderSu/pdf2epub/releases)
[![Stars](https://img.shields.io/github/stars/WanderSu/pdf2epub?color=0A0A0A&label=stars)](https://github.com/WanderSu/pdf2epub)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D6)](https://github.com/WanderSu/pdf2epub/releases)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**中文** | [English](README.en.md)

---

## 📑 目录

- [✨ 特性](#-特性)
- [🚀 快速开始](#-快速开始)
- [🖥️ 桌面端](#️-桌面端)
- [⌨️ CLI 用法](#️-cli-用法)
- [⚙️ 配置](#️-配置)
- [❓ 常见问题](#-常见问题)
- [⚠️ 已知限制](#️-已知限制)
- [📁 目录结构](#-目录结构)
- [📄 License](#-license)

---

## ✨ 特性

| 特性 | 说明 |
|---|---|
| 🔍 **自动检测** | 文字层完好 → 本地提取;纯扫描 → 云端 OCR;混合 → 页级路由 |
| 🧠 **伪文字层识别** | 提取乱码的 PDF 会提示你,由你决定是否改用 OCR |
| ☁️ **云端 OCR** | MinerU / PaddleOCR-VL 可切换;MinerU 失败自动降级为渲染纯图重试 |
| 📇 **元数据** | 文件名符合「标题 - 作者」自动嵌入 `dc:title` / `dc:creator` |
| 🧹 **智能清理** | 页码剔除、跨页断行连接、中文空格修正、强调字体标注粗体 |
| 📦 **批处理** | 失败重试(指数退避)、跳过已完成、断点续跑、单文件失败不中断 |
| 📚 **>200 页自动分片** | MinerU 单任务限 200 页/200MB;超大书自动按 page_ranges 分段提交、并行解析、按序合并 |
| 🎨 **精装书级排版** | 内置 book.css:中文衬线正文、首行缩进、标题体系、公式/表格/图片保护 |

### 工作流

```text
电子版 PDF ──► PyMuPDF4LLM(本地)──┐
扫描版 PDF ──► 云端 OCR ──────────┼──► Markdown 清理 ──► Pandoc + CSS ──► EPUB
MinerU 输出 ──► full.md + images ──┤      (自动:页码/断行/空格/粗体)
已有 Markdown ────────────────────┘
```

---

## 🚀 快速开始

### 方式一:桌面端(推荐)

从 [Releases](https://github.com/WanderSu/pdf2epub/releases) 下载 `pdf2epub-v0.1.1-win-x64.zip` 并解压到任意目录,双击 `pdf2epub.exe`。

> 💡 压缩包内含转换引擎 `cli.exe`(已内置 Python 运行环境,免安装 Python);保持 `pdf2epub.exe`、`cli.exe`、`config/` 三者同级即可运行,无需放在项目根。

**首次使用两步**:

1. 安装 [Pandoc](https://pandoc.org/installing.html)(EPUB 生成引擎,必需):`winget install pandoc`
2. 在解压目录创建 `apikey.json`(云端 OCR 凭证,模板见下方「准备凭证」)

- **Import** — 拖放 / 选择 PDF、Markdown
- **Queue** — 转换队列,实时进度、日志、后端徽标
- **Library** — 转换结果,一键在资源管理器中定位
- **Settings** — OCR 后端、输出目录、CLI 路径、清理选项、主题(亮/暗)

### 方式二:CLI

**依赖**:Python 3.12 · [uv](https://docs.astral.sh/uv/) · [Pandoc](https://pandoc.org/installing.html)(≥3.0,需在 PATH 中)

```bash
git clone <repo-url> pdf2epub
cd pdf2epub
uv sync    # 创建 .venv 并安装 ebook-converter 命令
```

**准备凭证**(仅扫描版 / OCR 需要)——项目根创建 `apikey.json`(已 gitignore):

```json
{
    "MinerU": "你的 MinerU Token",
    "PaddleOCR-VL": "你的 PaddleOCR Token"
}
```

也可以用环境变量 `MINERU_API_TOKEN` / `PADDLEOCR_TOKEN`(优先级更高)。

**转换**:

```bash
# 单个文件(自动检测类型)
.venv/Scripts/ebook-converter.exe "我的书.pdf" -o output

# 整目录批处理
.venv/Scripts/ebook-converter.exe books/ -o output

# 已有 Markdown(含 MinerU 桌面端输出的 full.md,按「书名.md + images/」放置)
.venv/Scripts/ebook-converter.exe "我的书.md" -o output

# 强制指定 OCR 后端
.venv/Scripts/ebook-converter.exe "扫描书.pdf" -o output --backend mineru
```

**输出**:每本书生成 `output/<书名>.epub`;中间产物在 `work/<书名>/book.md + images/`(可手工修订后重新生成)。

> ⚠️ 检测到疑似**伪文字层**时,单文件转换会交互询问是否改用 OCR;批处理模式不打断,仅在日志中警告。

---

## 🖥️ 桌面端

基于 **Tauri 2 + React**,UI 源自 Figma 设计稿(瑞士国际主义风格,黑白 + 橙色强调)。

### 从源码构建

```bash
cd desktop
npm install
npm run tauri dev                    # 开发模式
npm run tauri build -- --no-bundle   # 构建 release exe
```

前置要求:Node.js ≥ 20 · Rust(`stable-x86_64-pc-windows-msvc`)· Visual Studio Build Tools(C++ workload)

---

## ⌨️ CLI 用法

```
ebook-converter <文件或目录>... [-o 输出目录] [选项]
```

| 选项 | 说明 |
|---|---|
| `--backend {auto,pymupdf,mineru,paddleocr}` | 强制指定后端(默认 `auto` 自动检测) |
| `-o, --output DIR` | EPUB 输出目录(默认 `output/`) |
| `--retries N` | 单文件失败重试次数,指数退避(默认 2) |
| `--force` | 忽略「已完成」状态,强制重新转换 |
| `--no-log` | 不写日志文件 |
| `--verbose` | 控制台输出 DEBUG 日志 |

---

## ⚙️ 配置

`config/config.yaml`:

```yaml
ocr_backend: mineru          # 默认 OCR 后端:mineru / paddleocr
mineru:
  max_pages_per_task: 200    # MinerU 单任务页数上限;超过自动分片提交(page_ranges)
pymupdf:
  write_images: true
  bold_fonts: [...]          # 强调字体列表(楷体/中宋等),其文字标注为 **粗体**
```

`config/book.css` — EPUB 全局样式(精装书级中文排版:衬线正文、首行缩进、标题体系、公式/表格/图片保护),可按需修改。

---

## ❓ 常见问题

<details>
<summary><b>扫描 PDF 转换很慢 / 耗 Token?</b></summary>

OCR 是云端按页计费服务。确认这本书值得转再跑;`--retries 0` 可避免失败重试浪费配额。
</details>

<details>
<summary><b>检测说「文字层损坏」但我想要本地提取?</b></summary>

交互询问时选 `[3] 继续本地提取`,或直接 `--backend pymupdf`。注意结果可能不可读。
</details>

<details>
<summary><b>MinerU 解析失败(parsing failed)?</b></summary>

工具会自动降级为「渲染纯图后重试」,一般可解决;仍失败可换 `--backend paddleocr` 或检查文件。
</details>

<details>
<summary><b>超过 200 页的书能转吗?</b></summary>

可以。MinerU 单任务限 200 页,工具自动按 200 页分段(page_ranges)并行解析后按序合并,日志会显示「自动分片 N 段」。注意云端每日有 1000 页优先额度。
</details>

<details>
<summary><b>转换结果有乱码 / 页码 / 断行问题?</b></summary>

`src/markdown/cleaner.py` 负责清理。若你的书出现误删/误拼,调整其中的规则或反馈维护者。
</details>

<details>
<summary><b>书名 / 作者不对?</b></summary>

文件名命名为「标题 - 作者」格式(如 `三体 - 刘慈欣.pdf`),元数据自动正确;否则 EPUB 标题取文件名。
</details>

---

## ⚠️ 已知限制

- PyMuPDF4LLM 提取行间公式为图片(非 LaTeX);云端 OCR 的 LaTeX 公式可正常转为 MathML
- 双栏 PDF 偶发同行合并(边缘情况)
- 页码剔除 / 断行拼接为启发式规则,极端排版可能有误伤
- MinerU 分片后,跨段边界的表格 / 段落可能被截断(清理规则可部分弥补)
- EPUBCheck 未安装,未做 EPUB 标准合规验证

---

## 📁 目录结构

```text
src/
  backends/         # 后端:base(抽象) / pymupdf / mineru / paddleocr
  detector/         # PDF 类型自动检测(含伪文字层识别)
  markdown/         # cleaner(清理) / bold(粗体标注)
  epub/             # pandoc 封装
  batch.py          # 批处理(重试/跳过/断点续跑)
  cli.py            # ebook-converter 命令入口
  convert.py        # 自动路由(text/scanned/hybrid)
  paths.py          # 路径与凭证(apikey.json)读取
config/             # config.yaml + book.css
desktop/            # Tauri 2 桌面端(React + Tailwind v4)
scripts/            # 测试样本生成 / 端到端测试 / EPUB 验证
```

---

## 📄 License

[MIT](LICENSE) © 2026 WanderSu

> ⚠️ 本仓库不含任何书籍内容,仅代码与自生成测试样本。转换受版权保护的书籍仅供个人使用,请勿分发转换产物。
