"""批处理与端到端管线测试(计划 1.2)。

样本现场生成(不依赖仓库里的真实书籍),因此任何机器上 `uv run pytest -q` 都应通过。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import batch
from batch import (
    VerifyError,
    epub_path,
    existing_epub,
    output_stem,
    parse_title_author,
    process_batch,
    process_one,
    sanitize_name,
    verify_output,
)
from conftest import make_pdf, rewrite_epub

from epub.verify import verify_epub

CFG = {"pymupdf": {"write_images": True, "bold_fonts": []}}


# ---------------------------------------------------------------- 纯函数

def test_sanitize_name_replaces_spaces() -> None:
    assert sanitize_name("资本论 - 马克思") == "资本论_-_马克思"
    assert sanitize_name("  a/b:c  ") == "a_b_c"


def test_output_stem_keeps_spaces() -> None:
    assert output_stem("资本论 - 马克思") == "资本论 - 马克思"
    assert output_stem('坏:名*字') == "坏_名_字"


def test_epub_path_keeps_spaces(tmp_path: Path) -> None:
    assert epub_path(Path("资本论 - 马克思.pdf"), tmp_path).name == "资本论 - 马克思.epub"


def test_existing_epub_accepts_legacy_underscore_name(tmp_path: Path) -> None:
    legacy = tmp_path / "资本论_-_马克思.epub"
    legacy.write_bytes(b"x")
    assert existing_epub(Path("资本论 - 马克思.pdf"), tmp_path) == legacy


@pytest.mark.parametrize("stem,expected", [
    ("资本论 - 马克思", ("资本论", "马克思")),
    ("没有作者", ("没有作者", None)),
    ("Ab - Bc - Cd", ("Ab", "Bc - Cd")),
    ("A - 短标题", ("A - 短标题", None)),      # 标题过短 → 不按「标题 - 作者」解析
])
def test_parse_title_author(stem: str, expected: tuple[str, str | None]) -> None:
    assert parse_title_author(stem) == expected


# ---------------------------------------------------------------- 端到端

def test_pdf_to_epub_end_to_end(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "src" / "测试书.pdf", pages=2)
    results = process_batch(
        [pdf], config=dict(CFG),
        work_root=tmp_path / "work", output_dir=tmp_path / "out",
        force=True,
    )
    assert [r.status for r in results] == ["done"]
    epub = results[0].epub
    assert epub is not None and epub.exists()
    assert epub.name == "测试书.epub"          # 输出名不 sanitize
    assert (tmp_path / "work" / "测试书").is_dir()   # 工作目录存在
    assert results[0].verify.startswith("0 失败")
    assert verify_epub(epub).ok


def test_second_run_is_skipped(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "src" / "跳过测试.pdf", pages=1)
    kwargs = dict(config=dict(CFG), work_root=tmp_path / "work", output_dir=tmp_path / "out")
    assert process_batch([pdf], force=True, **kwargs)[0].status == "done"
    assert process_batch([pdf], **kwargs)[0].status == "skipped"


def test_markdown_input_pipeline(tmp_path: Path) -> None:
    md = tmp_path / "src" / "手记.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("# 手记\n\n正文段落,句号结尾。\n", encoding="utf-8")
    results = process_batch(
        [md], config=dict(CFG),
        work_root=tmp_path / "work", output_dir=tmp_path / "out", force=True,
    )
    assert results[0].status == "done"
    assert results[0].backend == "markdown"
    assert results[0].epub is not None and results[0].epub.exists()


# ---------------------------------------------------------------- 校验门禁

def test_verify_output_reports_and_strict_raises(sample_epub: Path, tmp_path: Path) -> None:
    broken = rewrite_epub(sample_epub, tmp_path / "broken.epub", drop_suffixes=(".png",))
    result = verify_output(broken)                      # 默认只告警
    assert result.ok is False
    with pytest.raises(VerifyError, match="EPUB 校验未通过"):
        verify_output(broken, strict=True)


def test_verify_error_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """校验失败是确定性失败:必须一次即止(重试会重复消耗云端 OCR 额度)。"""
    pdf = make_pdf(tmp_path / "src" / "不重试.pdf", pages=1)
    calls = {"n": 0}

    def fake(*args, **kwargs):
        calls["n"] += 1
        raise VerifyError("模拟结构校验失败")

    monkeypatch.setattr(batch, "_process_pdf", fake)
    result = process_one(pdf, config=dict(CFG), work_root=tmp_path / "work",
                         output_dir=tmp_path / "out", retries=3)
    assert result.status == "failed"
    assert calls["n"] == 1
    assert "模拟结构校验失败" in result.error


def test_other_errors_are_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = make_pdf(tmp_path / "src" / "会重试.pdf", pages=1)
    calls = {"n": 0}

    def fake(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("临时故障")

    monkeypatch.setattr(batch, "_process_pdf", fake)
    monkeypatch.setattr(batch.time, "sleep", lambda *_: None)
    result = process_one(pdf, config=dict(CFG), work_root=tmp_path / "work",
                         output_dir=tmp_path / "out", retries=2)
    assert result.status == "failed"
    assert calls["n"] == 3      # 首次 + 2 次重试
