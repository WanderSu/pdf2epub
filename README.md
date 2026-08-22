# pdf2epub

[![Release](https://img.shields.io/github/v/release/WanderSu/pdf2epub?color=FF4D00&label=release)](https://github.com/WanderSu/pdf2epub/releases)
[![Stars](https://img.shields.io/github/stars/WanderSu/pdf2epub?color=0A0A0A&label=stars)](https://github.com/WanderSu/pdf2epub)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D6)](https://github.com/WanderSu/pdf2epub/releases)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**中文** | [English](README.en.md)

PDF → EPUB 电子书转换工具。统一处理**电子版 PDF**、**扫描版 PDF**、**MinerU 桌面端/云端输出**和**已有 Markdown**,自动检测、自动清理,输出排版良好的 EPUB。

提供 **CLI** 与 **桌面端(Tauri + Figma 设计稿)** 两种使用方式。

> 方案设计详见 [`IDEA.md`](IDEA.md)。

## 它能做什么

```text
电子版 PDF ──► PyMuPDF4LLM(本地)──┐
扫描版 PDF ──► 云端 OCR ──────────┼──► Markdown 清理 ──► Pandoc + CSS ──► EPUB
MinerU 输出 ──► full.md + images ──┤      (自动:页码/断行/空格/粗体)
已有 Markdown ────────────────────┘
```

- **自动检测** PDF 类型:文字层完好 → 本地提取;纯扫描 → 云端 OCR;混合 → 页级路由
- **伪文字层识别**:提取乱码的 PDF(渲染正常但文字层损坏)会提示你,由你决定是否改用 OCR
- **云端 OCR**:MinerU 或 PaddleOCR-VL(二选一,可切换),MinerU 解析失败时自动降级为"渲染纯图后重试"
- **元数据**:文件名符合「标题 - 作者」格式时,自动嵌入 EPUB 的 `dc:title` / `dc:creator`
- **清理**:页码剔除、跨页断行连接、中文排版空格修正、强调字体标注粗体、图片引用校验
- **批处理**:失败重试(指数退避)、跳过已完成、断点续跑、单文件失败不中断

## 桌面端(推荐)

基于 **Tauri 2 + React**,UI 源自 Figma 设计稿(瑞士国际主义风格,黑白 + 橙色强调,支持亮/暗主题)。

- **Import**:拖放/选择 PDF、Markdown
- **Queue**:转换队列,实时进度、日志、后端徽标(LOCAL / MINERU / PADDLE)
- **Library**:转换结果,一键在资源管理器中定位
- **Settings**:OCR 后端(auto / MinerU / PaddleOCR-VL)、输出目录、CLI 路径、清理选项、主题

### 安装与运行

发布版(绿色 exe,免安装):从 [Releases](https://github.com/WanderSu/pdf2epub/releases) 下载 `pdf2epub.exe`,**放到项目根目录**运行(自动定位 `.venv` 中的转换引擎;放其他位置需在 Settings 中手动填写 CLI 路径)。

### 从源码构建

```bash
cd desktop
npm install
npm run tauri dev      # 开发模式
npm run tauri build -- --no-bundle   # 构建 release exe(输出 src-tauri/target/release/)
```

前置要求:Node.js ≥ 20、Rust(stable-x86_64-pc-windows-msvc)、Visual Studio Build Tools(C++ workload)。

## CLI 安装

**依赖**:Python 3.12、[uv](https://docs.astral.sh/uv/)、[Pandoc](https://pandoc.org/installing.html)(≥3.0,需 `pandoc` 在 PATH 中)。

```bash
git clone <your-repo-url> pdf2epub
cd pdf2epub
uv sync          # 创建 .venv 并安装依赖(含 ebook-converter 命令)
```

## 快速开始

### 1. 准备凭证(仅扫描版/OCR 需要)

在项目根目录创建 `apikey.json`(已被 gitignore,不会入库):

```json
{
    "MinerU": "你的 MinerU Token",
    "PaddleOCR-VL": "你的 PaddleOCR Token"
}
```

也可以改用环境变量:`MINERU_API_TOKEN` / `PADDLEOCR_TOKEN`(优先级更高)。

### 2. 转换

```bash
# 单个文件(自动检测类型)
.venv/Scripts/ebook-converter.exe "我的书.pdf" -o output

# 整目录批处理
.venv/Scripts/ebook-converter.exe books/ -o output

# 已有 Markdown(含 MinerU 桌面端输出的 full.md,按「书名.md + images/」放置即可)
.venv/Scripts/ebook-converter.exe "社会改良还是社会革命？.md" -o output

# 强制指定 OCR 后端(跳过自动检测)
.venv/Scripts/ebook-converter.exe "扫描书.pdf" -o output --backend mineru
.venv/Scripts/ebook-converter.exe "扫描书.pdf" -o output --backend paddleocr
```

输出:每本书生成 `output/<书名>.epub`;中间产物在 `work/<书名>/book.md + images/`(可手工修订后重新生成 EPUB)。

### 3. 交互提示

检测到疑似**伪文字层**(提取文本不可读)时,单文件转换会询问你:

```
⚠️ 检测到 84/155 页疑似文字层损坏(乱码),本地提取的文本可能不可读(当前类型: hybrid)。
是否改用云端 OCR?
  [1] MinerU
  [2] PaddleOCR-VL
  [3] 继续本地提取
  [0] 取消
```

批处理模式不打断,会在日志中给出警告和重跑建议。

## 命令行选项

```
ebook-converter <文件或目录>... [-o 输出目录] [选项]

  --backend {auto,mineru,paddleocr,pymupdf}  强制指定后端(auto=自动检测,默认)
  --retries N     单文件失败重试次数(指数退避,默认 2)
  --force         忽略"已完成"状态,强制重新转换
  --no-log        不写日志文件
  --verbose       控制台输出 DEBUG 日志
```

## 配置

`config/config.yaml` 常用项:

```yaml
ocr_backend: mineru          # 默认 OCR 后端:mineru / paddleocr
pymupdf:
  write_images: true
  bold_fonts: [...]          # 强调字体列表:这些字体的文字标注为 **粗体**
                             # (如楷体/中宋排版的引文,按需为每本书调整)
```

`config/book.css`:EPUB 全局样式(中文排版优先,图片自适应,公式区保护),可按需修改。

## 验证

```bash
# EPUB 结构验证(自研,支持 --expect-images / --expect-formulas 等)
.venv/Scripts/python.exe scripts/verify_epub.py output/我的书.epub --expect-images 2
```

建议转换后在实际阅读器(Apple Books / 微信读书 / KOReader 等)中打开检查渲染效果。

## 常见问题

**Q:扫描 PDF 转换很慢 / 耗 Token?**
OCR 是云端服务(按页计费),扫描书转换必然涉及网络往返。确认这本书值得转再跑;`--retries 0` 可避免失败重试浪费配额。

**Q:检测说"文字层损坏"但我想要本地提取?**
交互询问时选 `[3] 继续本地提取`;或直接用 `--backend pymupdf`。注意结果可能不可读。

**Q:MinerU 解析失败(parsing failed)?**
工具会自动降级为"渲染纯图后重试",一般可解决。若仍失败,可换 `--backend paddleocr` 或检查文件是否损坏。

**Q:转换结果有乱码 / 页码 / 断行问题?**
`src/markdown/cleaner.py` 负责清理(页码剔除、跨页断行连接、中文空格修正)。若你的书出现误删/误拼,调整其中的规则或告知维护者。

**Q:书名 / 作者不对?**
文件名命名为「标题 - 作者」格式(如 `三体 - 刘慈欣.pdf`),元数据自动正确;否则 EPUB 标题取文件名。

## 已知限制

- PyMuPDF4LLM 提取行间公式为图片(非 LaTeX);云端 OCR 输出的 LaTeX 公式可正常转为 MathML
- 双栏 PDF 偶发同行合并(边缘情况)
- 页码剔除/断行拼接是启发式规则,极端排版可能有误伤
- EPUBCheck 未安装,未做 EPUB 标准合规验证

## 目录结构

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
scripts/            # 测试样本生成 / 端到端测试 / EPUB 验证
```
