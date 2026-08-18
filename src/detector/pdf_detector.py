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

# 可疑字符:私有区/替换符/框线绘图/几何形状/杂项符号/CJK 扩展区等 ——
# 文字层损坏(伪文字层)的特征。正常中文正文中这些字符占比极低
# (CJK 扩展区字符在正常正文中几乎不出现)。
# 注意:私有区 B 与扩展 B+ 需用 \U 八位转义;PUA-A 为 \ue000-\uf8ff
SUSPICIOUS_RE = re.compile(
    "[\ue000-\uf8ff"                    # Unicode 私有区 A
    "\U00010000-\U0010ffff"             # Unicode 私有区 B
    "\ufffd"                            # 替换字符
    "\u2e00-\u2e7f"                     # 补充标点区
    "\u2190-\u21ff"                     # 箭头
    "\u2500-\u257f"                     # 框线绘图
    "\u25a0-\u25ff"                     # 几何形状
    "\u2600-\u26ff"                     # 杂项符号
    "\u2700-\u27bf"                     # 装饰符号
    "\u2b00-\u2bff"                     # 杂项符号与箭头
    "\u3400-\u4dbf"                     # CJK 扩展 A(正常正文罕见)
    "\U00020000-\U0002ebef"             # CJK 扩展 B-F
    "]"
)


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
    suspicious_pages: int = 0   # 疑似文字层损坏(乱码)页数,供用户手动判断是否 OCR
    text_page_idxs: list[int] = field(default_factory=list)  # 可靠文字层页(0-indexed)

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
        suspicious_ratio_limit: float = 0.25,
    ) -> None:
        """阈值:
        - text_char_threshold:页字符数(去空白)≥ 此值视为有可靠文字层
        - text_ratio_high:文字页占比 ≥ 此值 → text
        - text_ratio_low: 文字页占比 ≤ 此值 → scanned
        - suspicious_ratio_limit:页内可疑(乱码)字符占比上限,
          超过则视为伪文字层(等同扫描页)
        """
        self.text_char_threshold = text_char_threshold
        self.text_ratio_high = text_ratio_high
        self.text_ratio_low = text_ratio_low
        self.suspicious_ratio_limit = suspicious_ratio_limit

    def page_valid_char_counts(self, pdf_path: str | Path) -> list[int]:
        """每页有效字符数:剔除空白与可疑乱码字符。
        部分 PDF 文字层损坏(嵌入字体无正确 ToUnicode 映射),提取出
        私有区乱码但渲染正常 —— 这种页面等同扫描页,应进入 OCR。
        """
        doc = pymupdf.open(pdf_path)
        counts = []
        for page in doc:
            text = page.get_text("text")
            compact = re.sub(r"\s+", "", text)
            bad = len(SUSPICIOUS_RE.findall(compact))
            counts.append(max(0, len(compact) - bad))
        return counts

    def detect(self, pdf_path: str | Path) -> DetectionResult:
        counts = self.page_valid_char_counts(pdf_path)
        doc = pymupdf.open(pdf_path)
        total = len(counts)
        if total == 0:
            raise ValueError(f"PDF 无页面: {pdf_path}")

        # 乱码率:每页可疑字符占比,超过上限的页即使有效字符达标也视为扫描页
        text_pages = 0
        suspicious_pages = 0
        text_page_idxs: list[int] = []
        for i, page in enumerate(doc):
            compact = re.sub(r"\s+", "", page.get_text("text"))
            if not compact:
                continue
            bad = len(SUSPICIOUS_RE.findall(compact))
            ratio = bad / len(compact)
            if ratio > self.suspicious_ratio_limit:
                suspicious_pages += 1
            if counts[i] >= self.text_char_threshold and ratio <= self.suspicious_ratio_limit:
                text_pages += 1
                text_page_idxs.append(i)
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
            suspicious_pages=suspicious_pages,
            text_page_idxs=text_page_idxs,
        )
