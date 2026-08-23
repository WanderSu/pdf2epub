"""Phase 3 端到端测试:电子 PDF → Markdown → EPUB → 验证。

对 samples/pdfs/ 下每个测试 PDF:
  1. PyMuPDF4LLM → work/<name>/book.md + images/
  2. Pandoc → output/<name>.epub(--toc --toc-depth=3 --css=config/book.css --mathml)
  3. verify_epub.py 结构验证
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from backends.pymupdf_backend import convert_pdf_to_markdown  # noqa: E402

# (文件名, 标题, 验证期望参数列表)
PDFS = [
    ("中文电子书测试", "中文电子书排版测试", ["--expect-images", "2", "--expect-footnotes"]),
    ("双栏测试", "双栏排版测试", []),
    ("公式测试", "数学公式测试文档", ["--expect-images", "4", "--expect-math"]),
]


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    print("  $ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def main() -> int:
    overall = 0
    for name, title, expects in PDFS:
        print(f"\n===== {name} =====")
        pdf = ROOT / "samples" / "pdfs" / f"{name}.pdf"
        work = ROOT / "work" / name

        # 1. 提取
        book_md = convert_pdf_to_markdown(pdf, work)
        print(f"[1] 提取完成: {book_md}")
        md_text = book_md.read_text(encoding="utf-8")
        img_count = md_text.count("![](")
        print(f"    字符数 {len(md_text)}, 图片引用 {img_count} 个")

        # 2. 转换 EPUB
        epub = ROOT / "output" / f"{name}.epub"
        res = run([
            "pandoc", str(book_md), "-o", str(epub),
            "--toc", "--toc-depth=3",
            "--css", str(ROOT / "config" / "book.css"),
            "--resource-path", str(work),
            "--mathml",
            "--metadata", f"title={title}",
            "--metadata", "lang=zh-CN",
        ])
        if res.returncode != 0:
            print(f"[2] Pandoc 失败:\n{res.stderr}")
            overall = 1
            continue
        print(f"[2] EPUB: {epub} ({epub.stat().st_size} 字节)")

        # 3. 验证
        res = run([sys.executable, str(ROOT / "scripts" / "verify_epub.py"), str(epub), *expects])
        print(res.stdout)
        if res.returncode != 0:
            overall = 1

    print(f"\n=== Phase 3 端到端: {'全部通过' if overall == 0 else '存在问题'} ===")
    return overall


if __name__ == "__main__":
    sys.exit(main())
