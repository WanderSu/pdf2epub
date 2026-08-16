"""Markdown 清理模块(idea.md §7)— 最小可用版。

原则:修复结构,不改写正文;不用 LLM 重写。
当前实现:
  - 统一换行符(CRLF → LF)
  - 多余空行压缩(连续 ≥3 个空行 → 1 个)
  - 行尾空白清理
  - 图片引用存在性校验(引用 images/xxx 但文件缺失 → 报告)
后续扩展(待办):页眉/页脚/页码剔除、OCR 异常空格、跨页断行、
重复/空标题、标题层级修正。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CleanReport:
    issues: list[str] = field(default_factory=list)

    def add(self, msg: str) -> None:
        self.issues.append(msg)


def clean_markdown(
    md_text: str,
    images_dir: Path | None = None,
    report: CleanReport | None = None,
) -> str:
    """清理 Markdown 文本,返回清理后的内容。"""
    report = report or CleanReport()

    # 1. 统一换行
    md = md_text.replace("\r\n", "\n").replace("\r", "\n")

    # 2. 压缩多余空行(3+ → 1)
    md = re.sub(r"\n{3,}", "\n\n", md)

    # 3. 行尾空白
    md = re.sub(r"[ \t]+$", "", md, flags=re.MULTILINE)

    # 4. 图片引用存在性校验
    if images_dir is not None and images_dir.is_dir():
        existing = {p.name for p in images_dir.iterdir() if p.is_file()}
        for m in re.finditer(r"!\[[^\]]*\]\(([^)\s]+)\)", md):
            ref = m.group(1)
            name = Path(ref).name
            if name not in existing and not Path(ref).is_absolute():
                report.add(f"图片引用缺失: {ref}")

    return md


def clean_file(book_md: str | Path) -> CleanReport:
    """就地清理 book.md 文件。"""
    book_md = Path(book_md)
    text = book_md.read_text(encoding="utf-8", errors="replace")
    report = CleanReport()
    cleaned = clean_markdown(text, images_dir=book_md.parent / "images", report=report)
    if cleaned != text:
        book_md.write_text(cleaned, encoding="utf-8")
    return report
