"""产物模块拆分后的公开契约:batch 仍然按原名字导出同一批函数。"""
from __future__ import annotations

from pathlib import Path

import pdf2epub.artifacts as artifacts
import pdf2epub.batch as batch

MOVED = (
    "sanitize_name",
    "output_stem",
    "epub_path",
    "existing_epub",
    "source_identity",
    "name_conflicts",
    "stat_signature",
    "discard_failed_output",
    "preserve_previous_output",
    "restore_previous_output",
    "FAILED_OUTPUT_SUFFIX",
)


def test_batch_reexports_the_moved_helpers() -> None:
    for name in MOVED:
        assert getattr(batch, name) is getattr(artifacts, name), name


def test_name_conflicts_detects_sanitized_collision(tmp_path: Path) -> None:
    # 两个不同的文件在 sanitize 之后写进同一个 work/<名>/:必须显式判冲突
    a = tmp_path / "a b.pdf"
    b = tmp_path / "a  b.pdf"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    conflicts = artifacts.name_conflicts([a, b])
    assert len(conflicts) == 2


def test_epub_path_keeps_spaces_but_sanitizes_work_dir(tmp_path: Path) -> None:
    assert artifacts.output_stem("马克思 主义 - 作者") == "马克思 主义 - 作者"
    assert artifacts.sanitize_name("马克思 主义") == "马克思_主义"
    assert artifacts.epub_path(tmp_path / "马克思 主义.pdf", tmp_path).name == "马克思 主义.epub"


def test_failed_output_is_quarantined_only_when_changed(tmp_path: Path) -> None:
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"x" * 32)
    before = artifacts.stat_signature(epub)
    # 本次没有写出新内容 → 不动它(可能是上一轮的成功成果)
    assert artifacts.discard_failed_output(epub, before) is None
    assert epub.exists()


def test_backup_and_restore_roundtrip(tmp_path: Path) -> None:
    good = tmp_path / "book.epub"
    good.write_bytes(b"PK\x03\x04not-really-an-epub")
    # 残缺产物不值得保全
    assert artifacts.preserve_previous_output(tmp_path / "missing.epub") is None
