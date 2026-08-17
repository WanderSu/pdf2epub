"""Pandoc EPUB 生成封装(idea.md §10/§11)。

统一命令:
    pandoc book.md -o out.epub --toc --toc-depth=3 --css=config/book.css
           --resource-path=<work> --mathml --metadata title/author/lang
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from paths import book_css

DEFAULT_CSS = book_css()


def infer_title(book_md: Path, fallback: str | None = None) -> str:
    """从 Markdown 标题推断书名:跳过过短/无意义的候选(如版权页'说明')。"""
    try:
        text = book_md.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"^#\s+(.+)$", text, re.MULTILINE):
            candidate = m.group(1).strip()
            condensed = re.sub(r"\s+", "", candidate)
            if len(condensed) >= 3:
                return candidate
    except OSError:
        pass
    return fallback or book_md.parent.name


def build_epub(
    book_md: str | Path,
    work_dir: str | Path,
    output_dir: str | Path,
    title: str | None = None,
    author: str | None = None,
    css: str | Path = DEFAULT_CSS,
) -> Path:
    """用 Pandoc 将 work/book.md 转为 EPUB,返回 epub 路径。

    Raises:
        RuntimeError: Pandoc 执行失败
    """
    book_md = Path(book_md)
    work_dir = Path(work_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    epub = output_dir / f"{book_md.parent.name}.epub"
    title = title or infer_title(book_md)

    cmd = [
        "pandoc", str(book_md), "-o", str(epub),
        "--toc", "--toc-depth=3",
        "--css", str(css),
        "--resource-path", str(work_dir),
        "--mathml",
        "--metadata", f"title={title}",
        "--metadata", "lang=zh-CN",
    ]
    if author:
        cmd += ["--metadata", f"author={author}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"Pandoc 失败(exit={proc.returncode}): {proc.stderr[:500]}")
    return epub
