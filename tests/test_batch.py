"""批处理与端到端管线测试(计划 1.2)。

样本现场生成(不依赖仓库里的真实书籍),因此任何机器上 `uv run pytest -q` 都应通过。
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import batch
import events
from batch import (
    PandocMissingError,
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
from conftest import corrupt_zip_member, make_pdf, rewrite_epub, truncate_file

from epub import pandoc
from epub.verify import VerifyResult, verify_epub

CFG = {"pymupdf": {"write_images": True, "bold_fonts": []}}


def _raise_winerror2(*args, **kwargs):
    """Windows 上 pandoc 不在 PATH 时,subprocess 抛的正是这个。"""
    raise FileNotFoundError(2, "系统找不到指定的文件。")


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


def test_unretryable_backend_error_is_not_retried(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """后端显式标了 retryable=False 的失败(认证/参数/配额/云端判死)必须一次即止。

    这类错误重试改变不了结果,而扫描书每重试一次就可能重新上传、重新 OCR 一次。
    """
    from backends.mineru_backend import MinerUError

    pdf = make_pdf(tmp_path / "src" / "认证失败.pdf", pages=1)
    calls = {"n": 0}

    def fake(*args, **kwargs):
        calls["n"] += 1
        raise MinerUError("缺少 MinerU API Token:请设置环境变量 MINERU_API_TOKEN",
                          retryable=False)

    monkeypatch.setattr(batch, "_process_pdf", fake)
    monkeypatch.setattr(batch.time, "sleep", lambda *_: None)
    result = process_one(pdf, config=dict(CFG), work_root=tmp_path / "work",
                         output_dir=tmp_path / "out", retries=3)

    assert result.status == "failed"
    assert calls["n"] == 1                       # 不重试
    assert "Token" in result.error


# ---------------------------------------------------------------- 同名冲突

def test_same_named_sources_in_different_dirs_fail_loudly(tmp_path: Path) -> None:
    """不同目录下的同名源文件会写进同一个 work/ 与 output/ 路径 → 必须判失败。

    旧行为:先转的产出 output/书.epub,后一个被判「已完成」静默跳过 ——
    两本书只得到一本,汇总还报「成功 1 跳过 1」、退出码 0。
    """
    make_pdf(tmp_path / "卷一" / "书.pdf", pages=1)
    make_pdf(tmp_path / "卷二" / "书.pdf", pages=1)
    buf = io.StringIO()
    events.configure(stream=buf)              # 事件流是 GUI 的状态源,必须收到失败
    try:
        results = process_batch(
            [tmp_path / "卷一", tmp_path / "卷二"], config=dict(CFG),
            work_root=tmp_path / "work", output_dir=tmp_path / "out",
        )
    finally:
        events.configure(False)

    assert [r.status for r in results] == ["failed", "failed"]
    assert all("重名冲突" in r.error and "书.pdf" in r.error for r in results)
    assert not list((tmp_path / "out").glob("*.epub"))    # 不产出、更不覆盖
    errs = [json.loads(line) for line in buf.getvalue().splitlines()
            if '"name_conflict"' in line]
    assert len(errs) == 2 and all(e["event"] == "error" for e in errs)


def test_work_dir_collision_after_sanitize_is_detected(tmp_path: Path) -> None:
    """输出名不同但工作目录同名(空白→下划线)也算冲突:两者共用 work/<名>/。"""
    make_pdf(tmp_path / "src" / "我的 书.pdf", pages=1)
    make_pdf(tmp_path / "src" / "我的_书.pdf", pages=1)
    results = process_batch(
        [tmp_path / "src"], config=dict(CFG),
        work_root=tmp_path / "work", output_dir=tmp_path / "out",
    )

    assert [r.status for r in results] == ["failed", "failed"]
    assert results[0].error != results[1].error           # 各自指出对方的名字
    assert "我的 书.pdf" in results[1].error


def test_repeated_same_source_is_not_a_conflict(tmp_path: Path) -> None:
    """同一条路径重复传入不是冲突(既有行为不变:第二次跳过)。"""
    pdf = make_pdf(tmp_path / "src" / "重复.pdf", pages=1)
    results = process_batch(
        [pdf, pdf], config=dict(CFG),
        work_root=tmp_path / "work", output_dir=tmp_path / "out",
    )
    assert [r.status for r in results] == ["done", "skipped"]


# ------------------------------------------- P2-1 产物完好性(截断/损坏不算完成)

def _batch_kwargs(tmp_path: Path) -> dict:
    return dict(config=dict(CFG), work_root=tmp_path / "work", output_dir=tmp_path / "out")


def test_truncated_output_is_not_treated_as_completed(tmp_path: Path) -> None:
    """截断的 EPUB 不比源文件旧、也不是空文件 —— 旧判据会永久跳过它。

    旧行为:is_done() 只看「存在 + 大小 > 0 + mtime ≥ 源」→ 半截产物被当成已完成,
    用户拿到打不开的书,还得自己删文件才能重转(或加 --force)。
    """
    kw = _batch_kwargs(tmp_path)
    pdf = make_pdf(tmp_path / "src" / "截断书.pdf", pages=1)
    assert process_batch([pdf], force=True, **kw)[0].status == "done"
    out = tmp_path / "out" / "截断书.epub"
    truncate_file(out)                       # 留前 30%:大小 > 0、mtime 比源新
    st = out.stat()
    assert st.st_size > 0 and st.st_mtime >= pdf.stat().st_mtime   # 旧判据会判「已完成」

    buf = io.StringIO()
    events.configure(stream=buf)
    try:
        second = process_batch([pdf], **kw)[0]
    finally:
        events.configure(False)

    assert second.status == "done"           # 重新转换,而不是静默跳过
    assert verify_epub(out).ok               # 产物恢复可用
    warns = [json.loads(line) for line in buf.getvalue().splitlines()
             if '"output_incomplete"' in line]
    assert warns and warns[0]["event"] == "warning"      # 不静默
    assert process_batch([pdf], **kw)[0].status == "skipped"      # 完好产物照旧跳过


def test_corrupt_output_is_not_treated_as_completed(tmp_path: Path) -> None:
    """条目表完整、大小和 mtime 都正常,只有数据区坏了 → 同样不算完成。"""
    kw = _batch_kwargs(tmp_path)
    pdf = make_pdf(tmp_path / "src" / "损坏书.pdf", pages=1)
    assert process_batch([pdf], force=True, **kw)[0].status == "done"
    out = tmp_path / "out" / "损坏书.epub"
    corrupt_zip_member(out)

    assert process_batch([pdf], **kw)[0].status == "done"
    assert verify_epub(out).ok


# ------------------------------------------- P2-2 失败产物不能伪装成成功结果

def test_strict_failure_leaves_no_output_that_blocks_rerun(tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """`--strict` 失败时 output 里已经有一份 EPUB:它不能被下一次运行当成「已完成」。

    旧行为:pandoc 先把 out.epub 写好,strict 校验才判失败 —— 产物留在原地,
    下一次运行按「比源新」直接跳过,用户既没拿到合格的书,也没法重转。
    """
    kw = _batch_kwargs(tmp_path)
    pdf = make_pdf(tmp_path / "src" / "严格书.pdf", pages=1)
    out = tmp_path / "out" / "严格书.epub"
    real_verify_epub = batch.verify_epub

    def failing_verify(epub, **kwargs):
        result = VerifyResult()
        result.fail("chapters_empty_mass", "模拟结构校验失败")
        return result

    monkeypatch.setattr(batch, "verify_epub", failing_verify)
    first = process_one(pdf, strict=True, retries=0, **kw)
    assert first.status == "failed"
    assert "模拟结构校验失败" in first.error
    assert not out.exists(), "失败产物留在 output 里会被下一次判成「已完成」"
    assert (tmp_path / "out" / "严格书.epub.failed").is_file()   # 现场保留,不删用户文件

    monkeypatch.setattr(batch, "verify_epub", real_verify_epub)
    second = process_batch([pdf], **kw)[0]
    assert second.status == "done"           # 重跑真的重转,不是跳过
    assert verify_epub(out).ok


def test_half_written_output_from_failed_build_is_moved_aside(tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """pandoc 写了一半就失败:半截产物同样不能留在 output/ 里冒充完成。"""
    kw = _batch_kwargs(tmp_path)
    pdf = make_pdf(tmp_path / "src" / "半截书.pdf", pages=1)

    def partial_build(book_md, work_dir, output_dir, *, out_name=None, **kwargs):
        target = Path(output_dir) / f"{out_name}.epub"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"PK\x03\x04 half written")
        raise RuntimeError("Pandoc 失败(exit=1): 模拟写一半就退出")

    monkeypatch.setattr(batch, "build_epub", partial_build)
    result = process_one(pdf, retries=0, **kw)
    assert result.status == "failed"
    assert result.epub is None
    assert not (tmp_path / "out" / "半截书.epub").exists()
    assert (tmp_path / "out" / "半截书.epub.failed").read_bytes().startswith(b"PK")


def test_previous_good_output_is_kept_when_force_run_fails_early(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """--force 重转在构建之前就失败:上一轮成功的产物是用户的成果,不能动。"""
    kw = _batch_kwargs(tmp_path)
    pdf = make_pdf(tmp_path / "src" / "旧成果.pdf", pages=1)
    assert process_batch([pdf], force=True, **kw)[0].status == "done"
    out = tmp_path / "out" / "旧成果.epub"
    before = (out.stat().st_size, out.stat().st_mtime)

    def boom(*args, **kwargs):
        raise RuntimeError("模拟提取阶段失败")

    monkeypatch.setattr(batch, "_process_pdf", boom)
    result = process_one(pdf, force=True, retries=0, **kw)
    assert result.status == "failed"
    assert out.is_file() and (out.stat().st_size, out.stat().st_mtime) == before
    assert not list((tmp_path / "out").glob("*.failed"))


# ------------------------------------------- P2-3 Pandoc 缺失(环境缺失,不是临时故障)

def test_missing_pandoc_fails_before_expensive_stages(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """没装 pandoc 时必须在提取/OCR 之前失败:跑完整条链路才发现,重跑要再花一次额度。"""
    kw = _batch_kwargs(tmp_path)
    pdf = make_pdf(tmp_path / "src" / "缺依赖.pdf", pages=1)
    stages: list[str] = []
    monkeypatch.setattr(batch, "pandoc_available", lambda: False)
    monkeypatch.setattr(batch, "_process_pdf", lambda *a, **k: stages.append("extract"))

    buf = io.StringIO()
    events.configure(stream=buf)
    try:
        result = process_one(pdf, retries=3, **kw)
    finally:
        events.configure(False)

    assert result.status == "failed"
    assert "pandoc" in result.error.lower()
    assert stages == [], "环境缺失必须在昂贵阶段之前暴露"
    errs = [json.loads(line) for line in buf.getvalue().splitlines()
            if '"missing_dependency"' in line]
    assert len(errs) == 1 and errs[0]["event"] == "error"


def test_missing_pandoc_is_not_retried(tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """`[WinError 2]` 这类环境缺失不是「临时故障」:每次重试都会重跑整条链路。"""
    kw = _batch_kwargs(tmp_path)
    pdf = make_pdf(tmp_path / "src" / "缺失不重试.pdf", pages=1)
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        raise PandocMissingError("未找到 pandoc")

    monkeypatch.setattr(batch, "_process_pdf", boom)
    monkeypatch.setattr(batch.time, "sleep", lambda *_: None)
    result = process_one(pdf, retries=3, **kw)
    assert result.status == "failed"
    assert calls["n"] == 1 and "pandoc" in result.error


def test_build_epub_reports_missing_pandoc(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH 里没有 pandoc → PandocMissingError(而不是让 subprocess 抛 WinError 2)。"""
    monkeypatch.setattr(pandoc.shutil, "which", lambda _: None)
    md = tmp_path / "book.md"
    md.write_text("# 书\n\n正文。\n", encoding="utf-8")
    with pytest.raises(PandocMissingError, match="pandoc"):
        pandoc.build_epub(md, tmp_path / "work", tmp_path / "out", title="书")


def test_winerror2_from_pandoc_is_classified_as_missing_pandoc(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH 探测没漏、真执行时才找不到(WinError 2)→ 同样归为环境缺失。"""
    monkeypatch.setattr(pandoc.shutil, "which", lambda _: "pandoc")
    monkeypatch.setattr(pandoc.subprocess, "run", _raise_winerror2)
    md = tmp_path / "book.md"
    md.write_text("# 书\n\n正文。\n", encoding="utf-8")
    with pytest.raises(PandocMissingError, match="pandoc"):
        pandoc.build_epub(md, tmp_path / "work", tmp_path / "out", title="书")
