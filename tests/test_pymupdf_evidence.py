"""本地文字层后端(电子版 PDF)的页边界与证据层。

电子版 PDF 曾经**整本一次提取**:页边界整段消失 —— 产物里既没有页码注释,也没有
证据层,于是跨页断行拼不上、页眉页脚无从判定、页账只能记「无从归属」。实测一本
4 页的电子书,三处句中页界原样留在正文里。

这里钉住三件事:

1. 逐页提取:``book.md`` 每页一条 ``<!-- page N -->``;
2. 同时写出 ``evidence.json``(段落级,带 0-indexed ``page_idx``);
3. ``collect_evidence`` 认得这份本地证据,且**后端不符时不采用** —— 同一个 work
   目录可能先后被不同后端写过,拿旧后端的页号对齐新正文会整本错位。
"""
from __future__ import annotations

import json

from conftest import make_pdf


def test_page_evidence_splits_paragraphs_and_images() -> None:
    """一页 Markdown → 段落级条目;标题只留文字,图片按引用名记(img_path)。"""
    from pdf2epub.backends.pymupdf_backend import page_evidence

    items = page_evidence(
        "# 第三章 标题\n\n第一段正文。\n\n![](images/fig1.png)\n\n第二段正文。\n",
        page_idx=4,
    )
    assert items == [
        {"type": "text", "text": "第三章 标题", "page_idx": 4},
        {"type": "text", "text": "第一段正文。", "page_idx": 4},
        {"type": "image", "img_path": "images/fig1.png", "page_idx": 4},
        {"type": "text", "text": "第二段正文。", "page_idx": 4},
    ]


def test_convert_writes_page_markers_and_local_evidence(tmp_path) -> None:
    """逐页提取:页码注释按页出现,证据层逐段落带页号。"""
    from pdf2epub.backends.pymupdf_backend import PyMuPDFBackend

    pdf = make_pdf(tmp_path / "三页.pdf", pages=3, with_image=True)
    work = tmp_path / "work"
    result = PyMuPDFBackend(write_images=True).convert(pdf, work)

    md = result.book_md.read_text(encoding="utf-8")
    for page in (1, 2, 3):
        assert f"<!-- page {page} -->" in md, f"缺少第 {page} 页的页码注释"
    assert result.stats["pages"] == 3

    evidence = json.loads((work / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["backend"] == "pymupdf"
    assert evidence["total_pages"] == 3
    pages = {item["page_idx"] for item in evidence["items"]}
    assert pages == {0, 1, 2}, "证据层的页号必须是 0-indexed 且覆盖每一页"
    assert any(item["type"] == "image" for item in evidence["items"]), "图片条目缺失"


def test_collect_evidence_reads_local_file_and_respects_backend(tmp_path) -> None:
    from pdf2epub.backends.base import collect_evidence
    from pdf2epub.backends.pymupdf_backend import PyMuPDFBackend

    pdf = make_pdf(tmp_path / "两页.pdf", pages=2)
    work = tmp_path / "work"
    PyMuPDFBackend(write_images=True).convert(pdf, work)

    bundle = collect_evidence(work, backend="pymupdf")
    assert bundle.available and bundle.total_pages == 2
    assert bundle.parts == ("evidence.json",)

    # 换了后端(比如重跑用了 MinerU):本地证据层不采用,页号宁可空着也不错位
    other = collect_evidence(work, backend="mineru")
    assert not other.available


def test_text_book_blocks_get_page_attribution(tmp_path) -> None:
    """有本地证据层后,每个正文块都能拿到页号(跨块拼接与页眉页脚判定的前提)。"""
    from pdf2epub.backends.base import collect_evidence
    from pdf2epub.backends.pymupdf_backend import PyMuPDFBackend
    from pdf2epub.ir import build_ir

    pdf = make_pdf(tmp_path / "三页.pdf", pages=3)
    work = tmp_path / "work"
    result = PyMuPDFBackend(write_images=True).convert(pdf, work)
    bundle = collect_evidence(work, backend="pymupdf")

    ir = build_ir(result.book_md.read_text(encoding="utf-8"), source=str(pdf),
                  backend="pymupdf", total_pages=bundle.total_pages,
                  evidence=bundle.items)
    assert ir.meta.backend_evidence["page_attribution"] == "complete"
    assert ir.links, "证据层没有挂上任何链接:对齐没跑起来"
