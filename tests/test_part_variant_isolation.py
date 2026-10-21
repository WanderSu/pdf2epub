"""段缓存的输入变体隔离(阶段 B §E.8)。

要防的事故只有一件:同一个段 id、同一份源文件指纹,「提交原文件」与「提交渲染纯图
PDF」是两次不同的 OCR —— 如果缓存键不看变体,渲染段会命中原始段(拿到错的文本),
两者还会写同一个 ``_parts/<part_id>/`` 目录(互相覆盖)。

六类场景(与 §E.8 的清单一一对应):

1. 有 ``original`` 缓存时 ``rendered:*`` 不命中;
2. 写 ``rendered:*`` 不影响 ``original`` 的命中,两份产物共存不互相覆盖;
3. 旧 ``.part.json``(无 ``variant`` 字段)按 ``original`` 解释;
4. 两个不同 ``render_fp`` 互不命中;
5. ``resume=False`` 语义不变(不读仍写);
6. 端到端:原文件已缓存时,渲染变体**必须真的重新上传**。
"""
from __future__ import annotations

import json

import pytest

from backends.base import (VARIANT_ORIGINAL, PartStore, TaskCache,
                           file_fingerprint, render_fingerprint,
                           rendered_variant)
from conftest import make_paged_pdf

FP = "源文件指纹"
PARAMS = "参数指纹"
PART = "1-2"


def _store(work, variant=VARIANT_ORIGINAL, *, resume=True) -> PartStore:
    return PartStore(work, "mineru", FP, resume=resume, variant=variant)


def _write_legacy_part(work, part_id: str, text: str) -> None:
    """造一份改动前的段缓存:``.part.json`` 里没有 ``variant`` 字段。"""
    part_dir = work / "_parts" / part_id
    part_dir.mkdir(parents=True, exist_ok=True)
    (part_dir / "full.md").write_text(text, encoding="utf-8")
    (part_dir / ".part.json").write_text(json.dumps({
        "schema": 1, "backend": "mineru", "part_id": part_id,
        "fingerprint": FP, "params": PARAMS, "created_at": 1,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 1

def test_rendered_variant_does_not_hit_an_original_part(tmp_path) -> None:
    original = _store(tmp_path)
    original.save(PART, PARAMS, "原始文件的 OCR 结果")

    rendered = _store(tmp_path, rendered_variant())
    assert rendered.has_result(PART, PARAMS) is False
    assert rendered.load(PART, PARAMS) is None


# ---------------------------------------------------------------- 2

def test_rendered_write_keeps_original_hit_and_does_not_overwrite_it(tmp_path) -> None:
    original = _store(tmp_path)
    original.save(PART, PARAMS, "原始文件的 OCR 结果")

    rendered = _store(tmp_path, "rendered:abc123")
    rendered.save(PART, PARAMS, "渲染纯图的 OCR 结果")

    assert rendered.dir_for(PART) != original.dir_for(PART)
    assert original.md_path(PART).read_text(encoding="utf-8") == "原始文件的 OCR 结果"
    assert rendered.md_path(PART).read_text(encoding="utf-8") == "渲染纯图的 OCR 结果"
    # 原文件那份仍然命中,渲染那份也命中自己,互不串门
    assert _store(tmp_path).load(PART, PARAMS) == original.dir_for(PART)
    assert _store(tmp_path, "rendered:abc123").load(PART, PARAMS) == rendered.dir_for(PART)
    # 目录名可读:原文件路径不变,渲染变体带后缀
    assert original.dir_for(PART).name == PART
    assert rendered.dir_for(PART).name.startswith(f"{PART}__rendered")


def test_part_meta_records_the_variant_it_was_written_with(tmp_path) -> None:
    """``variant`` 由 store 自己写,调用方覆盖不了 —— 否则目录与键会错位。"""
    store = _store(tmp_path, "rendered:abc123")
    store.save(PART, PARAMS, "正文", variant="original")
    meta = json.loads((store.dir_for(PART) / ".part.json").read_text(encoding="utf-8"))
    assert meta["variant"] == "rendered:abc123"
    assert meta["backend"] == "mineru" and meta["part_id"] == PART


# ---------------------------------------------------------------- 3

def test_legacy_part_without_variant_counts_as_original(tmp_path) -> None:
    _write_legacy_part(tmp_path, PART, "改动前写下的段结果")

    assert _store(tmp_path).has_result(PART, PARAMS) is True
    assert _store(tmp_path).load(PART, PARAMS) == tmp_path / "_parts" / PART
    # 渲染变体绝不能"继承"这份历史缓存
    assert _store(tmp_path, "rendered:abc123").has_result(PART, PARAMS) is False


def test_legacy_part_still_hits_after_writing_a_rendered_sibling(tmp_path) -> None:
    _write_legacy_part(tmp_path, PART, "改动前写下的段结果")
    _store(tmp_path, "rendered:abc123").save(PART, PARAMS, "渲染结果")
    assert _store(tmp_path).has_result(PART, PARAMS) is True


# ---------------------------------------------------------------- 4

def test_two_render_fingerprints_are_isolated(tmp_path) -> None:
    low = _store(tmp_path, "rendered:aaaaaa")
    high = _store(tmp_path, "rendered:bbbbbb")
    low.save(PART, PARAMS, "150dpi 的识别结果")

    assert high.has_result(PART, PARAMS) is False
    assert low.has_result(PART, PARAMS) is True
    assert low.dir_for(PART) != high.dir_for(PART)


def test_render_fingerprint_is_stable_and_parameter_sensitive() -> None:
    """指纹由参数决定(命中检查发生在渲染之前),不依赖临时文件内容。"""
    assert render_fingerprint() == render_fingerprint(150, 85)
    assert render_fingerprint(150, 85) != render_fingerprint(300, 85)
    assert render_fingerprint(150, 85) != render_fingerprint(150, 95)
    assert rendered_variant() == f"rendered:{render_fingerprint()}"
    assert len(render_fingerprint()) == 6


# ---------------------------------------------------------------- 5

def test_no_resume_still_writes_but_never_reads(tmp_path) -> None:
    _store(tmp_path, resume=False).save(PART, PARAMS, "强制重做的结果")
    forced = _store(tmp_path, resume=False)
    assert forced.load(PART, PARAMS) is None            # 不读
    assert forced.has_result(PART, PARAMS) is True      # 但事实存在(--no-resume 也要看见本次产物)
    assert _store(tmp_path).has_result(PART, PARAMS) is True


# ---------------------------------------------------------------- TaskCache

def test_task_cache_files_are_per_variant(tmp_path) -> None:
    source = tmp_path / "src.pdf"
    source.write_bytes(b"%PDF-1.4 fake")

    original = TaskCache(tmp_path, "mineru")
    original.save(source, VARIANT_ORIGINAL, batch_id="batch-1")
    assert original.path.name == ".ocr_task.json"

    rendered = TaskCache(tmp_path, "mineru", variant="rendered:abc123")
    assert rendered.path != original.path
    assert rendered.load(source) is None                # 别的变体的任务不能复用
    rendered.save(source, "rendered:abc123", batch_id="batch-2")

    assert original.load(source)["batch_id"] == "batch-1"
    assert rendered.load(source)["batch_id"] == "batch-2"
    assert TaskCache(tmp_path, "mineru").load(source, "rendered:abc123") is None


def test_task_cache_refuses_to_write_another_variant(tmp_path) -> None:
    source = tmp_path / "src.pdf"
    source.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(ValueError):
        TaskCache(tmp_path, "mineru").save(source, "rendered:abc123", batch_id="x")


# ---------------------------------------------------------------- 6(端到端)

def _adapter():
    from backends.mineru_backend import MinerUAdapter

    return MinerUAdapter(token="fake-token", poll_interval=0, timeout=30)


def test_rendered_conversion_really_uploads_instead_of_reusing_original(tmp_path, monkeypatch) -> None:
    """原文件段已缓存时,渲染变体必须真的上传渲染文件(不能用原文件的结果顶替)。

    假云端只记录"提交/上传"次数,不联网;判定标准是**上传真的又发生了一次**,
    以及两个变体的段缓存落在不同目录。
    """
    fake = pytest.importorskip("test_ocr_sharding_resume")
    cloud = fake.install_fake_cloud(monkeypatch)
    work = tmp_path / "work"
    source = make_paged_pdf(tmp_path / "book.pdf", 2)
    adapter = _adapter()

    adapter.convert(source, work)
    first_uploads = len(cloud.uploads)
    assert first_uploads > 0

    adapter.convert(source, work)                       # 全命中,不该再上传
    assert len(cloud.uploads) == first_uploads

    rendered_pdf = make_paged_pdf(tmp_path / "rendered.pdf", 2)
    assert file_fingerprint(rendered_pdf) != file_fingerprint(source)
    token = rendered_variant()
    adapter._convert_inner(rendered_pdf, work, variant=token, source_pdf=source)

    assert len(cloud.uploads) > first_uploads           # 真的重新上传了
    parts_root = work / "_parts"
    names = sorted(p.name for p in parts_root.iterdir())
    assert "1-2" in names
    assert any(name.startswith("1-2__rendered_") for name in names), names

    original_meta = json.loads((parts_root / "1-2" / ".part.json").read_text(encoding="utf-8"))
    rendered_meta = json.loads(
        (parts_root / next(n for n in names if n.startswith("1-2__rendered_")) / ".part.json")
        .read_text(encoding="utf-8")
    )
    assert original_meta["variant"] == VARIANT_ORIGINAL
    assert rendered_meta["variant"] == token

    uploads_after_rendered = len(cloud.uploads)
    adapter._convert_inner(rendered_pdf, work, variant=token, source_pdf=source)
    assert len(cloud.uploads) == uploads_after_rendered   # 同变体第二次才复用
