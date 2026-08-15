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
work/
├── book.md
└── images/
```

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

- 多余空行
- 页眉
- 页脚
- 页码
- 跨页断行
- OCR 异常空格
- 重复标题
- 空标题
- Markdown 格式错误
- 图片路径
- 标题层级

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

使用环境变量或配置文件。

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
│   │   └── pdf_detector.py
│   ├── backends/
│   │   ├── pymupdf_backend.py
│   │   ├── mineru_backend.py
│   │   └── paddleocr_backend.py
│   ├── markdown/
│   │   ├── cleaner.py
│   │   ├── validator.py
│   │   └── images.py
│   ├── epub/
│   │   ├── pandoc.py
│   │   └── validator.py
│   └── cli.py
├── config/
│   ├── config.yaml
│   └── book.css
├── tests/
├── logs/
├── output/
├── pyproject.toml
└── README.md
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

---

### Phase 7：批处理

最后实现：

- 批量处理
- 日志
- 重试
- 跳过已完成文件
- 断点续跑

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
