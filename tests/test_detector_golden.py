"""检测器 golden 样本回归(v0.3.2 P0-1)。

样本现场生成(不依赖仓库里的真实书籍),期望指标在 `GOLDEN` 表里冻结:
类型 / 页数 / 文字页数 / text_ratio / 可疑页数 / 文字页索引。

两条硬规矩:

1. **不要为了让样本变绿而放宽阈值**(阈值改了就改这张表 + 说明原因)。
2. 本地链路测试用的样本(见 `make_pdf`)必须是 text 型 —— 一旦样本被判扫描版,
   测试就会去调云端 OCR:慢、消耗额度、没凭证的机器直接红。这条由 `test_local_samples_are_text` 守住。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_fake_text_layer_pdf, make_mixed_pdf, make_pdf
from detector.pdf_detector import PDFDetector, PDFType

GOLDEN: list[dict] = [
    {
        "name": "纯文字版",
        "build": lambda p: make_pdf(p, pages=3, with_image=False),
        "type": PDFType.TEXT, "total": 3, "text_pages": 3,
        "ratio": 1.0, "suspicious": 0, "idxs": [0, 1, 2],
    },
    {
        "name": "纯扫描版(只有图,没有文字层)",
        "build": lambda p: make_mixed_pdf(p, "SS"),
        "type": PDFType.SCANNED, "total": 2, "text_pages": 0,
        "ratio": 0.0, "suspicious": 0, "idxs": [],
    },
    {
        "name": "混合版(文字/扫描交错)",
        "build": lambda p: make_mixed_pdf(p, "TTTSSTTT"),
        "type": PDFType.HYBRID, "total": 8, "text_pages": 6,
        "ratio": 0.75, "suspicious": 0, "idxs": [0, 1, 2, 5, 6, 7],
    },
    {
        "name": "伪文字层(部分页损坏):文字页 + 乱码页",
        "build": lambda p: make_fake_text_layer_pdf(p, text_pages=3, junk_pages=2),
        "type": PDFType.HYBRID, "total": 5, "text_pages": 3,
        "ratio": 0.6, "suspicious": 2, "idxs": [0, 1, 2],
    },
    {
        "name": "伪文字层(全篇损坏):等同扫描版,但可疑页数 > 0",
        "build": lambda p: make_fake_text_layer_pdf(p, text_pages=0, junk_pages=2),
        "type": PDFType.SCANNED, "total": 2, "text_pages": 0,
        "ratio": 0.0, "suspicious": 2, "idxs": [],
    },
]


@pytest.mark.parametrize("case", GOLDEN, ids=[c["name"] for c in GOLDEN])
def test_detector_golden(case: dict, tmp_path: Path) -> None:
    pdf = case["build"](tmp_path / "sample.pdf")
    result = PDFDetector().detect(pdf)

    assert result.pdf_type == case["type"]
    assert result.total_pages == case["total"]
    assert result.text_pages == case["text_pages"]
    assert result.text_ratio == pytest.approx(case["ratio"], abs=1e-6)
    assert result.suspicious_pages == case["suspicious"]
    assert result.text_page_idxs == case["idxs"]
    assert result.scanned_pages == case["total"] - case["text_pages"]


def test_local_samples_are_text(tmp_path: Path) -> None:
    """本地链路用的样本必须判为 text:否则测试会悄悄走云端 OCR(慢 + 耗额度)。"""
    for with_image in (False, True):
        pdf = make_pdf(tmp_path / f"local_{with_image}.pdf", pages=3, with_image=with_image)
        result = PDFDetector().detect(pdf)
        assert result.pdf_type == PDFType.TEXT, (
            f"make_pdf(with_image={with_image}) 被判为 {result.pdf_type.value};"
            "每页有效字符数必须 ≥ 检测阈值(50),否则本地测试会去调云端 OCR"
        )


def test_fake_layer_sample_triggers_interactive_prompt_condition(tmp_path: Path) -> None:
    """CLI 交互询问的条件:suspicious_pages > 0 且类型不是 scanned。

    伪文字层样本必须命中这个条件(所以它是 hybrid 而不是 scanned),否则
    「用户手动决定是否改用 OCR」这条链路就没有回归网。
    """
    pdf = make_fake_text_layer_pdf(tmp_path / "junk.pdf", text_pages=3, junk_pages=2)
    result = PDFDetector().detect(pdf)
    assert result.suspicious_pages > 0
    assert result.pdf_type != PDFType.SCANNED
