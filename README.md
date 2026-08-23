# pdf2epub

PDF → EPUB 电子书转换工具。统一处理电子版 PDF、扫描版 PDF 和已有 Markdown,输出高质量 EPUB。

方案详见 [`IDEA.md`](IDEA.md)。

## 工作流

```text
电子版 PDF ──► PyMuPDF4LLM ──┐
扫描版 PDF ──► 云端 OCR ─────┼──► Markdown 清理 ──► Pandoc + CSS ──► EPUB
已有 Markdown ──────────────┘   (MinerU / PaddleOCR-VL)
```

所有输入最终统一为 `work/<书名>/book.md + images/`,再经 Pandoc 生成 EPUB。

## 环境

- Python 3.12(由 `.python-version` 固定,uv 管理)
- Pandoc(系统安装,≥3.0)
- uv

```bash
uv sync        # 安装项目依赖
```

## 当前进度

| Phase | 状态 |
|---|---|
| 1 环境检查 | ✅ |
| 2 Markdown → EPUB | ✅ |
| 3 电子 PDF → Markdown(PyMuPDF4LLM) | ✅ |
| 4 MinerU Cloud(Adapter) | ✅ |
| 5 PaddleOCR-VL 1.6(Adapter) | ✅ |
| 6 PDF 类型自动检测 + backend 自动选择 | ✅ |
| 7 批处理(日志/重试/跳过/断点续跑) | ✅ |

## 后端适配器

统一接口见 `src/backends/base.py`,切换方式见 `config/config.yaml` 的 `ocr_backend` 字段:

```bash
export MINERU_API_TOKEN=your_token
python -c "from backends import get_backend; r = get_backend('mineru').convert('book.pdf', 'work/book')"
```

凭证一律使用环境变量,不写入代码或配置:`MINERU_API_TOKEN`(MinerU)、`PADDLEOCR_TOKEN`(PaddleOCR-VL)。

## 测试与验证

```bash
# Phase 3:电子 PDF 端到端(3 个样本)
.venv/Scripts/python.exe scripts/test_phase3_e2e.py

# Phase 4:MinerU Cloud 端到端(需 MINERU_API_TOKEN)
.venv/Scripts/python.exe scripts/test_phase4_e2e.py

# EPUB 结构验证(自研,可指定期望内容)
.venv/Scripts/python.exe scripts/verify_epub.py output/book.epub --expect-images 2
```

测试样本由 `scripts/make_test_images.py`、`scripts/make_test_pdfs.py`、`scripts/make_scanned_pdf.py` 生成。

## 已知限制

- PyMuPDF4LLM 提取行间公式为图片(非 LaTeX),行内公式转为斜体/上标
- PDF 页脚脚注提取为引用块,需 Markdown 清理阶段转为脚注语法
- 双栏 PDF 偶发同行合并
- EPUBCheck 未安装,未做标准合规验证;EPUB 需在阅读器中实际打开验证
