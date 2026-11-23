"""安全解压的用例:路径穿越、绝对路径、符号链接与体积/数量上限。"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from pdf2epub.backends.safe_zip import UnsafeArchiveError, safe_extract


def _make_zip(path: Path, entries: dict[str, bytes], *, attrs: dict | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (attrs or {}).get(name, 0o600 << 16)
            zf.writestr(info, data)
    return path


def test_extracts_normal_members(tmp_path: Path) -> None:
    src = _make_zip(tmp_path / "a.zip", {"full.md": b"# hi", "images/1.jpg": b"jpg"})
    written = safe_extract(src, tmp_path / "out")
    assert (tmp_path / "out" / "full.md").read_bytes() == b"# hi"
    assert (tmp_path / "out" / "images" / "1.jpg").read_bytes() == b"jpg"
    assert len(written) == 2


@pytest.mark.parametrize(
    "name",
    ["../evil.txt", "images/../../evil.txt", "/etc/passwd", "C:/windows/evil.txt", "a\\..\\..\\evil.txt"],
)
def test_rejects_unsafe_paths(tmp_path: Path, name: str) -> None:
    src = _make_zip(tmp_path / "a.zip", {name: b"x"})
    with pytest.raises(UnsafeArchiveError):
        safe_extract(src, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()


def test_rejects_symlink_entry(tmp_path: Path) -> None:
    src = _make_zip(
        tmp_path / "a.zip",
        {"link": b"target"},
        attrs={"link": 0o120777 << 16},
    )
    with pytest.raises(UnsafeArchiveError):
        safe_extract(src, tmp_path / "out")


def test_rejects_too_many_entries(tmp_path: Path) -> None:
    src = _make_zip(tmp_path / "a.zip", {f"f{i}.txt": b"x" for i in range(5)})
    with pytest.raises(UnsafeArchiveError):
        safe_extract(src, tmp_path / "out", max_entries=4)


def test_rejects_oversized_member(tmp_path: Path) -> None:
    src = _make_zip(tmp_path / "a.zip", {"big.bin": b"x" * 2048})
    with pytest.raises(UnsafeArchiveError):
        safe_extract(src, tmp_path / "out", max_file_bytes=1024)


def test_rejects_total_size_over_budget(tmp_path: Path) -> None:
    src = _make_zip(tmp_path / "a.zip", {"a.bin": b"x" * 900, "b.bin": b"y" * 900})
    with pytest.raises(UnsafeArchiveError):
        safe_extract(src, tmp_path / "out", max_total_bytes=1000)


def test_wanted_filter_only_writes_selected(tmp_path: Path) -> None:
    src = _make_zip(
        tmp_path / "a.zip",
        {"full.md": b"md", "images/1.jpg": b"jpg", "layout.json": b"{}"},
    )
    written = safe_extract(src, tmp_path / "out", wanted=lambda n: n.endswith(".md"))
    assert [p.name for p in written] == ["full.md"]
    assert not (tmp_path / "out" / "layout.json").exists()
    assert not (tmp_path / "out" / "images").exists()


def test_wanted_does_not_hide_unsafe_member(tmp_path: Path) -> None:
    """被过滤掉的恶意条目也要让整包失败 —— 不能因为「不读它」就放行。"""
    src = _make_zip(tmp_path / "a.zip", {"full.md": b"md", "../evil.txt": b"x"})
    with pytest.raises(UnsafeArchiveError):
        safe_extract(src, tmp_path / "out", wanted=lambda n: n.endswith(".md"))
