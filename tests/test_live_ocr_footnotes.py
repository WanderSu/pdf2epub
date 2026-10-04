"""真实云端 OCR 的脚注验证(**默认不跑**)。

为什么默认不跑:云端任务成功即计费,把它挂进默认套件等于每跑一次 pytest 就花掉
几页额度(而套件本身应该完全离线)。显式开启:

    PDF2EPUB_LIVE_OCR=1 uv run pytest tests/test_live_ocr_footnotes.py -q

没有 Token 时同样 skip —— **绝不伪造 PASS**(判据只有:产物里真的出现脚注文本)。

这两条用例就是本次修复的验收口径:
  - PaddleOCR-VL:云端默认忽略 `footnote`,必须靠显式 `markdownIgnoreLabels` 保留;
  - MinerU:`full.md` 不含 `page_footnote`,必须靠结构化结果补回。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from paths import load_api_key


def _live_enabled() -> bool:
    return os.environ.get("PDF2EPUB_LIVE_OCR") == "1"


def _has_token(key: str) -> bool:
    env = "PADDLEOCR_TOKEN" if key == "PaddleOCR-VL" else "MINERU_API_TOKEN"
    return bool(os.environ.get(env) or load_api_key(key))


FOOTNOTE_TEXT = "这是测试脚注:内容必须出现在最终 Markdown 里,不能丢失。"


def make_footnote_pdf(path: Path) -> Path:
    """1 页样本:正文 + 页底脚注(分隔线 + 小号字),用来验证脚注是否被保留。"""
    import pymupdf

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    margin = 60
    page.insert_textbox(pymupdf.Rect(margin, 70, 595 - margin, 110),
                        "第一章 测试章节", fontname="china-s", fontsize=18)
    page.insert_textbox(
        pymupdf.Rect(margin, 130, 595 - margin, 420),
        "本页用于验证云端 OCR 是否保留脚注。正文里出现一个注释标记①,随后继续写作,"
        "补充足够长度的文字以便版面分析能正确切块。\n\n"
        "第二段正文继续说明:脚注是学术书籍中常见的排版元素,如果 OCR 只输出正文而"
        "丢掉页底的脚注,读者就会失去这部分信息。",
        fontname="china-s", fontsize=11, lineheight=1.7,
    )
    page.draw_line(pymupdf.Point(margin, 700), pymupdf.Point(595 - margin, 700), width=0.8)
    page.insert_textbox(pymupdf.Rect(margin, 710, 595 - margin, 800),
                        f"① {FOOTNOTE_TEXT}", fontname="china-s", fontsize=8.5, lineheight=1.5)
    doc.save(path)
    doc.close()
    return path


pytestmark = pytest.mark.skipif(
    not _live_enabled(),
    reason="真实云端 OCR(消耗额度):设 PDF2EPUB_LIVE_OCR=1 显式开启",
)


def test_paddleocr_keeps_page_footnote(tmp_path: Path) -> None:
    """PaddleOCR-VL:不显式传 `markdownIgnoreLabels` 时脚注会被云端丢掉。"""
    if not _has_token("PaddleOCR-VL"):
        pytest.skip("未配置 PaddleOCR-VL Token")

    from backends.paddleocr_backend import PaddleOCRAdapter

    pdf = make_footnote_pdf(tmp_path / "脚注样本.pdf")
    result = PaddleOCRAdapter(timeout=900).convert(pdf, tmp_path / "work")

    md = result.book_md.read_text(encoding="utf-8")
    assert "这是测试脚注" in md, f"脚注被云端过滤掉了,book.md = {md!r}"


def test_mineru_recovers_page_footnote(tmp_path: Path) -> None:
    """MinerU:`full.md` 不含 page_footnote → 由结构化结果补回(=内容不能丢)。"""
    if not _has_token("MinerU"):
        pytest.skip("未配置 MinerU Token")

    from backends.mineru_backend import MinerUAdapter, CONTENT_LIST_FILE

    pdf = make_footnote_pdf(tmp_path / "脚注样本.pdf")
    work = tmp_path / "work"
    result = MinerUAdapter(timeout=900).convert(pdf, work)

    md = result.book_md.read_text(encoding="utf-8")
    part_md = next((work / "_parts").rglob("full.md")).read_text(encoding="utf-8")

    assert "这是测试脚注" in md, f"脚注既不在 full.md 也没被补回: {md!r}"
    if "这是测试脚注" not in part_md:
        # 结构化结果确实存在,且补回计数对得上
        assert result.stats["recovered_footnotes"] >= 1
        assert list((work / "_parts").rglob(CONTENT_LIST_FILE))
