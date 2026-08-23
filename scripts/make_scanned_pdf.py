"""生成扫描版测试 PDF:把源 PDF 每页渲染为图像,合成无文字层的纯图 PDF。

模拟真实扫描书(只有图像、没有文字层),用于 Phase 4 云端 OCR 测试。
"""
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "samples" / "pdfs" / "中文电子书测试.pdf"
OUT = ROOT / "samples" / "pdfs" / "扫描版测试.pdf"
DPI = 150


def main() -> None:
    src = pymupdf.open(SRC)
    out = pymupdf.open()

    for page in src:
        pix = page.get_pixmap(dpi=DPI)
        png_bytes = pix.tobytes("png")
        rect = page.rect
        new_page = out.new_page(width=rect.width, height=rect.height)
        new_page.insert_image(new_page.rect, stream=png_bytes)

    out.save(OUT)
    print(f"OK: {OUT} ({out.page_count} 页, DPI={DPI})")

    # 验证:扫描版应几乎无文字层
    check = pymupdf.open(OUT)
    texts = [p.get_text().strip() for p in check]
    total = sum(len(t) for t in texts)
    print(f"文字层字符数: {total}(应为 0 或极少)")


if __name__ == "__main__":
    main()
