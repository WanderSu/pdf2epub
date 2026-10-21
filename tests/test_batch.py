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
from conftest import (corrupt_zip_member, make_pdf, rewrite_epub, truncate_file,
                      write_png)
from ir.model import text_sha256
from ir.model import text_sha256

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


# ---------------------------------------------------------------- 清理接线(P6)

def test_conversion_runs_the_ir_clean_engine_and_leaves_an_audit_trail(tmp_path: Path) -> None:
    """正式转换流程走的是 IR 清理引擎,且留下可复核的报告(P6 的接线验收)。

    「引擎单测全绿」和「引擎真的接进了转换流程」是两件事:这里跑的是
    ``process_batch`` → ``_process_markdown`` → ``clean_stage``,断言的落点是
    **产物目录**(``work/<书名>/ir/``)与**清理后的 book.md**,而不是函数返回值。
    """
    raw = "# 手记\n\n正文第一段,句号结尾。\n\n12\n\n正文第二段,句号结尾。\n"
    md = tmp_path / "src" / "清理接线.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(raw, encoding="utf-8")

    results = process_batch(
        [md], config=dict(CFG),
        work_root=tmp_path / "work", output_dir=tmp_path / "out", force=True,
    )
    assert [r.status for r in results] == ["done"]

    work = tmp_path / "work" / "清理接线"
    ir_dir = work / "ir"
    for name in ("meta.json", "blocks.jsonl", "clean-report.json", "clean.diff"):
        assert (ir_dir / name).exists(), f"缺少 IR 产物 {name}"

    report = json.loads((ir_dir / "clean-report.json").read_text(encoding="utf-8"))
    assert report["engine"] == "ir"                       # 不是旧引擎,也不是"没跑"
    assert report["run"]["backend"] == "markdown"
    assert report["run"]["source_md_sha256"] == text_sha256(raw)   # 记的是清理前的输入
    assert "page_numbers" in report["stats"]["by_rule"]  # 独立页码行确实被提了案
    assert all(p["status"] == "applied" for p in report["patches"])

    # 清理真的写回了 book.md(Pandoc 吃的是清理后的这一份)
    cleaned = (work / "book.md").read_text(encoding="utf-8")
    assert "\n12\n" not in cleaned
    assert "正文第一段,句号结尾。" in cleaned


def test_clean_failure_fails_the_file_without_falling_back(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """清理失败 = 该文件失败:错误码 clean_failed、不重试、不产出"没清理过的书"。

    P6 删掉旧引擎后,「清理出问题就走另一条路」不再是选项 —— 静默产出一本没清理的
    书比失败更糟(用户会把噪声当成产物缺陷)。这里把引擎打桩成必然失败,验证批处理
    侧的反应。
    """
    from clean import CleanEngineError

    md = tmp_path / "src" / "清理失败.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("# 手记\n\n正文,句号结尾。\n", encoding="utf-8")

    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        raise CleanEngineError("IR 构造失败(模拟)")

    monkeypatch.setattr(batch, "clean_book_md", boom)
    monkeypatch.setattr(batch.time, "sleep", lambda *_: None)

    result = process_one(md, config=dict(CFG), work_root=tmp_path / "work",
                         output_dir=tmp_path / "out", retries=2, force=True)
    assert result.status == "failed"
    assert calls["n"] == 1                       # 确定性失败:不重试
    assert "IR 构造失败(模拟)" in result.error
    assert result.epub is None
    assert not list((tmp_path / "out").glob("*.epub"))     # 没有半成品交付物


# ---------------------------------------------------------------- 清理接线(P6)

def test_conversion_runs_the_ir_clean_engine_and_leaves_an_audit_trail(tmp_path: Path) -> None:
    """正式转换流程走的是 IR 清理引擎,且留下可复核的报告(P6 的接线验收)。

    「引擎单测全绿」和「引擎真的接进了转换流程」是两件事:这里跑的是
    ``process_batch`` → ``_process_markdown`` → ``clean_stage``,断言的落点是
    **产物目录**(``work/<书名>/ir/``)与**清理后的 book.md**,而不是函数返回值。
    """
    raw = "# 手记\n\n正文第一段,句号结尾。\n\n12\n\n正文第二段,句号结尾。\n"
    md = tmp_path / "src" / "清理接线.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(raw, encoding="utf-8")

    results = process_batch(
        [md], config=dict(CFG),
        work_root=tmp_path / "work", output_dir=tmp_path / "out", force=True,
    )
    assert [r.status for r in results] == ["done"]

    work = tmp_path / "work" / "清理接线"
    ir_dir = work / "ir"
    for name in ("meta.json", "blocks.jsonl", "clean-report.json", "clean.diff"):
        assert (ir_dir / name).exists(), f"缺少 IR 产物 {name}"

    report = json.loads((ir_dir / "clean-report.json").read_text(encoding="utf-8"))
    assert report["engine"] == "ir"                       # 不是旧引擎,也不是"没跑"
    assert report["run"]["backend"] == "markdown"
    assert report["run"]["source_md_sha256"] == text_sha256(raw)   # 记的是清理前的输入
    assert "page_numbers" in report["stats"]["by_rule"]  # 独立页码行确实被提了案
    assert all(p["status"] == "applied" for p in report["patches"])

    # 清理真的写回了 book.md(Pandoc 吃的是清理后的这一份)
    cleaned = (work / "book.md").read_text(encoding="utf-8")
    assert "\n12\n" not in cleaned
    assert "正文第一段,句号结尾。" in cleaned


def test_clean_failure_fails_the_file_without_falling_back(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """清理失败 = 该文件失败:错误码 clean_failed、不重试、不产出"没清理过的书"。

    P6 删掉旧引擎后,「清理出问题就走另一条路」不再是选项 —— 静默产出一本没清理的
    书比失败更糟(用户会把噪声当成产物缺陷)。这里把引擎打桩成必然失败,验证批处理
    侧的反应。
    """
    from clean import CleanEngineError

    md = tmp_path / "src" / "清理失败.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("# 手记\n\n正文,句号结尾。\n", encoding="utf-8")

    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        raise CleanEngineError("IR 构造失败(模拟)")

    monkeypatch.setattr(batch, "clean_book_md", boom)
    monkeypatch.setattr(batch.time, "sleep", lambda *_: None)

    result = process_one(md, config=dict(CFG), work_root=tmp_path / "work",
                         output_dir=tmp_path / "out", retries=2, force=True)
    assert result.status == "failed"
    assert calls["n"] == 1                       # 确定性失败:不重试
    assert "IR 构造失败(模拟)" in result.error
    assert result.epub is None
    assert not list((tmp_path / "out").glob("*.epub"))     # 没有半成品交付物


# ---------------------------------------------------------------- 校验门禁

def test_fatal_verify_error_fails_in_default_mode_too(sample_epub: Path, tmp_path: Path) -> None:
    """致命校验错误:**默认模式**也必须判失败(P8)。

    旧行为:默认模式只把 error 写进日志,任务照旧判成功、退出码 0,残缺 EPUB
    留在 output/ 里 —— 调用方(桌面端/CI)无从发现。P7 记为 R-1,这里收敛。
    """
    broken = rewrite_epub(sample_epub, tmp_path / "broken.epub", drop_suffixes=(".png",))
    with pytest.raises(VerifyError, match="EPUB 校验未通过"):
        verify_output(broken)                        # 默认模式:同样判失败
    with pytest.raises(VerifyError, match="EPUB 校验未通过"):
        verify_output(broken, strict=True)           # 严格模式:判失败


def _fake_verify(monkeypatch: pytest.MonkeyPatch,
                 issues: tuple[tuple[str, str, str], ...],
                 *, strict: bool = False) -> VerifyResult:
    """把 ``batch.verify_epub`` 换成"只产出指定 issue"的假校验器后跑 verify_output。"""
    def fake(epub, **kwargs):
        result = VerifyResult()
        for level, code, message in issues:
            result.add(level, code, message)
        return result

    monkeypatch.setattr(batch, "verify_epub", fake)
    return verify_output(Path("whatever.epub"), strict=strict)


@pytest.mark.parametrize("code", ["image_orphan", "empty_chapters", "cover_not_first",
                                  "content_text_less", "content_headings_lost"])
def test_non_fatal_warnings_are_tolerated_by_default(monkeypatch: pytest.MonkeyPatch,
                                                     code: str) -> None:
    """非致命警告:默认只记录,不判失败 —— 不能把 warning 一律升级成错误。"""
    result = _fake_verify(monkeypatch, (("warning", code, f"模拟 {code}"),))
    assert result.ok
    assert [i.code for i in result.warnings] == [code]


def test_strict_upgrades_only_content_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--strict`` 只升级**内容对照**类警告:排版/格式类提示仍然只是提示。

    "严格"不等于"所有 warning 都判失败":正常书几乎总带几条格式提示,一律升级
    会让每本书都失败,用户随即学会忽略提示 —— 报警的价值也就没了。
    """
    with pytest.raises(VerifyError, match="模拟 content_text_less"):
        _fake_verify(monkeypatch,
                     (("warning", "content_text_less", "模拟 content_text_less"),),
                     strict=True)
    tolerated = _fake_verify(monkeypatch, (("warning", "image_orphan", "孤立图片"),),
                             strict=True)
    assert tolerated.ok


def _image_md(path: Path, names: tuple[str, ...]) -> Path:
    """一篇引用若干图片的 Markdown(正文够长,免得被判空章节)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n\n".join(f"![插图 {i}](images/{name})" for i, name in enumerate(names, 1))
    path.write_text(f"# 图册\n\n这是一段足够长的正文,用来说明图片之外的正文也完整无缺。\n\n{body}\n",
                    encoding="utf-8")
    return path


def test_missing_images_directory_fails_in_default_mode(tmp_path: Path) -> None:
    """`images/` 整个目录不存在:默认模式必须判失败,且不留可被误认成功的产物。"""
    md = _image_md(tmp_path / "src" / "整批缺图.md", ("a.png", "b.png"))
    out = tmp_path / "out"
    result = process_one(md, config=dict(CFG), work_root=tmp_path / "work",
                         output_dir=out, force=True)
    assert result.status == "failed"
    assert result.epub is None
    assert "图片" in result.error
    assert not (out / "整批缺图.epub").exists(), "残缺产物不能留在成功输出位置"
    assert (out / "整批缺图.epub.failed").is_file(), "失败产物改名保留现场,便于排查"


def test_single_missing_image_fails_in_default_mode(tmp_path: Path) -> None:
    """只缺一张图:同样判失败;清理阶段还要在报告里如实记一条「图片引用缺失」。"""
    md = _image_md(tmp_path / "src" / "缺一张.md", ("a.png", "b.png"))
    images = md.parent / "images"
    images.mkdir(parents=True, exist_ok=True)
    write_png(images / "a.png")
    out = tmp_path / "out"
    result = process_one(md, config=dict(CFG), work_root=tmp_path / "work",
                         output_dir=out, force=True)
    assert result.status == "failed"
    assert not (out / "缺一张.epub").exists()
    assert (out / "缺一张.epub.failed").is_file()

    report = json.loads((tmp_path / "work" / "缺一张" / "ir" / "clean-report.json")
                        .read_text(encoding="utf-8"))
    assert any("图片引用缺失" in issue and "b.png" in issue for issue in report["issues"])


def test_verifier_exception_never_reports_success(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """校验器自己抛异常:不能因为异常没被识别成"校验失败"就把任务报成成功。"""
    def boom(epub, **kwargs):
        raise RuntimeError("校验器内部错误(模拟)")

    monkeypatch.setattr(batch, "verify_epub", boom)
    monkeypatch.setattr(batch.time, "sleep", lambda *_: None)
    md = tmp_path / "src" / "校验炸了.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("# 校验炸了\n\n正文,句号结尾。\n", encoding="utf-8")
    out = tmp_path / "out"
    result = process_one(md, config=dict(CFG), work_root=tmp_path / "work",
                         output_dir=out, retries=0, force=True)
    assert result.status == "failed"
    assert "校验器内部错误" in result.error
    assert result.epub is None
    assert not list(out.glob("*.epub"))


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


# ------------------------------------------- P9 旧成品保全(重转失败的代价)

def _good_md(path: Path, title: str = "重转书") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {title}\n\n这是一段足够长的正文,用来保证产物本身是合格的成品。\n",
                    encoding="utf-8")
    return path


def _failing_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """让新一轮「构建成功、校验失败」——最容易被忽略的覆盖旧成品的场景。"""
    def fake(epub, **kwargs):
        result = VerifyResult()
        result.fail("chapters_empty_mass", "模拟校验失败")
        return result

    monkeypatch.setattr(batch, "verify_epub", fake)


def test_previous_good_epub_survives_a_failing_reconversion(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """新一轮**构建成功但校验失败**时,上一轮能读的那本书必须还在(P9)。

    pandoc 是就地写 ``output/<名>.epub`` 的:旧行为下旧成品先被新字节盖掉,失败产物
    再被改名移走 —— 用户重转一次,output/ 里只剩一个 ``.failed``,原本能读的书没了。
    重转失败的正确结果是「回到还有上一本」,而不是连上一本一起赔进去。
    """
    kw = _batch_kwargs(tmp_path)
    md = _good_md(tmp_path / "src" / "重转书.md")
    assert process_one(md, force=True, retries=0, **kw).status == "done"
    out = tmp_path / "out" / "重转书.epub"
    good = out.read_bytes()

    _failing_verify(monkeypatch)
    result = process_one(md, force=True, retries=0, **kw)
    assert result.status == "failed"
    assert result.epub is None                       # 失败产物不是交付物
    assert out.is_file(), "上一轮能读的书不能因为重转失败而消失"
    assert out.read_bytes() == good, "还原的必须是上一轮那个字节不差的成品"
    assert batch.archive_issue(out) is None          # 还原物本身完好,可被后续复用
    assert (tmp_path / "out" / "重转书.epub.failed").is_file()   # 失败现场另行保留
    assert "已恢复上一轮的成品" in result.error      # 且不静默


def test_incomplete_previous_epub_is_not_restored(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """上一轮产物本身就是残缺的:没有保全价值,不该被放回 output/ 冒充完成。"""
    kw = _batch_kwargs(tmp_path)
    md = _good_md(tmp_path / "src" / "残书.md")
    assert process_one(md, force=True, retries=0, **kw).status == "done"
    out = tmp_path / "out" / "残书.epub"
    truncate_file(out)                               # 上一轮就是半截产物

    _failing_verify(monkeypatch)
    result = process_one(md, force=True, retries=0, **kw)
    assert result.status == "failed"
    assert not out.exists(), "残缺旧产物不该被还原回成功输出位置"
    assert (tmp_path / "out" / "残书.epub.failed").is_file()
    assert "已恢复上一轮的成品" not in result.error


def test_verifier_exception_closes_verify_stage_and_is_coded_verify_failed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """校验器自己抛异常:`verify` 阶段要有 done 收尾,错误码归 verify_failed(P9)。

    只有 start 没有 done 时前端会永久停在 95%;报成 ``convert_failed`` 则会让人去查
    pandoc,而 pandoc 这一步其实已经写出了产物。
    """
    kw = _batch_kwargs(tmp_path)
    md = _good_md(tmp_path / "src" / "校验炸了.md", title="校验炸了")

    def boom(epub, **kwargs):
        raise RuntimeError("校验器内部错误(模拟)")

    monkeypatch.setattr(batch, "verify_epub", boom)
    monkeypatch.setattr(batch.time, "sleep", lambda *_: None)

    buf = io.StringIO()
    events.configure(stream=buf)
    try:
        result = process_one(md, retries=0, force=True, **kw)
    finally:
        events.configure(False)

    assert result.status == "failed"
    assert "校验器内部错误" in result.error
    lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    stages = [(e["name"], e["state"], bool(e.get("failed")))
              for e in lines if e.get("event") == "stage"]
    assert ("verify", "start", False) in stages
    assert ("verify", "done", True) in stages, "校验异常也要收尾,否则前端进度卡死"
    codes = [e["code"] for e in lines if e.get("event") == "error"]
    assert codes == ["verify_failed"]


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
