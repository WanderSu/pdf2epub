"""Phase 4 端到端测试:MinerU Cloud OCR → Markdown → EPUB → 验证。

用法:
    python scripts/test_phase4_e2e.py [pdf 路径]
默认测试 samples/pdfs/扫描版测试.pdf。
需要环境变量 MINERU_API_TOKEN(不在命令行传入,避免泄露)。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from backends import get_backend  # noqa: E402


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    print("  $ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def main() -> int:
    pdf = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "samples" / "pdfs" / "扫描版测试.pdf"
    name = pdf.stem
    work = ROOT / "work" / name
    epub = ROOT / "output" / f"{name}.epub"

    print(f"===== Phase 4: {name} =====")
    backend = get_backend("mineru")  # 从环境变量读 token

    # 1. 云端 OCR → 统一 Markdown
    result = backend.convert(pdf, work)
    print(f"[1] OCR 完成: {result.book_md}")
    print(f"    stats: {result.stats}")
    md_text = result.book_md.read_text(encoding="utf-8")
    print(f"    字符数 {len(md_text)}, 图片引用 {md_text.count('![](')} 个")

    # 2. Pandoc → EPUB
    res = run([
        "pandoc", str(result.book_md), "-o", str(epub),
        "--toc", "--toc-depth=3",
        "--css", str(ROOT / "config" / "book.css"),
        "--resource-path", str(work),
        "--mathml",
        "--metadata", f"title={name}",
        "--metadata", "lang=zh-CN",
    ])
    if res.returncode != 0:
        print(f"[2] Pandoc 失败:\n{res.stderr}")
        return 1
    print(f"[2] EPUB: {epub} ({epub.stat().st_size} 字节)")

    # 3. 验证(期望 ≥2 张图;OCR 文本无公式/脚注语义,不设期望)
    res = run([sys.executable, str(ROOT / "scripts" / "verify_epub.py"), str(epub), "--expect-images", "2"])
    print(res.stdout)
    return res.returncode


if __name__ == "__main__":
    sys.exit(main())
