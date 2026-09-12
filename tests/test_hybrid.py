"""hybrid 页序回归测试(P0-1 Hybrid PageResult 架构)。

被钉住的旧缺陷:`text→scan→text` 交错的书,原实现把「全部扫描页」整块拼到
最前面(按各组首页页码排序),页序错位、页边界丢失 —— 输出「看着正常、
内容其实错序」。

样本现场生成(见 conftest.make_mixed_pdf),OCR 用假后端(monkeypatch),
全程离线、不消耗云端额度。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import convert
from backends.base import ConversionResult
from conftest import make_mixed_pdf, write_png
from detector.pdf_detector import PDFDetector, PDFType
from markdown.cleaner import clean_markdown
from page_result import (
    PageResult,
    contiguous_runs,
    format_pages,
    merge_page_results,
    merge_runs,
    page_marker_from_range,
    page_marks,
    parse_pages,
    plan_ocr_runs,
    resolve_ocr_run_limit,
)

#: 前 3 页文字、第 4-5 页扫描、后 3 页文字 —— 计划里点名的回归样本
LAYOUT = "TTTSSTTT"

CFG = {
    "ocr_backend": "mineru",
    "pymupdf": {"write_images": True, "bold_fonts": []},
    "mineru": {"max_pages_per_task": 200},
}


class FakeOCR:
    """假云端 OCR:按收到的纯图 PDF 页数产出 Markdown + 一张同名图片。

    同名图片是为了覆盖「跨区段图片重名 → 加前缀并改引用」这条路径;
    calls 记录每次收到的临时 PDF 名与页数,用来断言区段切分。
    """

    name = "fake-ocr"

    def __init__(self, image_name: str = "ocr_img.png") -> None:
        self.image_name = image_name
        self.calls: list[tuple[str, int]] = []

    def convert(self, pdf_path: str | Path, work_dir: str | Path) -> ConversionResult:
        import pymupdf

        pdf_path = Path(pdf_path)
        work_dir = Path(work_dir)
        with pymupdf.open(pdf_path) as doc:
            pages = doc.page_count
        images = work_dir / "images"
        images.mkdir(parents=True, exist_ok=True)
        write_png(images / self.image_name)

        body = [
            f"# OCR 区段第 {i} 页\n\nOCR 识别出来的正文段落,以句号结尾。"
            for i in range(1, pages + 1)
        ]
        md = "\n\n".join(body) + f"\n\n![插图](images/{self.image_name})\n"
        book_md = work_dir / "book.md"
        book_md.write_text(md, encoding="utf-8")
        self.calls.append((pdf_path.name, pages))
        return ConversionResult(
            book_md=book_md, images_dir=images, backend=self.name, stats={"chars": len(md)}
        )


def _convert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
             layout: str = LAYOUT, config: dict | None = None, fake: FakeOCR | None = None):
    """跑一次 hybrid 转换:文字页走真实本地后端,OCR 走假后端。"""
    pdf = make_mixed_pdf(tmp_path / "混合书.pdf", layout)
    fake = fake or FakeOCR()
    real_get_backend = convert.get_backend

    def fake_get_backend(name: str, cfg: dict):
        return real_get_backend("pymupdf", cfg) if name == "pymupdf" else fake

    monkeypatch.setattr(convert, "get_backend", fake_get_backend)
    cfg = dict(config or CFG)
    result, detection = convert.convert_auto(pdf, tmp_path / "work", config=cfg)
    return result, detection, fake


def _flatten(marks: list[list[int]]) -> list[int]:
    return [p for group in marks for p in group]


def _flat(md: str) -> str:
    """去掉所有空白后的文本:PyMuPDF4LLM 会在句子中间插换行,子串定位要先压平。"""
    return re.sub(r"\s+", "", md)


# ---------------------------------------------------------------- 页序(核心回归)

def test_text_scan_text_page_order_is_preserved(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    result, detection, fake = _convert(tmp_path, monkeypatch)
    assert detection.pdf_type == PDFType.HYBRID

    md = result.book_md.read_text(encoding="utf-8")
    marks = page_marks(md)
    assert marks == [[1], [2], [3], [4, 5], [6], [7], [8]]
    assert _flatten(marks) == sorted(_flatten(marks))          # 页码注释单调递增

    # 扫描区段必须落在第 3 页之后、第 6 页之前(旧实现在这里会把扫描块提到最前)
    flat = _flat(md)
    assert flat.index("第3页的正文段落") < flat.index("OCR区段第1页")
    assert flat.index("OCR区段第1页") < flat.index("第6章测试标题")

    # 4-5 页是**一个**连续区段 → 一次 OCR,收到 2 页
    assert fake.calls == [("_hybrid_scan_p4.pdf", 2)]
    assert result.stats["ocr_runs"] == 1
    assert result.stats["text_pages"] == 6 and result.stats["scanned_pages"] == 2


def test_alternating_pages_produce_one_run_per_scan_block(tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """文字/扫描交替时:每个扫描块单独一次 OCR,仍按原文页序排列。"""
    _, _, fake = _convert(tmp_path, monkeypatch, layout="TTSTTSTT")
    md = (tmp_path / "work" / "book.md").read_text(encoding="utf-8")
    assert page_marks(md) == [[1], [2], [3], [4], [5], [6], [7], [8]]
    assert fake.calls == [("_hybrid_scan_p3.pdf", 1), ("_hybrid_scan_p6.pdf", 1)]


def test_ocr_runs_are_capped_and_warn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    """区段数超上限 → 合并成少数几个云端任务,并且明确警告(不静默)。"""
    cfg = {**CFG, "hybrid": {"max_ocr_runs": 2}}
    _, _, fake = _convert(tmp_path, monkeypatch, layout="TSTSTSTS", config=cfg)
    md = (tmp_path / "work" / "book.md").read_text(encoding="utf-8")

    assert len(fake.calls) == 2                    # 4 个扫描页 → 2 个任务
    assert [pages for _, pages in fake.calls] == [2, 2]
    # 合并区段(2,4 / 6,8)整体排在自己首页的位置,中间的文字页(3 / 7)因此被
    # 推到它后面 —— 这正是「合并会让局部页序与原文不一致」的那个代价,已警告。
    assert _flatten(page_marks(md)) == [1, 2, 4, 3, 5, 6, 8, 7]
    assert "超过上限" in capsys.readouterr().out


# ---------------------------------------------------------------- 图片与清理链

def test_ocr_images_merge_into_images_dir_and_refs_are_valid(tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    result, _, _ = _convert(tmp_path, monkeypatch, layout="TTSTTSTT")
    work = result.images_dir.parent

    images = sorted(p.name for p in (work / "images").iterdir())
    # 两段 OCR 各产出同名图 → 第二张加 scan_p6_ 前缀
    assert images == ["ocr_img.png", "scan_p6_ocr_img.png"]

    md = result.book_md.read_text(encoding="utf-8")
    refs = [line.split("](", 1)[1].rstrip(")") for line in md.splitlines() if line.startswith("![")]
    assert refs == ["images/ocr_img.png", "images/scan_p6_ocr_img.png"]
    assert all((work / ref).exists() for ref in refs)

    # 清理阶段(清理管线是 hybrid 与纯文字版共用的那一条)不得报图片缺失
    from markdown.cleaner import CleanReport

    report = CleanReport()
    clean_markdown(md, images_dir=work / "images", report=report)
    assert [i for i in report.issues if "图片引用缺失" in i] == []


# ---------------------------------------------------------------- 页单元合并(纯函数)

def test_merge_page_results_sorts_by_page_and_marks_pages() -> None:
    merged = merge_page_results([
        PageResult(pages=(6,), source="text", markdown="# 六"),
        PageResult(pages=(4, 5), source="ocr", markdown="# 扫描段"),
        PageResult(pages=(1,), source="text", markdown="  "),      # 空单元跳过
    ])
    assert page_marks(merged) == [[4, 5], [6]]
    assert merged.index("扫描段") < merged.index("六")


def test_merge_page_results_keeps_sparse_pages_honest() -> None:
    """合并区段是非连续页集合:注释必须如实写 ``2,4``,不能谎称 ``2-4``。"""
    merged = merge_page_results([PageResult(pages=(2, 4), source="ocr", markdown="X")])
    assert page_marks(merged) == [[2, 4]]
    assert "page 2,4 ocr" in merged


def test_format_and_parse_pages_are_inverse() -> None:
    assert format_pages([4, 5, 6, 9]) == "4-6,9"
    assert format_pages([3]) == "3"
    assert format_pages([]) == ""
    assert parse_pages("4-6,9") == [4, 5, 6, 9]
    assert parse_pages("7") == [7]
    assert parse_pages("bad") == []
    assert page_marker_from_range("201-302") == "<!-- page 201-302 -->"
    assert page_marker_from_range("坏值") == "<!-- page-group 坏值 -->"


def test_contiguous_runs_and_merge_runs() -> None:
    assert contiguous_runs([0, 1, 2, 4, 5]) == [[0, 1, 2], [4, 5]]
    assert contiguous_runs([5, 3, 3]) == [[3], [5]]                # 去重 + 升序
    assert contiguous_runs([]) == []

    runs = [[1], [3], [5], [7]]
    merged = merge_runs(runs, 2)
    assert merged == [[1, 3], [5, 7]]                              # 按顺序均分
    assert merge_runs(runs, 4) == runs                             # 未超限则不动
    assert merge_runs(runs, 3) == [[1], [3], [5, 7]]                # 多出的区段落在末组
    assert sorted(p for r in merged for p in r) == [1, 3, 5, 7]    # 页不丢


def test_plan_ocr_runs_respects_limit_and_config_coercion() -> None:
    assert plan_ocr_runs([1, 3, 5], limit=8) == [[1], [3], [5]]
    assert len(plan_ocr_runs([1, 3, 5], limit=1)) == 1
    assert resolve_ocr_run_limit(None) == 8
    assert resolve_ocr_run_limit("4") == 4
    assert resolve_ocr_run_limit(0) == 8                           # 非正 → 默认
    assert resolve_ocr_run_limit("bad") == 8


# ---------------------------------------------------------------- 预检同步

def test_dry_run_reports_hybrid_ocr_runs(tmp_path: Path) -> None:
    """预检报的云端任务数必须与实际提交的区段数一致。"""
    from dryrun import plan_source

    pdf = make_mixed_pdf(tmp_path / "混合书.pdf", "TTSTTSTT")
    detection = PDFDetector().detect(pdf)
    assert detection.pdf_type == PDFType.HYBRID

    item = plan_source(pdf, CFG)
    assert item.ocr_pages == 2
    assert item.shards == 2                                        # 两个扫描块 → 两个任务
    assert any("连续区段" in note for note in item.notes)

    merged_item = plan_source(pdf, {**CFG, "hybrid": {"max_ocr_runs": 1}})
    assert merged_item.shards == 1
