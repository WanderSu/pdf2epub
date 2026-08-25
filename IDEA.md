# PDF → EPUB 电子书转换工具

## 1. 项目目标

构建一个 Windows 下的电子书转换工具，统一处理：

1. 可复制文字的电子版 PDF
2. 扫描版 PDF
3. 已经 OCR 完成的 Markdown 文件

最终统一输出高质量 EPUB。

核心设计：

电子版 PDF
→ PyMuPDF4LLM
→ Markdown + images/
→ Markdown 清理
→ Pandoc + EPUB CSS
→ EPUB

扫描版 PDF
→ 云端 MinerU 或 PaddleOCR-VL 1.6
→ Markdown + images/
→ Markdown 清理
→ Pandoc + EPUB CSS
→ EPUB

已有 Markdown
→ Markdown 清理
→ Pandoc + EPUB CSS
→ EPUB

---

## 2. 核心工作流

### 电子版 PDF

适用于具有可靠文字层、可以直接复制文字的 PDF。

```text
PDF
 ↓
PDF 类型检测
 ↓
PyMuPDF4LLM
 ↓
Markdown + images/
 ↓
Markdown 清理
 ↓
Pandoc + EPUB CSS
 ↓
EPUB
```

默认不进行 OCR。

### 扫描版 PDF

扫描 PDF 的 OCR 不在本地运行。

OCR 使用云端服务，可切换：

- MinerU Cloud
- PaddleOCR-VL 1.6

```text
扫描 PDF
 ↓
云端 OCR
 ├── MinerU
 └── PaddleOCR-VL 1.6
 ↓
统一 Markdown + images/
 ↓
Markdown 清理
 ↓
Pandoc + EPUB CSS
 ↓
EPUB
```

OCR 后端必须通过 Adapter 接口接入，不能让具体 OCR 服务与后续流程耦合。

### 已有 Markdown

已经 OCR 完成的 Markdown 不再重新 OCR。

```text
book.md + images/
 ↓
Markdown 清理
 ↓
Pandoc + EPUB CSS
 ↓
EPUB
```

---

## 3. PDF 类型

需要支持三种情况：

### text

PDF 存在可靠文字层。

→ PyMuPDF4LLM

### scanned

PDF 基本没有有效文字层。

→ 云端 OCR

### 伪文字层(检测增强,已实现)

部分 PDF 文字层损坏(嵌入字体无正确 ToUnicode 映射),提取出乱码但渲染正常。

检测:统计页内「可疑字符」占比(私有区 / 替换符 / 框线绘图 / 杂项符号 / CJK 扩展区),超过阈值(25%)视为伪文字层页。

处理:检测到疑似伪文字层时,**由用户手动决定是否改用 OCR**(单文件交互询问;批处理仅警告)。

### hybrid

部分页面存在文字层，部分页面是扫描内容。

需要保留 hybrid 能力。

优先提取原生文字，无法提取的内容再进入 OCR 流程。

不要仅根据 PDF 文件大小判断类型。

---

## 4. 技术选型

### 核心

- Python
- uv
- PyMuPDF4LLM
- Pandoc

### 云端 OCR

- MinerU Cloud
- PaddleOCR-VL 1.6

### 桌面端(Phase 8 新增)

- Tauri 2 + React + Tailwind v4
- UI 源自 Figma 设计稿,桥接 CLI 子进程
- 构建工具链:Windows MSVC

### EPUB

统一使用 Pandoc。

项目自行维护 EPUB CSS，不依赖已经存在的 `book.css`。

---

## 5. 不使用的工具

当前阶段不要引入：

- 本地 MinerU
- 本地 PaddleOCR-VL
- pdf-craft
- research2epub
- Docling
- Marker

除非实际测试证明当前方案无法满足需求，否则不要增加新的 PDF 解析框架。

---

## 6. Markdown 是核心中间格式

所有来源最终都必须进入统一结构：

```text
work/<书名>/
├── book.md
└── images/
```

> 注：实际实现中按书名分目录(`work/<书名>/`),便于多书并行与批处理跳过判定。

不同解析器不能直接生成各自独立的 EPUB。

应该统一：

```text
各种输入
 ↓
Markdown
 ↓
清理
 ↓
Pandoc
 ↓
EPUB
```

这样以后可以方便替换 PDF 解析器和 OCR 服务。

---

## 7. Markdown 清理

建立独立的 Markdown 清理模块。

需要处理：

- 多余空行 ✅ 已实现(连续 ≥3 压缩)
- 页眉 ⏳ 未实现(依赖 OCR 服务自身过滤)
- 页脚 ⏳ 未实现(同上)
- 页码 ✅ 已实现(独立纯数字行剔除)
- 跨页断行 ✅ 已实现(结构感知拼接,保护代码块/表格)
- OCR 异常空格 ⏳ 未实现(如 "Py Mu PDF 4 LLM" 类,待处理)
- 重复标题 ⏳ 未实现
- 空标题 ⏳ 未实现
- Markdown 格式错误 ⏳ 未实现
- 图片路径 ✅ 已实现(引用存在性校验)
- 标题层级 ⏳ 未实现
- 中文排版空格 ✅ 已实现(汉字-汉字/数字、中文标点两侧、标签与括号两侧)
- 强调字体粗体标注 ✅ 已实现(`src/markdown/bold.py`,KaiTi/中宋等 → `**`)

原则：

> 修复结构，而不是改写正文。

禁止使用 LLM 大规模重写正文。

尤其中文书籍必须尽量保持原文。

---

## 8. 图片

图片是 EPUB 的硬性质量要求。

必须保证：

```text
PDF / OCR
 ↓
images/
 ↓
Markdown 正确引用
 ↓
Pandoc
 ↓
EPUB
```

最终 EPUB 中：

- 图片不能丢失
- 图片路径必须正确
- 中文文件名必须正常
- 空格和特殊字符必须正常
- MIME type 必须正确
- 图片不能无故被裁切
- 图片应根据阅读器宽度自适应
- 阅读器必须能够正常显示

生成 EPUB 后需要检查图片引用和实际文件是否匹配。

---

## 9. 数学公式

公式是 EPUB 的硬性质量要求。

Markdown 中优先保持：

```markdown
$...$
```

和：

```markdown
$$
...
$$
```

或者其他 Pandoc 能可靠转换的数学表示。

最终 EPUB 必须实际检查公式是否能够显示。

不能仅因为 Pandoc 命令执行成功，就认为公式正常。

如果默认 Pandoc EPUB 方案不能可靠显示公式，需要调整转换方案。

注意：

> CSS 只能负责公式的显示和排版，不能代替数学公式的正确转换。

目标：

> 公式在最终 EPUB 阅读器中可读，并尽量保持原始结构。

---

## 10. EPUB CSS

项目自行维护基础 EPUB CSS，例如：

```text
config/
└── book.css
```

不依赖旧的 `book.css`，因为原文件已经删除。

CSS 的目标是：

- 中文正文舒适阅读
- 合理的字体大小和行距
- 合理的段落间距
- H1/H2/H3 层级清晰
- 图片自适应阅读器宽度
- 图片不超出页面
- 脚注正常显示
- 表格尽量适应屏幕
- 公式显示区域不被破坏
- 不使用过度复杂的固定布局
- 兼容常见 EPUB 阅读器

不要一开始设计复杂的主题。

优先建立简单、稳定、跨阅读器兼容的基础 CSS。

CSS 应该独立于 Markdown 清理和 PDF 解析模块。

---

## 11. EPUB

统一使用 Pandoc。

默认考虑：

```text
--toc
--toc-depth=3
--css=config/book.css
```

但具体参数以实际测试为准。

需要支持：

- 中文
- 图片
- 数学公式
- 脚注
- TOC
- 标题
- 超链接
- 表格
- 代码块

---

## 12. EPUB 验证

生成 EPUB 后不能只检查命令是否成功。

至少检查：

- EPUB 文件结构
- XHTML
- 图片
- CSS
- 图片引用
- 数学公式
- TOC
- 空章节
- 内部链接

如果环境中有 EPUBCheck，可以使用。

如果没有 EPUBCheck，不要声称完成了 EPUB 标准验证。

至少需要实际使用一个 EPUB 阅读器验证最终文件。

重点确认：

1. 中文正文正常
2. 图片正常
3. 数学公式正常
4. TOC 正常
5. CSS 正常
6. 脚注正常

---

## 13. OCR Adapter

MinerU 和 PaddleOCR-VL 必须有统一接口。

例如：

```python
class OCRBackend:
    def convert(self, pdf_path) -> ConversionResult:
        ...
```

实现：

- `MinerUAdapter`
- `PaddleOCRAdapter`

配置能够切换：

```text
ocr_backend = mineru
```

或者：

```text
ocr_backend = paddleocr
```

API Key 和 Endpoint 不得硬编码。

使用环境变量或配置文件。✅ 已实现,读取顺序:显式参数 → 环境变量(`MINERU_API_TOKEN` / `PADDLEOCR_TOKEN`)→ 项目根 `apikey.json`(已 gitignore,由用户维护)。

> 补充：MinerU 解析失败时(如伪文字层 PDF)自动降级为「渲染纯图(JPEG 压缩)后重试」,已固化在后端内。

> 补充：**>200 页自动分片(2026-08 实现)**。MinerU 官方精准解析 API 单任务限制 ≤200 页 / ≤200MB(超限错误码 `-60006`,官方建议"拆分文件或使用 page_ranges")。实现采用 page_ranges 方案:`MinerUAdapter` 提交 PDF 时按 `mineru.max_pages_per_task`(默认 200)切分为多个 files 条目(如 302 页 → `1-200`、`201-302`),同一 batch 并行解析;条目名带 `_partN` 后缀 + `data_id`,轮询按 data_id/file_name 区分;结果下载后按段序合并为统一 `work/book.md` + `work/images/`(跨段图片重名自动加 `p{N}_` 前缀并替换引用),并加 `<!-- page-group N -->` 页标记(与 hybrid 流程一致)。CLI 与桌面端(Tauri 壳为 CLI 子进程)均自动生效。

---

## 14. CLI

最终希望支持：

```powershell
ebook-converter book.pdf
```

自动判断 PDF 类型。

也支持：

```powershell
ebook-converter book.pdf --backend pymupdf
ebook-converter book.pdf --backend mineru
ebook-converter book.pdf --backend paddleocr
ebook-converter book.md
ebook-converter ./books/
```

批量处理不能因为单个文件失败而全部停止。

---

## 15. 批处理

需要支持：

- 批量处理
- 日志
- 失败重试
- 跳过已完成文件
- 断点续跑

默认不要大量并发。

云端 OCR 必须考虑 API 限制。

优先保证稳定性。

---

## 16. 推荐目录结构

```text
ebook-converter/
├── src/
│   ├── detector/
│   │   └── pdf_detector.py      # 类型检测(含乱码率/伪文字层判定)
│   ├── backends/
│   │   ├── base.py              # Backend 抽象 + normalize_image_refs
│   │   ├── pymupdf_backend.py
│   │   ├── mineru_backend.py    # 含渲染降级重试
│   │   └── paddleocr_backend.py
│   ├── markdown/
│   │   ├── cleaner.py           # 清理(页码/断行/空格/图片校验)
│   │   └── bold.py              # 强调字体 → 粗体标注
│   ├── epub/
│   │   └── pandoc.py            # Pandoc → EPUB 封装
│   ├── batch.py                 # 批处理(重试/跳过/断点续跑)
│   ├── convert.py               # 自动路由(text/scanned/hybrid)
│   ├── cli.py                   # ebook-converter 命令入口
│   └── paths.py                 # 路径与 apikey.json 凭证读取
├── config/
│   ├── config.yaml
│   └── book.css
├── desktop/                     # Tauri 2 桌面端(React + Tailwind v4,Figma 设计稿)
├── tests/                       # 真实书测试样本(已 gitignore)
├── logs/
├── output/
├── pyproject.toml
├── README.md
└── IDEA.md
```

实际结构可以根据工程需要调整，不要求机械遵守。

---

## 17. 开发顺序

### Phase 1：环境检查

检查：

- Python
- uv
- Pandoc
- PyMuPDF4LLM
- 当前项目
- 当前文件结构

不要直接修改环境。

✅ 已完成。

---

### Phase 2：Markdown → EPUB

先完成：

```text
Markdown + images
 ↓
Pandoc + config/book.css
 ↓
EPUB
```

建立最小测试 Markdown，同时包含：

- 中文正文
- 一级标题
- 二级标题
- 图片
- 行内公式
- 行间公式
- 脚注
- 超链接

重点验证：

- 中文
- 图片
- 数学公式
- TOC
- CSS
- 脚注

必须实际打开生成的 EPUB 进行验证。

✅ 已完成。

---


---

### Phase 3：电子 PDF → Markdown

实现：

```text
电子 PDF
 ↓
PyMuPDF4LLM
 ↓
Markdown + images/
 ↓
EPUB
```

重点测试：

- 普通中文电子书
- 双栏电子书
- 图片较多的 PDF
- 含公式的 PDF
- 含脚注的 PDF

✅ 已完成。

---


---

### Phase 4：MinerU Cloud

加入：

```text
扫描 PDF
 ↓
MinerU Cloud
 ↓
统一 Markdown + images/
```

✅ 已完成。

---


---

### Phase 5：PaddleOCR-VL 1.6

加入：

```text
扫描 PDF
 ↓
PaddleOCR-VL 1.6
 ↓
统一 Markdown + images/
```

确保其输出可以进入与 MinerU 完全相同的后处理流程。

✅ 已完成。

---


---

### Phase 6：自动检测

实现：

```text
PDF
 ↓
PDFDetector
 ↓
text / scanned / hybrid
```

对应：

```text
text
→ PyMuPDF4LLM

scanned
→ 配置的 OCR backend

hybrid
→ hybrid 流程
```

✅ 已完成。

---


---

### Phase 7：批处理

最后实现：

- 批量处理
- 日志
- 重试
- 跳过已完成文件
- 断点续跑

✅ 已完成。

---

### Phase 8：增强与桌面端(已完成)

在 Phase 1-7 基础上追加：

- ✅ 文件名「标题 - 作者」→ EPUB 元数据(`dc:title` / `dc:creator`)
- ✅ 凭证支持项目根 `apikey.json`(环境变量优先,已 gitignore)
- ✅ 伪文字层检测(乱码率)+ 运行时交互询问是否 OCR
- ✅ MinerU 解析失败自动降级「渲染纯图(JPEG)重试」
- ✅ MinerU >200 页自动分片(page_ranges 方案,详见 §13 补充;CLI/桌面端均生效)
- ✅ 桌面端:CLI 子进程隐藏控制台黑窗口(`CREATE_NO_WINDOW`)+ CLI stdout 行缓冲,前端日志实时显示、可点击展开完整日志
- ✅ 中文清理增强:页码剔除、跨页断行连接(结构感知)、中文空格修正、强调字体粗体标注
- ✅ 桌面端:Tauri 2 + React,UI 源自 Figma 设计稿(瑞士国际主义风格),桥接 CLI 子进程 + 进度事件
- ✅ 工具链:Windows MSVC(Visual Studio Build Tools + rustup stable-x86_64-pc-windows-msvc)
- ✅ 发布:GitHub Release v0.1.0(绿色版 exe),MIT License,仓库公开

---

## 18. 测试

至少测试：

1. 普通中文电子 PDF
2. 双栏电子 PDF
3. 扫描中文书
4. 复杂排版扫描书
5. 混合型 PDF
6. 已有 OCR Markdown
7. 大量图片的书
8. 数学公式较多的书
9. 有脚注的书
10. 中英文混排的书

重点检查：

- 文本是否缺失
- 阅读顺序
- 标题层级
- TOC
- 图片
- 公式
- 脚注
- CSS
- EPUB 阅读器兼容性

---

## 19. 开发原则

1. 不部署本地 MinerU。
2. 不部署本地 PaddleOCR-VL。
3. OCR 使用云端。
4. 已有 Markdown 不重新 OCR。
5. 不为了“智能”而使用 LLM 重写正文。
6. 不过度引入新的框架。
7. 优先使用 uv。
8. 项目自行维护 `config/book.css`。
9. 图片和公式是硬性验收项目。
10. “Pandoc 命令执行成功”不等于 EPUB 质量合格。
11. OCR 后端必须通过 Adapter 解耦。
12. 每完成一个 Phase 都先测试，再进入下一阶段。
13. 如果发现架构问题，先说明问题和影响，再修改。
14. 优先简单、可靠、可维护，而不是功能堆砌。
15. 不要为了处理一种特殊 PDF 而引入大量新的依赖。
16. 优先保证最终 EPUB 的阅读体验，而不是追求中间 Markdown 的形式复杂度。
17. 伪文字层等检测结果不可靠时，由用户手动决定是否使用 OCR，不强行自动判定。
18. 凭证只存于环境变量或 `apikey.json`(gitignore)，绝不入库；Token 不打印、不进会话记录。
