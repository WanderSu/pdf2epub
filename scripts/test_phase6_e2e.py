"""Phase 6 端到端测试:自动检测 + backend 自动选择 → EPUB → 验证。

样本:
- 中文电子书测试.pdf → text → PyMuPDF4LLM(本地)
- 混合型测试.pdf    → hybrid → 页级路由(本地 + MinerU OCR)
- (扫描版已在 Phase 4/5 验证,此处可选)

hybrid 样本需要 MINERU_API_TOKEN。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from convert import convert_auto  # noqa: E402

CONFIG = ROOT / "config" / "config.yaml"


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    print("  $ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def build_epub(book_md: Path, work: Path, title: str) -> int:
    epub = ROOT / "output" / f"{work.name}.epub"
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
        print(f"Pandoc 失败:\n{res.stderr}")
        return 1
    print(f"EPUB: {epub} ({epub.stat().st_size} 字节)")
    res = run([sys.executable, str(ROOT / "scripts" / "verify_epub.py"), str(epub)])
    print(res.stdout)
    return res.returncode


def main() -> int:
    overall = 0

    # 1. text 样本:自动选 pymupdf
    print("===== text 样本:中文电子书测试 =====")
    result, detection = convert_auto(
        ROOT / "samples" / "pdfs" / "中文电子书测试.pdf",
        ROOT / "work" / "自动检测_text",
        config_path=CONFIG,
    )
    print(f"[1] backend={result.backend} stats={result.stats}")
    overall |= build_epub(result.book_md, result.book_md.parent, "自动检测-text")

    # 2. hybrid 样本:页级路由(需要 MINERU_API_TOKEN)
    print("\n===== hybrid 样本:混合型测试 =====")
    result, detection = convert_auto(
        ROOT / "samples" / "pdfs" / "混合型测试.pdf",
        ROOT / "work" / "自动检测_hybrid",
        config_path=CONFIG,
    )
    print(f"[1] backend={result.backend} stats={result.stats}")
    overall |= build_epub(result.book_md, result.book_md.parent, "自动检测-hybrid")

    print(f"\n=== Phase 6 端到端: {'全部通过' if overall == 0 else '存在问题'} ===")
    return overall


if __name__ == "__main__":
    sys.exit(main())
