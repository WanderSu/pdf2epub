"""预检(--dry-run)测试(计划 1.6)。

预检必须真的「只读」:不建 work//output 目录、不写日志文件、不调用任何后端。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import cli
from conftest import make_pdf

from detector.pdf_detector import DetectionResult, PDFType
from dryrun import (
    DAILY_OCR_PAGE_QUOTA,
    PlanItem,
    estimate_shards,
    max_pages_per_task,
    plan,
    plan_source,
    render,
)


class FakeDetector:
    """伪造检测结果:避免为「450 页扫描书」真的造一个大 PDF。"""

    def __init__(self, pdf_type: PDFType, total: int, text_pages: int = 0,
                 suspicious: int = 0) -> None:
        self.result = DetectionResult(
            pdf_type=pdf_type,
            total_pages=total,
            text_pages=text_pages,
            text_ratio=text_pages / total if total else 0.0,
            suspicious_pages=suspicious,
            text_page_idxs=list(range(text_pages)),
        )

    def detect(self, pdf_path) -> DetectionResult:   # noqa: ARG002
        return self.result


CFG = {"ocr_backend": "mineru", "mineru": {"max_pages_per_task": 200}}


def test_estimate_shards() -> None:
    assert estimate_shards(0, 200) == 0
    assert estimate_shards(1, 200) == 1
    assert estimate_shards(200, 200) == 1
    assert estimate_shards(450, 200) == 3
    assert estimate_shards(401, 200) == 3
    assert estimate_shards(50, 0) == 1        # 非法上限 → 退回默认值


def test_max_pages_per_task_fallback() -> None:
    assert max_pages_per_task(CFG, "mineru") == 200
    assert max_pages_per_task({}, "mineru") == 200
    assert max_pages_per_task({"mineru": {"max_pages_per_task": "50"}}, "mineru") == 50
    assert max_pages_per_task({"mineru": {"max_pages_per_task": "bad"}}, "mineru") == 200


def test_plan_scanned_book_shards_and_quota(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "扫描版.pdf", pages=1, with_image=False)
    det = FakeDetector(PDFType.SCANNED, total=450, text_pages=0)
    item = plan_source(pdf, CFG, detector=det)
    assert item.kind == "pdf-scanned"
    assert item.backend == "mineru"
    assert item.ocr_pages == 450
    assert item.shards == 3

    report = render([item], CFG)
    assert "预计分片 3 段" in report
    assert "450 页 / 当日额度 1000 页" in report


def test_render_warns_over_quota() -> None:
    items = [PlanItem(source=Path("大书.pdf"), kind="pdf-scanned", backend="mineru",
                      pages=1200, ocr_pages=1200, shards=6)]
    report = render(items, CFG)
    assert "超出当日额度" in report
    assert "建议分批转换" in report
    assert DAILY_OCR_PAGE_QUOTA == 1000


def test_text_pdf_needs_no_ocr(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "文字版.pdf", pages=1, with_image=False)
    det = FakeDetector(PDFType.TEXT, total=10, text_pages=10)
    item = plan_source(pdf, CFG, detector=det)
    assert item.backend == "pymupdf"
    assert item.ocr_pages == 0
    assert item.shards == 0


def test_hybrid_counts_only_scanned_pages(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "混合.pdf", pages=1, with_image=False)
    det = FakeDetector(PDFType.HYBRID, total=100, text_pages=60)
    item = plan_source(pdf, CFG, detector=det)
    assert item.kind == "pdf-hybrid"
    assert item.backend == "hybrid(mineru)"
    assert item.ocr_pages == 40
    assert item.shards == 1


def test_backend_override_marks_ocr(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "文字版.pdf", pages=1, with_image=False)
    det = FakeDetector(PDFType.TEXT, total=500, text_pages=500)
    item = plan_source(pdf, CFG, backend_override="paddleocr", detector=det)
    assert item.backend == "paddleocr"
    assert item.ocr_pages == 500
    assert item.shards == 3


def test_markdown_plan(tmp_path: Path) -> None:
    md = tmp_path / "手记.md"
    md.write_text("# 标题\n", encoding="utf-8")
    item = plan_source(md, CFG)
    assert item.kind == "markdown"
    assert item.backend == "markdown"
    assert not item.needs_ocr


def test_plan_expands_a_directory(tmp_path: Path) -> None:
    make_pdf(tmp_path / "a.pdf", pages=1, with_image=False)
    (tmp_path / "b.md").write_text("# b\n", encoding="utf-8")
    items = plan([tmp_path], CFG, detector=FakeDetector(PDFType.TEXT, 1, 1))
    assert sorted(i.source.suffix for i in items) == [".md", ".pdf"]


# ---------------------------------------------------------------- CLI 集成

def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "book.pdf", pages=1, with_image=False)
    out = tmp_path / "out"
    work = tmp_path / "work"
    logs = tmp_path / "logs"
    code = cli.main([str(pdf), "--dry-run", "-o", str(out), "--work", str(work),
                     "--log", str(logs / "x.log")])
    assert code == 0
    assert not out.exists()
    assert not work.exists()
    assert not logs.exists()


def test_dry_run_supports_markdown(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    md = tmp_path / "note.md"
    md.write_text("# 标题\n正文,句号结尾。\n", encoding="utf-8")
    assert cli.main([str(md), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "预检" in out and "markdown" in out
