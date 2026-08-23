"""PDF 类型检测器(idea.md §3 / Phase 6)。

依据:每页文字层字符数(去空白),不依赖文件大小。

- text:    几乎所有页面都有可靠文字层
- scanned: 基本没有有效文字层(需 OCR)
- hybrid:  部分页面有文字层,部分为扫描内容

阈值可通过构造参数调整。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import pymupdf


class PDFType(str, Enum):
    TEXT = "text"
    SCANNED = "scanned"
    HYBRID = "hybrid"


@dataclass
class DetectionResult:
    pdf_type: PDFType
    page_char_counts: list[int] = field(default_factory=list)
    text_pages: int = 0
    total_pages: int = 0
    text_ratio: float = 0.0

    @property
    def scanned_pages(self) -> int:
        return self.total_pages - self.text_pages

    def summary(self) -> str:
        return (
            f"type={self.pdf_type.value}, text_ratio={self.text_ratio:.0%} "
            f"({self.text_pages}/{self.total_pages} 页有文字层)"
        )


class PDFDetector:
    def __init__(
        self,
        text_char_threshold: int = 50,
        text_ratio_high: float = 0.9,
        text_ratio_low: float = 0.1,
    ) -> None:
        """阈值:
        - text_char_threshold:页字符数(去空白)≥ 此值视为有可靠文字层
        - text_ratio_high:文字页占比 ≥ 此值 → text
        - text_ratio_low: 文字页占比 ≤ 此值 → scanned
        """
        self.text_char_threshold = text_char_threshold
        self.text_ratio_high = text_ratio_high
        self.text_ratio_low = text_ratio_low

    def page_char_counts(self, pdf_path: str | Path) -> list[int]:
        doc = pymupdf.open(pdf_path)
        counts = []
        for page in doc:
            text = page.get_text("text")
            counts.append(len(re.sub(r"\s+", "", text)))
        return counts

    def detect(self, pdf_path: str | Path) -> DetectionResult:
        counts = self.page_char_counts(pdf_path)
        total = len(counts)
        if total == 0:
            raise ValueError(f"PDF 无页面: {pdf_path}")

        text_pages = sum(1 for n in counts if n >= self.text_char_threshold)
        ratio = text_pages / total

        if ratio >= self.text_ratio_high:
            pdf_type = PDFType.TEXT
        elif ratio <= self.text_ratio_low:
            pdf_type = PDFType.SCANNED
        else:
            pdf_type = PDFType.HYBRID

        return DetectionResult(
            pdf_type=pdf_type,
            page_char_counts=counts,
            text_pages=text_pages,
            total_pages=total,
            text_ratio=ratio,
        )
