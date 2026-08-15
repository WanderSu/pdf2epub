"""生成 hybrid 测试 PDF:文字层页 + 扫描页混合(Phase 6 测试用)。"""
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples" / "pdfs" / "混合型测试.pdf"


def main() -> None:
    text_pdf = ROOT / "samples" / "pdfs" / "中文电子书测试.pdf"
    scan_pdf = ROOT / "samples" / "pdfs" / "扫描版测试.pdf"

    doc = pymupdf.open()
    # 前 2 页:文字层(保留原始文字层)
    t = pymupdf.open(text_pdf)
    doc.insert_pdf(t, from_page=0, to_page=1)   # 第 1-2 页
    # 第 3 页:扫描图(无文字层)
    s = pymupdf.open(scan_pdf)
    doc.insert_pdf(s, from_page=0, to_page=0)   # 扫描版第 1 页

    doc.save(OUT)
    print(f"OK: {OUT} ({doc.page_count} 页)")


if __name__ == "__main__":
    main()
